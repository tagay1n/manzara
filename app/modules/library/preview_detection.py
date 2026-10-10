"""DocLayNet inference used to decide whether PDF pages are useful previews."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

from app.runtime_config import config_integer, config_number, config_text

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


def configure_detector_environment(cache_dir: Path) -> None:
    """Set process-wide dependency paths once at CLI bootstrap, before the task worker starts."""
    runtime_config_dir = cache_dir.parent
    for variable, directory in (("YOLO_CONFIG_DIR", "ultralytics"), ("MPLCONFIGDIR", "matplotlib")):
        path = runtime_config_dir / directory
        path.mkdir(parents=True, exist_ok=True)
        os.environ[variable] = str(path)
    os.environ["YOLO_AUTOINSTALL"] = "false"
    os.environ["YOLO_VERBOSE"] = "false"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"


class DocLayNetPageDetector:
    """Single-page CPU inference wrapper around the pinned DocLayNet checkpoint."""

    def __init__(self, model: Any) -> None:
        self._model = model

    @classmethod
    def from_huggingface(cls, *, cache_dir: Path) -> "DocLayNetPageDetector":
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
                repo_id=config_text("previews", "detector", "repo_id"),
                filename=config_text("previews", "detector", "checkpoint"),
                revision=config_text("previews", "detector", "revision"),
                cache_dir=str(cache_dir),
            )
            model = YOLO(checkpoint_path, verbose=False)
        except Exception as exc:
            raise PreviewModelError(f"Failed to initialize preview detector: {exc}") from exc
        return cls(model)

    def assess(self, image: Any, *, page_number: int) -> PageAssessment:
        started = perf_counter()
        try:
            predictions = self._model.predict(
                image,
                verbose=False,
                save=False,
                save_txt=False,
                save_crop=False,
                visualize=False,
                show=False,
                imgsz=config_integer("previews", "detector", "image_size"),
                device="cpu",
                conf=config_number("previews", "detector", "confidence", maximum=1),
                iou=config_number("previews", "detector", "iou", maximum=1),
                max_det=config_integer("previews", "detector", "max_detections"),
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
    "DocLayNetPageDetector",
    "PageAssessment",
    "PreviewModelError",
    "qualifying_layout_classes",
    "configure_detector_environment",
]
