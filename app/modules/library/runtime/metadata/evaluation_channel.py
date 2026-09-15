"""Worker coordination, deferrals, and evaluation artifact state."""

from __future__ import annotations

import os
import threading

from app.artifacts import durable_path


def _unprocessables_dir() -> str:
    return str(durable_path("library", "metadata-evaluation"))


class Channel:
    """Shared state between workers: exhausted keys and failed docs."""

    def __init__(self, dry_run: bool):
        self.lock = threading.Lock()
        self.dry_run = dry_run
        self.unprocessable_docs = self._load_file(
            _unprocessables_dir(), "unprocessables_eval.txt"
        )
        self.fatal_error: str | None = None
        self.deferred_docs: set[str] = set()

    def defer_document(self, md5: str) -> None:
        with self.lock:
            self.deferred_docs.add(str(md5))

    def get_deferred_docs(self) -> set[str]:
        with self.lock:
            return set(self.deferred_docs)

    def get_all_unprocessable_docs(self) -> set[str]:
        return self.unprocessable_docs

    def dump(self) -> None:
        if self.dry_run:
            return
        with self.lock:
            self._dump_to_file(
                _unprocessables_dir(),
                "unprocessables_eval.txt",
                self.unprocessable_docs,
            )

    def _load_file(self, dir_name: str, file_name: str) -> set[str]:
        candidates = [os.path.join(dir_name, file_name)]
        leaf_dir = os.path.basename(dir_name.rstrip("/"))
        if leaf_dir:
            candidates.append(os.path.join("_artifacts", leaf_dir, file_name))
            candidates.append(os.path.join(leaf_dir, file_name))

        loaded = set()
        for file_path in candidates:
            if os.path.exists(file_path):
                with open(file_path, "r") as f:
                    loaded.update(
                        {line.strip() for line in f.readlines() if line.strip()}
                    )
        return loaded

    def _dump_to_file(self, dir_name: str, file_name: str, items: set[str]) -> None:
        os.makedirs(dir_name, exist_ok=True)
        file_path = os.path.join(dir_name, file_name)
        with open(file_path, "w") as f:
            f.write("\n".join(sorted(items)))

    def set_fatal_error(self, text: str) -> None:
        with self.lock:
            if self.fatal_error is None:
                self.fatal_error = str(text or "").strip() or "Gemini fatal error"

    def get_fatal_error(self) -> str | None:
        with self.lock:
            return self.fatal_error

    def add_unprocessable_doc(self, md5: str) -> None:
        with self.lock:
            self.unprocessable_docs.add(md5)
            if not self.dry_run:
                self._dump_to_file(
                    _unprocessables_dir(),
                    "unprocessables_eval.txt",
                    self.unprocessable_docs,
                )
