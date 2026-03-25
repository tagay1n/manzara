"""Shared utility functions for embedded monocorpus-derived runtimes."""

from __future__ import annotations

import base64
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any
import zipfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import requests
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
import yaml

prefix = "enc:"

workdir = "~/.monocorpus"
REPO_ROOT = Path(__file__).resolve().parents[2]
REDACTED_SENTINEL = "<REDACTED>"
ENTRY_POINT_DIR = "0_entry_point"
UPSTREAM_METADATA_DIR = "misc/upstream_metadata"


def read_config(config_file: str = "config.yaml"):
    """Load YAML config with local-first precedence and redaction guard."""
    env_override = os.environ.get("MANZARA_CONFIG_PATH")
    candidates: list[Path]
    if env_override:
        candidates = [Path(env_override).expanduser()]
    elif config_file != "config.yaml":
        candidates = [REPO_ROOT / config_file]
    else:
        candidates = [
            REPO_ROOT / "config.local.yaml",
            REPO_ROOT / "config.yaml",
        ]

    checked: list[str] = []
    for config_path in candidates:
        checked.append(str(config_path))
        if not config_path.exists():
            continue
        with open(config_path, "r", encoding="utf-8") as file:
            config = yaml.safe_load(file) or {}
        if not isinstance(config, dict):
            raise ValueError(f"Config at {config_path} must be a YAML mapping")
        if _contains_redacted(config):
            raise ValueError(
                f"Config at {config_path} contains '{REDACTED_SENTINEL}'. "
                "Use a local unmasked config (config.local.yaml or config.yaml)."
            )
        return config

    searched = ", ".join(checked)
    raise FileNotFoundError(f"No config file found. Checked: {searched}")


def _contains_redacted(node: Any) -> bool:
    """Return True if config node contains placeholder redacted values."""
    if isinstance(node, str):
        return REDACTED_SENTINEL in node
    if isinstance(node, dict):
        return any(_contains_redacted(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_redacted(value) for value in node)
    return False


def get_engine(echo: bool = False):
    """Create a SQLAlchemy engine from the configured database URL."""
    config = read_config()
    return create_engine(
        config["database_url"],
        echo=echo,
        json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False),
    )


def get_session():
    """Return a new SQLAlchemy session bound to the configured engine."""
    session_factory = sessionmaker(bind=get_engine())
    return session_factory()


def _path_part(value: Any) -> str:
    if hasattr(value, "value"):
        return str(getattr(value, "value"))
    return str(value)


def pick_files(dir_path: Any):
    """List all files under a workdir subdirectory."""
    return [
        os.path.normpath(os.path.join(dir_name, filename))
        for dir_name, _, files in os.walk(get_in_workdir(dir_path))
        for filename in files
    ]


def calculate_md5(file_path: str):
    """Calculate MD5 hash of the file."""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(2048), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def get_in_workdir(*dir_names: Any, file: str | None = None, prefix: str = workdir):
    """Build (and create) a path under workdir, optionally including a filename."""
    normalized_dirs = [_path_part(item) for item in dir_names]
    script_parent_dir = os.path.dirname(os.path.realpath(sys.argv[0]))
    path_parts = [script_parent_dir, "..", os.path.expanduser(prefix), *normalized_dirs]
    path = os.path.normpath(os.path.join(*path_parts))
    os.makedirs(path, exist_ok=True)
    if file:
        return os.path.join(path, file)
    return path


def obtain_documents(cli_params, ya_client, entity_cls, predicate=None, limit=None, offset=None, session=None):
    """Yield documents based on CLI filters (md5/path) and optional predicates."""
    if session is None:
        session = get_session()
    md5 = getattr(cli_params, "md5", None)
    md5s = getattr(cli_params, "md5s", None)
    path = getattr(cli_params, "path", None)

    def _yield_by_md5(_md5, _predicate):
        print(f"Looking for document by md5 '{_md5}'")
        if _predicate is None:
            _predicate = entity_cls.md5 == _md5
        else:
            _predicate &= entity_cls.md5 == _md5
        yield from _find(session, predicate=_predicate, limit=1, entity_cls=entity_cls)

    def _yield_by_md5s(_md5s, _predicate):
        print(f"Looking for {len(_md5s)} documents by provided md5 list")
        if _predicate is None:
            _predicate = entity_cls.md5.in_(_md5s)
        else:
            _predicate &= entity_cls.md5.in_(_md5s)
        yield from _find(
            session,
            predicate=_predicate,
            limit=limit,
            offset=offset,
            entity_cls=entity_cls,
        )

    def _yield_by_path(_path, _predicate):
        _meta = ya_client.get_meta(_path, fields=["md5", "type", "path"])
        if _meta.type == "file":
            yield from _yield_by_md5(_meta.md5, _predicate)
        elif _meta.type == "dir":
            print(f"Traversing documents by path '{_path}'")
            unprocessed_docs = {doc.md5: doc for doc in _find(session, _predicate, entity_cls=entity_cls)}
            counter = 0
            dirs_to_visit = [_meta.path]
            while dirs_to_visit:
                dir_to_visit = dirs_to_visit.pop(0)
                for item in ya_client.listdir(dir_to_visit, max_items=None, fields=["md5", "type", "path"]):
                    if item.type == "dir":
                        dirs_to_visit.append(item.path)
                    elif item.type == "file" and (doc := unprocessed_docs.get(item.md5)):
                        yield doc
                        if limit:
                            counter += 1
                            if counter >= limit:
                                return

    if md5:
        yield from _yield_by_md5(md5, predicate)
    elif md5s:
        yield from _yield_by_md5s(md5s, predicate)
    elif path:
        yield from _yield_by_path(path, predicate)
    else:
        print("Traversing all unprocessed documents")
        yield from _find(session, predicate=predicate, limit=limit, offset=offset, entity_cls=entity_cls)


def download_file_locally(ya_client, doc, config):
    """Download a document to entry point if missing or outdated."""

    def _extension_by_mime_type(mime_type):
        if mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            return ".docx"
        if mime_type == "text/plain":
            return ".txt"
        if mime_type == "text/html":
            return ".html"
        if mime_type == "application/pdf":
            return ".pdf"
        raise ValueError("Unexpected mime type")

    _, ext = os.path.splitext(doc.ya_path)
    if not ext:
        ext = _extension_by_mime_type(doc.mime_type)
    local_path = get_in_workdir(ENTRY_POINT_DIR, file=f"{doc.md5}{ext}")
    if not (os.path.exists(local_path) and calculate_md5(local_path) == doc.md5):
        url = decrypt(doc.ya_public_url, config) if doc.sharing_restricted else doc.ya_public_url
        with open(local_path, "wb") as file_obj:
            ya_client.download_public(url, file_obj)
    return local_path


def _find(session, entity_cls, predicate=None, limit=None, offset=None):
    """Yield ORM results for the given entity and predicate."""
    statement = select(entity_cls)
    if predicate is not None:
        statement = statement.where(predicate)
    if limit:
        statement = statement.limit(limit)

    if offset:
        # Keep current behavior from embedded source runtime.
        statement.offset(offset)

    result = session.scalars(statement)
    yield from result


def walk_yadisk(
    client,
    root,
    fields=None,
):
    """Yield all file resources under ``root`` on Yandex Disk."""
    if fields is None:
        fields = [
            "type",
            "path",
            "mime_type",
            "md5",
            "public_key",
            "public_url",
            "resource_id",
            "name",
        ]
    fields.append("type")
    queue = deque([root])
    while queue:
        current = queue.popleft()
        print(f"Visiting '{current}'")
        empty = True
        for resource in client.listdir(current, max_items=30_000, fields=fields):
            empty = False
            if resource.type == "dir":
                queue.append(resource.path)
            else:
                yield resource
        if empty:
            print(f"Removing folder `{current}` because it is empty")
            client.remove(current, force_async=True, wait=False)


def encrypt(url, config):
    """Encrypt a URL for restricted sharing."""
    key = base64.urlsafe_b64decode(config["encryption_key"])
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    encrypted = aesgcm.encrypt(nonce, url.encode(), None)
    ciphercode = base64.urlsafe_b64encode(nonce + encrypted).decode()
    return f"{prefix}{ciphercode}"


def decrypt(ciphertext, config):
    """Decrypt a previously encrypted URL."""
    encrypted_url = ciphertext.removeprefix(prefix)
    data = base64.urlsafe_b64decode(encrypted_url)
    nonce, ct = data[:12], data[12:]
    key = base64.urlsafe_b64decode(config["encryption_key"])
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode()


def load_upstream_metadata(upstream_meta_url, md5):
    """Download upstream metadata ZIP and return sanitized JSON as a string."""
    if not upstream_meta_url:
        return None
    upstream_metadata_zip = get_in_workdir(UPSTREAM_METADATA_DIR, file=f"{md5}.zip")
    with open(upstream_metadata_zip, "wb") as zip_file, requests.get(
        upstream_meta_url, stream=True
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_content(chunk_size=8192):
            zip_file.write(chunk)

    upstream_metadata_unzip = get_in_workdir(UPSTREAM_METADATA_DIR, md5)
    with zipfile.ZipFile(upstream_metadata_zip, "r") as archive:
        archive.extractall(upstream_metadata_unzip)

    with open(os.path.join(upstream_metadata_unzip, "metadata.json"), "r") as raw_meta:
        metadata = json.load(raw_meta)
        metadata.pop("available_pages", None)
        metadata.pop("doc_card_url", None)
        metadata.pop("download_code", None)
        metadata.pop("doc_url", None)
        metadata.pop("access", None)
        metadata.pop("lang", None)
        return json.dumps(metadata, ensure_ascii=False)


__all__ = [
    "ENTRY_POINT_DIR",
    "REDACTED_SENTINEL",
    "REPO_ROOT",
    "UPSTREAM_METADATA_DIR",
    "_contains_redacted",
    "_find",
    "calculate_md5",
    "decrypt",
    "download_file_locally",
    "encrypt",
    "get_engine",
    "get_in_workdir",
    "get_session",
    "load_upstream_metadata",
    "obtain_documents",
    "pick_files",
    "prefix",
    "read_config",
    "walk_yadisk",
    "workdir",
]
