from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gridbot.state_store import StateStore


class GetRiskEventsSinceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self._tmpdir.name) / "test.db", "UTC")

    def tearDown(self) -> None:
        self.store.close()
        self._tmpdir.cleanup()

    def _insert_event(self, event_type: str, created_at: str) -> None:
        self.store.conn.execute(
            "INSERT INTO risk_events(event_type, symbol, details, created_at) VALUES (?, ?, ?, ?)",
            (event_type, "BTCUSDT", "{}", created_at),
        )
        self.store.conn.commit()

    def test_excludes_events_before_cutoff(self) -> None:
        self._insert_event("old_event", "2026-08-14T10:00:00+00:00")
        self._insert_event("recent_event", "2026-08-17T10:00:00+00:00")

        events = self.store.get_risk_events_since("2026-08-16T00:00:00+00:00")

        event_types = [e.event_type for e in events]
        self.assertEqual(event_types, ["recent_event"])

    def test_includes_event_exactly_at_cutoff(self) -> None:
        self._insert_event("boundary_event", "2026-08-16T00:00:00+00:00")

        events = self.store.get_risk_events_since("2026-08-16T00:00:00+00:00")

        self.assertEqual([e.event_type for e in events], ["boundary_event"])

    def test_returns_newest_first(self) -> None:
        self._insert_event("first", "2026-08-16T01:00:00+00:00")
        self._insert_event("second", "2026-08-16T02:00:00+00:00")
        self._insert_event("third", "2026-08-16T03:00:00+00:00")

        events = self.store.get_risk_events_since("2026-08-16T00:00:00+00:00")

        self.assertEqual([e.event_type for e in events], ["third", "second", "first"])

    def test_respects_limit(self) -> None:
        for i in range(5):
            self._insert_event(f"event_{i}", f"2026-08-16T0{i}:00:00+00:00")

        events = self.store.get_risk_events_since("2026-08-16T00:00:00+00:00", limit=2)

        self.assertEqual(len(events), 2)

    def test_no_events_in_window_returns_empty_list(self) -> None:
        self._insert_event("old_event", "2026-08-01T00:00:00+00:00")

        events = self.store.get_risk_events_since("2026-08-16T00:00:00+00:00")

        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
