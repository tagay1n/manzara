"""Settings controls and keyboard interaction; persistence belongs to the terminal."""

from prompt_toolkit.filters import Condition
from prompt_toolkit.layout.containers import ConditionalContainer, DynamicContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Button, Label, TextArea

from app.cli.settings import fields_for_task
from app.task_runtime.logging import redact


class SettingsForm:
    def __init__(self, *, busy, columns, has_focus, focus, invalidate, save, cancel):
        self._busy = busy
        self._columns = columns
        self._has_focus = has_focus
        self._focus = focus
        self._invalidate = invalidate
        self._save = save
        self._cancel = cancel
        self.fields = ()
        self.controls = {}
        self.error = ""
        self._body = HSplit([])
        self.save_button = Button("Save defaults", handler=save, width=16)
        self.cancel_button = Button("Cancel", handler=cancel, width=8)
        self.container = DynamicContainer(lambda: self._body)

    def open(self, task, options, config_path):
        self.fields = fields_for_task(task)
        self.controls = {}
        rows = []
        for field in self.fields:
            value = getattr(options, field.option)
            control = TextArea(text=str(value) if value is not None else "", height=1, multiline=False,
                               width=Dimension(min=8, preferred=12, max=18), style="class:setting",
                               read_only=Condition(self._busy))
            control.buffer.on_text_changed += self._changed
            self.controls[field.option] = control
            rows.append(DynamicContainer(lambda field=field, control=control:
                HSplit([Label(field.label), control]) if self._columns() < 55
                else VSplit([Label(field.label, width=29), control], padding=2)))
        self._body = HSplit([
            Label(f"Settings · {task.title}"),
            Label("Saved defaults · apply to future runs and survive restarts"),
            Window(FormattedTextControl(lambda: [("class:disabled", "Config: " + redact(str(config_path)))]),
                   height=Dimension(min=1, max=2), wrap_lines=True),
            *rows,
            Window(FormattedTextControl(self._hint), height=Dimension(min=1, max=3), wrap_lines=True),
            ConditionalContainer(Window(FormattedTextControl(lambda: [("class:warning", self.error)]),
                                        height=Dimension(min=1, max=3), wrap_lines=True),
                                 filter=Condition(lambda: bool(self.error))),
            VSplit([self.save_button, self.cancel_button] if self.fields else [self.cancel_button], padding=2),
        ])
        self.error = ""

    def targets(self):
        return list(self.controls.values()) + ([self.save_button] if self.fields else []) + [self.cancel_button]

    def _hint(self):
        if self._busy():
            return [("class:disabled", "Saving defaults…")]
        for field in self.fields:
            if self._has_focus(self.controls[field.option]):
                return [("class:disabled", field.hint)]
        return [("class:disabled", "This task requires the complete inventory; no editable run defaults."
                 if not self.fields else "Save defaults writes the config file. Cancel leaves it unchanged.")]

    def _changed(self, _buffer):
        self.error = ""
        self._invalidate()

    def move_focus(self, step):
        if self._busy():
            return
        targets = self.targets()
        current = next((i for i, target in enumerate(targets) if self._has_focus(target)), 0)
        self._focus(targets[(current + step) % len(targets)])

    def enter(self):
        if self._busy():
            return
        if self._has_focus(self.cancel_button):
            self._cancel()
        elif self._has_focus(self.save_button):
            self._save()
        else:
            self.move_focus(1)

    def adjust(self, step):
        if self._busy():
            return
        for field in self.fields:
            control = self.controls[field.option]
            if self._has_focus(control):
                try:
                    value = field.parse(control.text)
                except ValueError:
                    return
                value = (value if value is not None else 0) + step
                if field.allow_blank and value < field.minimum:
                    control.text = ""
                else:
                    control.text = str(max(field.minimum, min(value, field.maximum) if field.maximum is not None else value))
                return

    def values(self):
        values = {}
        for field in self.fields:
            self._focus(self.controls[field.option])
            values[field.option] = field.parse(self.controls[field.option].text)
        return values
