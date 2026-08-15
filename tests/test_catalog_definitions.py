from app.constants import PANEL_DEFS
from app.modules.maintenance.workflow import (
    maintenance_backup_full_workflow_bundle,
    maintenance_backup_incr_workflow_bundle,
)


def test_catalog_definitions_use_requested_names() -> None:
    panels = {item["panel_id"]: item for item in PANEL_DEFS}

    assert panels["shayan"]["title"] == "Shayan"
    assert panels["maintenance"]["title"] == "Yandex disk"
    assert panels["backup"]["title"] == "Backup"


def test_backup_workflows_belong_to_backup_catalog() -> None:
    full = maintenance_backup_full_workflow_bundle()["workflow"]
    incremental = maintenance_backup_incr_workflow_bundle()["workflow"]

    assert full["panel_id"] == "backup"
    assert incremental["panel_id"] == "backup"
