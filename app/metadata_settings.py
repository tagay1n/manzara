"""Shared task-specific metadata defaults and strictly validated run snapshots."""

from app.runtime_config import required_integer

METADATA_TASK_PREFIXES = {
    "library.metadata_extract": "",
    "maintenance.monocorpus_meta_evaluate": "evaluation_",
}
METADATA_SETTING_BOUNDS = {
    "response_timeout_seconds": (1, 900),
    "first_pages": (0, None),
    "last_pages": (0, None),
}


def metadata_setting_path(task_id, name):
    return ("metadata", METADATA_TASK_PREFIXES[task_id] + name)


def resolve_metadata_settings(config, task_id, *, options=None):
    """Take independent YAML defaults or explicit validated options for one run."""
    settings = {}
    for name, (minimum, maximum) in METADATA_SETTING_BOUNDS.items():
        value = getattr(options, "metadata_" + name, None)
        if value is None:
            value = required_integer(config, *metadata_setting_path(task_id, name), minimum=minimum, maximum=maximum)
        settings[name] = required_integer({name: value}, name, minimum=minimum, maximum=maximum)
    if settings["first_pages"] + settings["last_pages"] == 0:
        raise ValueError("Select at least one first or last page")
    return settings
