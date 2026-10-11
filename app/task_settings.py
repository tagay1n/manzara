"""Declarative numeric AI settings and strictly validated task snapshots."""

from dataclasses import dataclass

from app.runtime_config import required_integer


@dataclass(frozen=True)
class TaskSetting:
    name: str
    option: str
    path: tuple[str, ...]
    label: str
    hint: str
    minimum: int
    maximum: int | None = None


_TIMEOUT_HINT = "1–900 seconds. Covers Gemini's full response; preparation, admission and cleanup are outside it."
_METADATA_FIELDS = (
    ("response_timeout_seconds", "Response timeout (seconds)", _TIMEOUT_HINT, 1, 900),
    ("first_pages", "First pages", "Pages from the start of PDF/DjVu evidence. 0 skips this end; overlapping pages appear once.", 0, None),
    ("last_pages", "Last pages", "Pages from the end of PDF/DjVu evidence. 0 skips this end; select at least one page overall.", 0, None),
)
TASK_SETTINGS = {
    task_id: tuple(TaskSetting(name, "metadata_" + name, ("metadata", prefix + name), label, hint, minimum, maximum)
                   for name, label, hint, minimum, maximum in _METADATA_FIELDS)
    for task_id, prefix in {
        "library.metadata_extract": "",
        "maintenance.monocorpus_meta_evaluate": "evaluation_",
    }.items()
}
TASK_SETTINGS["library.normalize_personalities"] = (
    TaskSetting("response_timeout_seconds", "personality_response_timeout_seconds",
                ("gemini", "request", "personality_timeout_seconds"),
                "Response timeout (seconds)", _TIMEOUT_HINT, 1, 900),
)
OPTION_BOUNDS = {field.option: (field.minimum, field.maximum)
                 for fields in TASK_SETTINGS.values() for field in fields}


def validate_setting_options(options):
    for name, (minimum, maximum) in OPTION_BOUNDS.items():
        value = getattr(options, name)
        if value is not None:
            required_integer({name: value}, name, minimum=minimum, maximum=maximum)
    if options.metadata_first_pages == 0 and options.metadata_last_pages == 0:
        raise ValueError("Select at least one first or last page")


def validate_task_settings(task_id, options):
    allowed = {field.option for field in TASK_SETTINGS.get(task_id, ())}
    invalid = [name for name in OPTION_BOUNDS if getattr(options, name) is not None and name not in allowed]
    if invalid:
        raise ValueError(f"Settings do not apply to {task_id}: {', '.join(invalid)}")


def resolve_task_settings(config, task_id, *, options=None):
    values = {}
    for field in TASK_SETTINGS.get(task_id, ()):
        value = getattr(options, field.option, None)
        if value is None:
            value = required_integer(config, *field.path, minimum=field.minimum, maximum=field.maximum)
        values[field.name] = required_integer({field.name: value}, field.name,
                                              minimum=field.minimum, maximum=field.maximum)
    if "first_pages" in values and values["first_pages"] + values["last_pages"] == 0:
        raise ValueError("Select at least one first or last page")
    return values


def configured_task_options(config, task_id):
    values = resolve_task_settings(config, task_id)
    return {field.option: values[field.name] for field in TASK_SETTINGS.get(task_id, ())}
