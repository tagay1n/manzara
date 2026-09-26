"""Read-only sharing-policy validation for catalog publication."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

RESTRICTED_PATH = (
    "/neurotatarlar/kitaplar/monocorpus/__ТАРАТМАСКА_DONT_SHARE_НЕ_ДЕЛИТЬСЯ"
)


class SharingValidationError(RuntimeError):
    """The catalog contains documents that are unsafe to publish."""


def validate_catalog_sharing(records: Iterable[Mapping[str, Any]]) -> None:
    """Reject restricted-path exposure, reporting identities without URLs.

    Encryption is identified by the application's enc: storage marker. This
    check does not decrypt links or require access to the encryption key.
    """
    violations = []
    for row in records:
        path = str(row.get("ya_path") or "").strip().removeprefix("disk:").rstrip("/")
        restricted = path == RESTRICTED_PATH or path.startswith(RESTRICTED_PATH + "/")
        document_url = str(row.get("document_url") or "").strip()
        public_url = str(row.get("ya_public_url") or "").strip()
        encrypted = document_url.startswith("enc:")
        rules = []
        if restricted:
            if row.get("sharing_restricted") is not True:
                rules.append("sharing_restricted must be true")
            if document_url and (not encrypted or not document_url[4:].strip()):
                rules.append(
                    "document_url must contain an encrypted link (enc: with payload)"
                )
            if public_url:
                rules.append("ya_public_url must be empty")
        if rules:
            # ASCII JSON also prevents multiline values becoming log commands.
            identity = json.dumps(str(row.get("md5") or "<missing>"), ensure_ascii=True)
            violations.append(f"md5={identity}: {'; '.join(rules)}")
    if violations:
        raise SharingValidationError(
            f"Catalog sharing validation failed for {len(violations)} document(s); "
            "export blocked.\n" + "\n".join(violations)
        )
