"""Opt-in run pacing policy and pure adaptive state transitions."""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from app.runtime_config import config_integer, config_value

PacingOutcome = Literal["success", "quota", "service", "transient", "neutral"]


@dataclass(frozen=True)
class GeminiPacingPolicy:
    scope_id: str
    intervals_seconds: tuple[int, ...]
    cooldowns_seconds: tuple[int, ...]
    quota_failures_to_pause: int
    successes_to_recover: int

    @classmethod
    def from_config(cls, scope_id: str):
        values = {}
        for field in ("intervals_seconds", "cooldowns_seconds"):
            sequence = config_value("gemini", "personality_pacing", field)
            if (not isinstance(sequence, list) or len(sequence) < 2
                    or any(type(value) is not int or value <= 0 for value in sequence)
                    or any(a >= b for a, b in zip(sequence, sequence[1:]))):
                raise ValueError(f"gemini.personality_pacing.{field} must be increasing positive integers")
            values[field] = tuple(sequence)
        return cls(scope_id=scope_id, **values,
                   quota_failures_to_pause=config_integer("gemini", "personality_pacing", "quota_failures_to_pause"),
                   successes_to_recover=config_integer("gemini", "personality_pacing", "successes_to_recover"))


@dataclass(frozen=True)
class GeminiPacingState:
    mode: str = "normal"
    level: int = 0
    success_streak: int = 0
    quota_streak: int = 0
    cooldown_level: int = 0
    epoch: int = 0
    last_start_at: str | None = None
    next_start_at: str | None = None
    cooldown_until: str | None = None


def pacing_snapshot(policy: GeminiPacingPolicy, state: GeminiPacingState) -> dict:
    return {"state": asdict(state), "interval_seconds": policy.intervals_seconds[state.level]}


def _pause(policy, state, now, *, escalate):
    level = min(state.cooldown_level + int(escalate), len(policy.cooldowns_seconds) - 1)
    return replace(
        state, mode="cooldown", cooldown_level=level, epoch=state.epoch + 1,
        success_streak=0, quota_streak=0,
        cooldown_until=(now + timedelta(seconds=policy.cooldowns_seconds[level])).isoformat(),
    )


def advance_pacing(
    policy: GeminiPacingPolicy, state: GeminiPacingState,
    outcome: PacingOutcome, now: datetime,
) -> GeminiPacingState:
    """Change pacing on provider outcomes, independently of item content truth."""
    if outcome == "quota":
        updated = replace(
            state, level=min(state.level + 1, len(policy.intervals_seconds) - 1),
            success_streak=0, quota_streak=state.quota_streak + 1,
        )
        if state.mode in {"probe", "recovery"} or updated.quota_streak >= policy.quota_failures_to_pause:
            updated = _pause(policy, updated, now, escalate=state.mode != "normal")
    elif outcome == "success":
        if state.mode == "probe":
            updated = replace(state, mode="recovery", level=max(1, state.level),
                              success_streak=0, quota_streak=0, cooldown_until=None)
        else:
            streak = state.success_streak + 1 if state.level else 0
            level = state.level
            if streak >= policy.successes_to_recover:
                level -= 1
                streak = 0
            updated = replace(state, level=level, success_streak=streak, quota_streak=0)
            if level == 0:
                updated = replace(updated, mode="normal", cooldown_level=0, cooldown_until=None)
    else:
        # Model availability does not invalidate evidence of quota recovery.
        updated = state if outcome == "service" else replace(state, success_streak=0)
        if state.mode == "probe" and outcome in {"service", "transient"}:
            updated = _pause(policy, updated, now, escalate=True)
    if updated.last_start_at:
        next_start = datetime.fromisoformat(updated.last_start_at) + timedelta(
            seconds=policy.intervals_seconds[updated.level]
        )
        updated = replace(updated, next_start_at=next_start.isoformat())
    return updated
