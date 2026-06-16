"""Explicit task outcomes for durable queue dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TaskState = Literal["done", "skipped", "failed", "queued"]
DEFAULT_TASK_RETRY_DELAY_SECONDS = 300.0
MAX_TRANSIENT_TASK_ATTEMPTS = 2
MAX_INFRA_UNAVAILABLE_ATTEMPTS = 8
INFRA_UNAVAILABLE_MARKER = "infrastructure unavailable: "


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    state: TaskState
    reason: str | None = None
    retry_delay_seconds: float | None = None
    retry_limit: int | None = MAX_TRANSIENT_TASK_ATTEMPTS


class TaskControl(RuntimeError):
    """Abort normal handler flow with an explicit queue outcome."""

    def __init__(self, outcome: TaskOutcome) -> None:
        self.outcome = outcome
        super().__init__(outcome.reason or outcome.state)


class SkipWork(TaskControl):
    """Policy/precondition skip: mark event skipped, not failed."""

    def __init__(self, reason: str) -> None:
        super().__init__(TaskOutcome("skipped", reason))


class PermanentTaskError(TaskControl):
    """Non-retryable task failure: mark event failed."""

    def __init__(self, reason: str) -> None:
        super().__init__(TaskOutcome("failed", reason))


class TransientTaskError(TaskControl):
    """Retryable task failure: requeue until retry budget is exhausted."""

    def __init__(self, reason: str, *, retry_delay_seconds: float | None = None) -> None:
        super().__init__(TaskOutcome("queued", reason, retry_delay_seconds, MAX_TRANSIENT_TASK_ATTEMPTS))


class DeferredTask(TaskControl):
    """Policy wait: requeue without consuming the transient failure budget."""

    def __init__(self, reason: str, *, retry_delay_seconds: float) -> None:
        super().__init__(TaskOutcome("queued", reason, retry_delay_seconds, retry_limit=None))


class InfrastructureUnavailable(TaskControl):
    """A required dependency (e.g. the auth broker) is unreachable. Retry a
    bounded number of times, then dead-letter — never spin forever in silence
    the way an uncapped DeferredTask did. The reconciler re-queues a fresh
    delivery once the dependency recovers, so dead-lettering loses nothing."""

    def __init__(
        self,
        reason: str,
        *,
        retry_delay_seconds: float,
        retry_limit: int = MAX_INFRA_UNAVAILABLE_ATTEMPTS,
    ) -> None:
        super().__init__(
            TaskOutcome(
                "queued",
                f"{INFRA_UNAVAILABLE_MARKER}{reason}",
                retry_delay_seconds,
                retry_limit=retry_limit,
            )
        )


__all__ = [
    "DEFAULT_TASK_RETRY_DELAY_SECONDS",
    "DeferredTask",
    "INFRA_UNAVAILABLE_MARKER",
    "InfrastructureUnavailable",
    "MAX_INFRA_UNAVAILABLE_ATTEMPTS",
    "MAX_TRANSIENT_TASK_ATTEMPTS",
    "PermanentTaskError",
    "SkipWork",
    "TaskControl",
    "TaskOutcome",
    "TaskState",
    "TransientTaskError",
]
