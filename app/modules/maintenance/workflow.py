"""Workflow definitions for Maintenance/Library flows."""

from __future__ import annotations

from typing import Any, Dict, List

from app.modules.maintenance.tasks import (
    LIBRARY_PERSONALITY_SUGGESTIONS_REFRESH_TASK_ID,
    LIBRARY_PUBLISHER_SUGGESTIONS_REFRESH_TASK_ID,
    MAINTENANCE_PGBACKREST_FULL_TASK_ID,
    MAINTENANCE_PGBACKREST_INCR_TASK_ID,
    MONOCORPUS_META_EVALUATE_TASK_ID,
)


MAINTENANCE_BACKUP_FULL_WORKFLOW_ID = "maintenance.pgbackrest_full_weekly"
MAINTENANCE_BACKUP_FULL_SCHEDULE_ID = "maintenance.pgbackrest_full_weekly.schedule"
MAINTENANCE_BACKUP_INCR_WORKFLOW_ID = "maintenance.pgbackrest_incr_3h"
MAINTENANCE_BACKUP_INCR_SCHEDULE_ID = "maintenance.pgbackrest_incr_3h.schedule"
LIBRARY_WORKFLOW_ID = "library.meta_evaluate"
LIBRARY_WORKFLOW_SCHEDULE_ID = "library.meta_evaluate.schedule"
LIBRARY_PERSONALITY_NORM_WORKFLOW_ID = "library.personality_normalization_refresh"
LIBRARY_PERSONALITY_NORM_SCHEDULE_ID = "library.personality_normalization_refresh.schedule"
LIBRARY_PUBLISHER_NORM_WORKFLOW_ID = "library.publisher_normalization_refresh"
LIBRARY_PUBLISHER_NORM_SCHEDULE_ID = "library.publisher_normalization_refresh.schedule"


def library_workflow_bundle() -> Dict[str, Any]:
    """Return workflow, steps, and schedule seed for monocorpus meta evaluate."""
    workflow = {
        "workflow_id": LIBRARY_WORKFLOW_ID,
        "panel_id": "library",
        "title": "Library",
        "description": "Run monocorpus metadata evaluation.",
        "enabled": 1,
    }

    steps: List[Dict[str, Any]] = [
        {
            "step_order": 1,
            "task_id": MONOCORPUS_META_EVALUATE_TASK_ID,
            "step_type": "task",
            "condition_json": {},
        },
    ]

    schedule = {
        "schedule_id": LIBRARY_WORKFLOW_SCHEDULE_ID,
        "workflow_id": LIBRARY_WORKFLOW_ID,
        "schedule_type": "weekly",
        "day_of_week": 7,
        "time_of_day": "04:00",
        "timezone": "UTC",
        "enabled": 0,
        "overlap_policy": "skip",
        "catchup_policy": "once",
    }

    return {
        "workflow": workflow,
        "steps": steps,
        "schedule": schedule,
    }


def maintenance_backup_full_workflow_bundle() -> Dict[str, Any]:
    """Return workflow bundle for weekly full pgBackRest backup."""
    workflow = {
        "workflow_id": MAINTENANCE_BACKUP_FULL_WORKFLOW_ID,
        "panel_id": "maintenance",
        "title": "Postgres full backup (weekly)",
        "description": "Run weekly full pgBackRest backup for monocorpus stanza.",
        "enabled": 1,
    }

    steps: List[Dict[str, Any]] = [
        {
            "step_order": 1,
            "task_id": MAINTENANCE_PGBACKREST_FULL_TASK_ID,
            "step_type": "task",
            "condition_json": {},
        },
    ]

    schedule = {
        "schedule_id": MAINTENANCE_BACKUP_FULL_SCHEDULE_ID,
        "workflow_id": MAINTENANCE_BACKUP_FULL_WORKFLOW_ID,
        "schedule_type": "weekly",
        "day_of_week": 7,
        "time_of_day": "02:00",
        "timezone": "UTC",
        "interval_minutes": None,
        "enabled": 1,
        "overlap_policy": "skip",
        "catchup_policy": "once",
    }

    return {
        "workflow": workflow,
        "steps": steps,
        "schedule": schedule,
    }


def maintenance_backup_incr_workflow_bundle() -> Dict[str, Any]:
    """Return workflow bundle for 12-hour incremental pgBackRest backup."""
    workflow = {
        "workflow_id": MAINTENANCE_BACKUP_INCR_WORKFLOW_ID,
        "panel_id": "maintenance",
        "title": "Postgres incremental backup (every 12h)",
        "description": "Run pgBackRest incremental backup every 12 hours.",
        "enabled": 1,
    }

    steps: List[Dict[str, Any]] = [
        {
            "step_order": 1,
            "task_id": MAINTENANCE_PGBACKREST_INCR_TASK_ID,
            "step_type": "task",
            "condition_json": {},
        },
    ]

    schedule = {
        "schedule_id": MAINTENANCE_BACKUP_INCR_SCHEDULE_ID,
        "workflow_id": MAINTENANCE_BACKUP_INCR_WORKFLOW_ID,
        "schedule_type": "interval",
        "day_of_week": 1,
        "time_of_day": "00:00",
        "timezone": "UTC",
        "interval_minutes": 720,
        "enabled": 1,
        "overlap_policy": "skip",
        "catchup_policy": "once",
    }

    return {
        "workflow": workflow,
        "steps": steps,
        "schedule": schedule,
    }


def library_personality_normalization_workflow_bundle() -> Dict[str, Any]:
    """Return workflow bundle for personality normalization suggestion refresh."""
    workflow = {
        "workflow_id": LIBRARY_PERSONALITY_NORM_WORKFLOW_ID,
        "panel_id": "library",
        "title": "Library personality normalization refresh",
        "description": "Regenerate alias suggestions for personality normalization.",
        "enabled": 1,
    }

    steps: List[Dict[str, Any]] = [
        {
            "step_order": 1,
            "task_id": LIBRARY_PERSONALITY_SUGGESTIONS_REFRESH_TASK_ID,
            "step_type": "task",
            "condition_json": {},
        },
    ]

    schedule = {
        "schedule_id": LIBRARY_PERSONALITY_NORM_SCHEDULE_ID,
        "workflow_id": LIBRARY_PERSONALITY_NORM_WORKFLOW_ID,
        "schedule_type": "weekly",
        "day_of_week": 6,
        "time_of_day": "05:00",
        "timezone": "UTC",
        "enabled": 0,
        "overlap_policy": "skip",
        "catchup_policy": "once",
    }

    return {
        "workflow": workflow,
        "steps": steps,
        "schedule": schedule,
    }


def library_publisher_normalization_workflow_bundle() -> Dict[str, Any]:
    """Return workflow bundle for publisher normalization suggestion refresh."""
    workflow = {
        "workflow_id": LIBRARY_PUBLISHER_NORM_WORKFLOW_ID,
        "panel_id": "library",
        "title": "Library publisher normalization refresh",
        "description": "Regenerate alias suggestions for publisher normalization.",
        "enabled": 1,
    }

    steps: List[Dict[str, Any]] = [
        {
            "step_order": 1,
            "task_id": LIBRARY_PUBLISHER_SUGGESTIONS_REFRESH_TASK_ID,
            "step_type": "task",
            "condition_json": {},
        },
    ]

    schedule = {
        "schedule_id": LIBRARY_PUBLISHER_NORM_SCHEDULE_ID,
        "workflow_id": LIBRARY_PUBLISHER_NORM_WORKFLOW_ID,
        "schedule_type": "weekly",
        "day_of_week": 6,
        "time_of_day": "05:30",
        "timezone": "UTC",
        "enabled": 0,
        "overlap_policy": "skip",
        "catchup_policy": "once",
    }

    return {
        "workflow": workflow,
        "steps": steps,
        "schedule": schedule,
    }
