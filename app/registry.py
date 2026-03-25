"""Startup seed registry builders for panels, tasks, and workflows."""

from __future__ import annotations

from typing import Any, Callable

from app.constants import PANEL_DEFS
from app.contracts import JSONDict, StartupSeedRegistry
from app.modules.maintenance.tasks import maintenance_task_definitions
from app.modules.maintenance.workflow import (
    library_personality_normalization_workflow_bundle,
    library_publisher_normalization_workflow_bundle,
    library_workflow_bundle,
    maintenance_backup_full_workflow_bundle,
    maintenance_backup_incr_workflow_bundle,
)
from app.modules.oscar.tasks import oscar_task_definitions
from app.modules.oscar.workflow import oscar_pipeline_workflow_bundle
from app.modules.shayan.tasks import shayan_task_definitions
from app.modules.shayan.workflow import shayan_workflow_bundle
from app.settings import Settings


def build_startup_seed_registry(
    settings: Settings | Any,
    *,
    panel_defs: list[JSONDict] | None = None,
    shayan_task_definitions: Callable[[Any], list[JSONDict]] = shayan_task_definitions,
    maintenance_task_definitions: Callable[[Any], list[JSONDict]] = maintenance_task_definitions,
    oscar_task_definitions: Callable[[Any], list[JSONDict]] = oscar_task_definitions,
    shayan_workflow_bundle: Callable[[Any], JSONDict] = shayan_workflow_bundle,
    maintenance_backup_full_workflow_bundle: Callable[[], JSONDict] = maintenance_backup_full_workflow_bundle,
    maintenance_backup_incr_workflow_bundle: Callable[[], JSONDict] = maintenance_backup_incr_workflow_bundle,
    library_workflow_bundle: Callable[[], JSONDict] = library_workflow_bundle,
    library_personality_normalization_workflow_bundle: Callable[
        [], JSONDict
    ] = library_personality_normalization_workflow_bundle,
    library_publisher_normalization_workflow_bundle: Callable[
        [], JSONDict
    ] = library_publisher_normalization_workflow_bundle,
    oscar_pipeline_workflow_bundle: Callable[[], JSONDict] = oscar_pipeline_workflow_bundle,
) -> StartupSeedRegistry:
    """Build startup seed payloads with injectable task/workflow factories."""
    selected_panel_defs = [dict(item) for item in (panel_defs or PANEL_DEFS)]
    task_defs = [
        *shayan_task_definitions(settings.shayan),
        *maintenance_task_definitions(settings.maintenance),
        *oscar_task_definitions(settings.oscar),
    ]
    workflow_bundles = [
        shayan_workflow_bundle(settings.shayan),
        maintenance_backup_full_workflow_bundle(),
        maintenance_backup_incr_workflow_bundle(),
        library_workflow_bundle(),
        library_personality_normalization_workflow_bundle(),
        library_publisher_normalization_workflow_bundle(),
        oscar_pipeline_workflow_bundle(),
    ]
    return StartupSeedRegistry(
        panel_defs=selected_panel_defs,
        task_defs=task_defs,
        workflow_bundles=workflow_bundles,
    )
