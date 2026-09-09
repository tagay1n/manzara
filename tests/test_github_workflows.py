"""Contract tests for repository-owned GitHub Actions workflows."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_nightly_google_export_workflow_contract() -> None:
    workflow_path = REPO_ROOT / ".github" / "workflows" / "nightly-google-export.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert workflow["name"] == "Nightly Google Sheets & Drive Export"
    triggers = workflow["on"]
    assert triggers["schedule"] == [{"cron": "0 0 * * *"}]
    assert "workflow_dispatch" in triggers
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "nightly-google-catalog-export",
        "cancel-in-progress": "false",
    }

    job = workflow["jobs"]["export"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == "60"
    assert job["env"] == {
        "MANZARA_DATABASE_URL": "${{ secrets.MANZARA_DATABASE_URL }}",
        "MANZARA_DB_POOL_SIZE": "1",
        "MANZARA_DB_SCHEMA": "monocorpus",
    }

    steps = job["steps"]
    assert steps[0]["uses"] == "actions/checkout@v7"
    assert steps[1]["uses"] == "actions/setup-python@v7"
    assert steps[1]["with"]["python-version"] == "3.12"
    by_name = {step.get("name"): step for step in steps}
    credentials_step = by_name["Prepare Google OAuth credentials"]
    assert credentials_step["env"] == {
        "GOOGLE_OAUTH_TOKEN_JSON_BASE64": (
            "${{ secrets.GOOGLE_OAUTH_TOKEN_JSON_BASE64 }}"
        ),
        "MANZARA_ARTIFACTS_ROOT": "${{ runner.temp }}/manzara",
    }
    assert 'case "$MANZARA_DATABASE_URL" in' in credentials_step["run"]
    assert (
        "postgres://*|postgresql://*|postgresql+psycopg://*|postgresql+psycopg2://*)"
        in credentials_step["run"]
    )
    assert "MANZARA_DATABASE_URL must start with postgres://" in credentials_step["run"]
    export_step = by_name["Export catalog to Google Drive and Sheets"]
    assert export_step["env"] == {
        "MANZARA_ARTIFACTS_ROOT": "${{ runner.temp }}/manzara"
    }
    assert "python -m app.modules.maintenance.runtime.dump_state" in export_step["run"]
