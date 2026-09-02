"""DocLayNet inference used to decide whether PDF pages are useful previews."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable


DOCLAYNET_REPO_ID = "hantian/yolo-doclaynet"
DOCLAYNET_REVISION = "49b97586dbd3bdae169e8f5e165710d0facf5f1e"
DOCLAYNET_CHECKPOINT = "yolov12l-doclaynet.pt"
DOCLAYNET_IMAGE_SIZE = 1024
DOCLAYNET_CONFIDENCE = 0.25
DOCLAYNET_IOU = 0.45
DOCLAYNET_MAX_DETECTIONS = 300
_NON_RELEVANT_CLASSES = {"page-header", "page-footer", "picture"}


class PreviewModelError(RuntimeError):
    """The shared preview classifier could not be loaded or executed."""


@dataclass(frozen=True)
class PageAssessment:
    """One page-level DocLayNet decision and its audit details."""

    useful: bool
    detected_classes: tuple[str, ...]
    inference_seconds: float


def _normalized_class(value: object) -> str:
    return str(value or "").strip().lower().replace("_", "-").replace(" ", "-")


def qualifying_layout_classes(class_names: Iterable[object]) -> tuple[str, ...]:
    """Return stable unique classes that make a page useful as a preview."""
    selected: list[str] = []
    seen: set[str] = set()
    for raw_name in class_names:
        name = str(raw_name or "").strip()
        normalized = _normalized_class(name)
        if not normalized or normalized in _NON_RELEVANT_CLASSES or normalized in seen:
            continue
        seen.add(normalized)
        selected.append(name)
    return tuple(selected)


class DocLayNetPageDetector:
    """Single-page CPU inference wrapper around the pinned DocLayNet checkpoint."""

    def __init__(self, model: Any) -> None:
        self._model = model

    @classmethod
    def from_huggingface(cls, *, cache_dir: Path) -> "DocLayNetPageDetector":
        runtime_config_dir = cache_dir.parent
        ultralytics_config_dir = runtime_config_dir / "ultralytics"
        matplotlib_config_dir = runtime_config_dir / "matplotlib"
        ultralytics_config_dir.mkdir(parents=True, exist_ok=True)
        matplotlib_config_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("YOLO_CONFIG_DIR", str(ultralytics_config_dir))
        os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_config_dir))
        try:
            from huggingface_hub import hf_hub_download
            from ultralytics import YOLO
        except ImportError as exc:  # pragma: no cover - deployment validation
            raise PreviewModelError(
                "Preview detector dependencies are missing; install ultralytics and its CPU runtime"
            ) from exc

        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            checkpoint_path = hf_hub_download(
                repo_id=DOCLAYNET_REPO_ID,
                filename=DOCLAYNET_CHECKPOINT,
                revision=DOCLAYNET_REVISION,
                cache_dir=str(cache_dir),
            )
            model = YOLO(checkpoint_path)
        except Exception as exc:
            raise PreviewModelError(f"Failed to initialize preview detector: {exc}") from exc
        return cls(model)

    def assess(self, image: Any, *, page_number: int) -> PageAssessment:
        started = perf_counter()
        try:
            predictions = self._model.predict(
                image,
                verbose=False,
                imgsz=DOCLAYNET_IMAGE_SIZE,
                device="cpu",
                conf=DOCLAYNET_CONFIDENCE,
                iou=DOCLAYNET_IOU,
                max_det=DOCLAYNET_MAX_DETECTIONS,
                agnostic_nms=False,
            )
            if not predictions:
                class_names: list[str] = []
            else:
                result = predictions[0].cpu()
                boxes = result.boxes
                names = result.names
                class_names = []
                if boxes is not None:
                    for cls_index in boxes.cls:
                        class_id = int(cls_index.item())
                        if isinstance(names, dict):
                            class_names.append(str(names.get(class_id, f"class_{class_id}")))
                        else:
                            class_names.append(
                                str(names[class_id])
                                if 0 <= class_id < len(names)
                                else f"class_{class_id}"
                            )
        except Exception as exc:
            raise PreviewModelError(
                f"DocLayNet inference failed for PDF page {int(page_number)}: {exc}"
            ) from exc

        qualifying = qualifying_layout_classes(class_names)
        return PageAssessment(
            useful=bool(qualifying),
            detected_classes=qualifying,
            inference_seconds=max(0.0, perf_counter() - started),
        )


__all__ = [
    "DOCLAYNET_CHECKPOINT",
    "DOCLAYNET_CONFIDENCE",
    "DOCLAYNET_IMAGE_SIZE",
    "DOCLAYNET_IOU",
    "DOCLAYNET_MAX_DETECTIONS",
    "DOCLAYNET_REPO_ID",
    "DOCLAYNET_REVISION",
    "DocLayNetPageDetector",
    "PageAssessment",
    "PreviewModelError",
    "qualifying_layout_classes",
]
