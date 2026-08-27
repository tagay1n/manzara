"""Startup seed registry builders for panels and tasks."""

from __future__ import annotations

from typing import Any, Callable

from app.constants import PANEL_DEFS
from app.contracts import JSONDict, StartupSeedRegistry
from app.modules.maintenance.tasks import maintenance_task_definitions
from app.modules.library.tasks import library_task_definitions
from app.modules.library.collection_tasks import collection_task_definitions
from app.modules.shayan.tasks import shayan_task_definitions
from app.settings import Settings


def build_startup_seed_registry(
    settings: Settings | Any,
    *,
    panel_defs: list[JSONDict] | None = None,
    shayan_task_definitions: Callable[[Any], list[JSONDict]] = shayan_task_definitions,
    maintenance_task_definitions: Callable[
        [Any], list[JSONDict]
    ] = maintenance_task_definitions,
    library_task_definitions: Callable[[], list[JSONDict]] = library_task_definitions,
    collection_task_definitions: Callable[
        [], list[JSONDict]
    ] = collection_task_definitions,
) -> StartupSeedRegistry:
    """Build startup seed payloads with injectable task factories."""
    selected_panel_defs = [dict(item) for item in (panel_defs or PANEL_DEFS)]
    task_defs = [
        *shayan_task_definitions(settings.shayan),
        *maintenance_task_definitions(settings.maintenance),
        *library_task_definitions(),
        *collection_task_definitions(),
    ]
    return StartupSeedRegistry(
        panel_defs=selected_panel_defs,
        task_defs=task_defs,
    )
