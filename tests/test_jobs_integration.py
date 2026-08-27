from __future__ import annotations

from src.jobs import reconcile_calendar


class EngineSpy:
    def __init__(self) -> None:
        self.detected = False

    async def detect_conflicts(self, start, end):
        self.detected = True
        return []

    async def resolve_conflicts(self, start, end):
        raise AssertionError("reconciliation must inspect conflicts before rescheduling")


async def test_reconcile_uses_conflict_detection_not_blanket_reschedule():
    engine = EngineSpy()
    await reconcile_calendar(engine)
    assert engine.detected
