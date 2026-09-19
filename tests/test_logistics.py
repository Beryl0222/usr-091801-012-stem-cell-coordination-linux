"""采集排程与里程碑：幂等排程、归属校验、跨时区归一。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from coordination import CollectionConflict, MilestoneError, TimeWindow

from tests.helpers import CST, EDT, T0, UTC, World


def window(start, end, tz=CST):
    return TimeWindow(start.replace(tzinfo=tz), end.replace(tzinfo=tz))


class CollectionSchedulerTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.scheduler = self.world.scheduler
        self.window = window(datetime(2026, 9, 22, 8, 0), datetime(2026, 9, 22, 16, 0))

    def test_retry_returns_same_plan(self):
        plan = self.scheduler.schedule("C1", "D-a", self.window)
        again = self.scheduler.schedule("C1", "D-a", self.window)
        self.assertEqual(plan.plan_id, again.plan_id)
        self.assertEqual(len(self.world.audit.find("collection.scheduled")), 1)

    def test_same_case_other_donor_conflicts(self):
        self.scheduler.schedule("C1", "D-a", self.window)
        with self.assertRaises(CollectionConflict):
            self.scheduler.schedule("C1", "D-b", self.window)

    def test_same_donor_other_case_conflicts(self):
        self.scheduler.schedule("C1", "D-a", self.window)
        with self.assertRaises(CollectionConflict):
            self.scheduler.schedule("C2", "D-a", self.window)

    def test_cancel_releases_donor(self):
        self.scheduler.schedule("C1", "D-a", self.window)
        self.scheduler.cancel("C1", "donor_withdrawal")
        self.assertIsNone(self.scheduler.plan_for_donor("D-a"))
        self.scheduler.schedule("C2", "D-a", self.window)  # 不再冲突

    def test_naive_window_rejected(self):
        with self.assertRaises(ValueError):
            TimeWindow(datetime(2026, 9, 22, 8, 0), datetime(2026, 9, 22, 16, 0))


class MilestoneBoardTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.board = self.world.board
        self.board.plan(
            "C1",
            [
                ("collection_hospital", "collection_completed",
                 window(datetime(2026, 9, 22, 6, 0), datetime(2026, 9, 22, 12, 0))),
                ("transport", "arrived",
                 window(datetime(2026, 9, 22, 18, 0), datetime(2026, 9, 22, 23, 0))),
            ],
        )

    def test_ownership_is_enforced(self):
        with self.assertRaises(MilestoneError):
            self.board.plan("C2", [("transport", "collection_completed",
                                    window(datetime(2026, 9, 22, 6, 0), datetime(2026, 9, 22, 7, 0)))])
        with self.assertRaises(MilestoneError):
            self.board.report("C1", "transport", "collection_completed",
                              datetime(2026, 9, 22, 8, 0, tzinfo=CST))

    def test_report_within_window_is_met(self):
        milestone = self.board.report(
            "C1", "collection_hospital", "collection_completed",
            datetime(2026, 9, 22, 10, 0, tzinfo=CST),
        )
        self.assertEqual(milestone.status, "met")

    def test_cross_timezone_report_normalizes_to_utc(self):
        # 美东时间 2026-09-22 10:30（UTC-4）= UTC 14:30 = 北京 22:30，在窗口内
        milestone = self.board.report(
            "C1", "transport", "arrived", datetime(2026, 9, 22, 10, 30, tzinfo=EDT)
        )
        self.assertEqual(milestone.status, "met")
        self.assertEqual(milestone.reported_at, datetime(2026, 9, 22, 14, 30, tzinfo=UTC))

    def test_late_report_is_breached(self):
        milestone = self.board.report(
            "C1", "transport", "arrived", datetime(2026, 9, 23, 2, 0, tzinfo=CST)
        )
        self.assertEqual(milestone.status, "breached")
        self.assertEqual(self.board.breaches("C1"), [milestone])

    def test_duplicate_report_rejected(self):
        at = datetime(2026, 9, 22, 10, 0, tzinfo=CST)
        self.board.report("C1", "collection_hospital", "collection_completed", at)
        with self.assertRaises(MilestoneError):
            self.board.report("C1", "collection_hospital", "collection_completed", at)

    def test_adjust_window_recomputes_status_and_audits(self):
        self.board.report(
            "C1", "transport", "arrived", datetime(2026, 9, 23, 2, 0, tzinfo=CST)
        )
        adjusted = self.board.adjust_window(
            "C1", "arrived",
            window(datetime(2026, 9, 22, 18, 0), datetime(2026, 9, 23, 4, 0)),
            "航班延误改签",
        )
        self.assertEqual(adjusted.status, "met")
        events = self.world.audit.find("milestone.window_adjusted")
        self.assertEqual(events[0].basis["reason"], "航班延误改签")


if __name__ == "__main__":
    unittest.main()
