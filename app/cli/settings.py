"""Task-focused numeric settings backed by the selected private YAML."""

from dataclasses import dataclass

from app.task_settings import TASK_SETTINGS
from app.runtime_config import required_value


@dataclass(frozen=True)
class SettingField:
    option: str
    path: tuple[str, ...]
    label: str
    hint: str
    minimum: int = 1
    maximum: int | None = None
    allow_blank: bool = False

    def parse(self, text: str) -> int | None:
        value = text.strip()
        if not value and self.allow_blank:
            return None
        if not value or not value.isascii() or not value.isdigit():
            raise ValueError(f"{self.label}: enter a whole number" + (" or leave blank" if self.allow_blank else ""))
        number = int(value)
        if number < self.minimum or (self.maximum is not None and number > self.maximum):
            bounds = f"{self.minimum}–{self.maximum}" if self.maximum is not None else f"{self.minimum} or greater"
            raise ValueError(f"{self.label}: enter {bounds}")
        return number


def fields_for_task(task) -> tuple[SettingField, ...]:
    fields = () if task.requires_full_inventory else (
        SettingField("limit", ("terminal", "task_limits", task.task_id), "Candidate limit",
                     "Blank means unlimited. Save applies this task's default to the next run, replacing any launch override.", allow_blank=True),
    )
    fields += tuple(SettingField(field.option, field.path, field.label, field.hint,
                                 minimum=field.minimum, maximum=field.maximum)
                    for field in TASK_SETTINGS.get(task.task_id, ()))
    return fields


def configured_task_limit(config: dict, task_id: str) -> int | None:
    limits = required_value(config, "terminal", "task_limits")
    if not isinstance(limits, dict):
        raise ValueError("terminal.task_limits must be a mapping of task IDs to positive integers or null")
    value = limits.get(task_id)
    if value is not None and (type(value) is not int or value < 1):
        raise ValueError(f"Invalid saved candidate limit for {task_id}")
    return value
