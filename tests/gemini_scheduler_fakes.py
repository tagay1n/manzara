"""Adapter for workflow fakes; scheduler behavior has separate SQLite tests."""

from app.gemini_runtime import (
    GeminiAllKeysExhaustedError,
    GeminiServerPauseError,
    GeminiQuotaCooldownError,
)


class ScheduledManagerFake:
    max_quota_rotations_per_model = 3

    def run_with_available_model(self, *, models, call, run_id=None, **kwargs):
        paused = getattr(self, "_scheduler_paused", {})
        available = [m for m in models if m not in paused]
        if not available:
            for error in paused.values():
                if error.pause_until is not None:
                    self._sleep_until(error.pause_until)
            paused.clear()
            available = list(models)
        self._scheduler_paused = paused
        retries = []
        for model in available:
            try:
                result = self.run_with_key(
                    model_name=model,
                    call=lambda key, lease: call(model, key, lease),
                    run_id=run_id,
                    max_attempts=1,
                )
            except GeminiAllKeysExhaustedError as error:
                if getattr(error, "retry_at", None):
                    retries.append(error.retry_at)
                continue
            except Exception as error:
                error.model_name = model
                if isinstance(error, GeminiServerPauseError):
                    paused[model] = error
                raise
            return model, result
        if retries:
            raise GeminiQuotaCooldownError(
                "No ready fake models", retry_at=min(retries)
            )
        raise GeminiAllKeysExhaustedError("No available fake models")
