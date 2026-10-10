"""Compose flow-owned registrations for interactive and scheduled execution."""

from functools import partial
from importlib import import_module

from app.modules.library.collection_tasks import collection_task_definitions
from app.modules.library.tasks import library_task_definitions
from app.modules.maintenance.tasks import maintenance_task_definitions
from app.task_runtime.contracts import RunContext, TaskDescriptor


def _execute(module_name: str, context: RunContext) -> dict:
    return import_module(module_name).execute(context)


def build_descriptors(task_ids: tuple[str, ...] | None = None) -> list[TaskDescriptor]:
    """Default to interactive tasks; explicit IDs select ordered batch stages."""
    definitions = [*library_task_definitions(), *collection_task_definitions(),
                   *maintenance_task_definitions()]
    registrations = {item.task_id: item for item in definitions}
    if len(registrations) != len(definitions):
        raise ValueError("Duplicate task registration")
    if task_ids is None:
        selected = [item for item in definitions if item.interactive]
    else:
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("Duplicate selected task ID")
        unknown = set(task_ids) - registrations.keys()
        if unknown:
            raise ValueError(f"Unknown task IDs: {', '.join(sorted(unknown))}")
        selected = [registrations[task_id] for task_id in task_ids]
    return [TaskDescriptor(
        task_id=item.task_id, title=item.title, group=item.group, group_id=item.group_id,
        execute=partial(_execute, item.handler_module),
        requires_full_inventory=item.requires_full_inventory,
    ) for item in selected]
