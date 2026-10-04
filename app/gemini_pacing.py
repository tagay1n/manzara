"""Opt-in run pacing policy and pure adaptive state transitions."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

PacingOutcome = Literal["success", "quota", "transient", "neutral"]


@dataclass(frozen=True)
class GeminiPacingPolicy:
    scope_id: str
    intervals_seconds: tuple[int, ...] = (5, 10, 20, 40, 60)
    cooldowns_seconds: tuple[int, ...] = (60, 120, 240, 480, 600)
    quota_failures_to_pause: int = 3
    successes_to_recover: int = 5


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
        updated = replace(state, success_streak=0)
        if state.mode == "probe" and outcome == "transient":
            updated = _pause(policy, updated, now, escalate=True)
    if updated.last_start_at:
        next_start = datetime.fromisoformat(updated.last_start_at) + timedelta(
            seconds=policy.intervals_seconds[updated.level]
        )
        updated = replace(updated, next_start_at=next_start.isoformat())
    return updated
