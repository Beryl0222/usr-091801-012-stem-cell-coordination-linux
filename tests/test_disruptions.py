"""扰动处理：航班延误的优先级方案、体检异常与撤回后的替补。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from coordination import TimeWindow

from tests.helpers import CST, PATIENT_JIA_HLA, T0, UTC, World


class DisruptionTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.enroll_donors()
        self.patient = self.world.enroll_patient("患者甲")
        self.cases = self.world.cases
        self.d2 = self.world.donor_alias["志愿者乙"]
        self.d4 = self.world.donor_alias["志愿者丁"]
        self.d5 = self.world.donor_alias["志愿者戊"]
        self.cases.open_case("C1", self.patient, PATIENT_JIA_HLA, datetime(2026, 10, 1, tzinfo=UTC))
        self.cases.run_search("C1", self.world.donor_pool())
        self.world.consent.update_scope(
            self.d2, {"search", "recontact", "exam", "collection"}, actor="coordinator"
        )
        self.cases.assign_donor("C1", self.d2)

    def _plan_infusion_window(self, end=datetime(2026, 9, 23, 8, 0, tzinfo=CST)):
        self.world.board.plan(
            "C1",
            [("transplant_hospital", "infusion_started",
              TimeWindow(datetime(2026, 9, 22, 20, 0, tzinfo=CST), end))],
        )

    def test_flight_delay_alternatives_are_prioritized(self):
        self._plan_infusion_window()
        # 新预计到达 09-23 02:00（北京），仍在输注窗口内 → 改签可行
        alternatives = self.world.disruptions.handle_flight_delay(
            "C1", datetime(2026, 9, 23, 2, 0, tzinfo=CST)
        )
        self.assertEqual([a.rank for a in alternatives], [1, 2, 3])
        self.assertEqual(alternatives[0].action, "rebook_flight")
        self.assertTrue(alternatives[0].feasible)
        self.assertTrue(alternatives[2].feasible)  # 延长窗口始终可申请，优先级最低

    def test_flight_delay_beyond_window_marks_rebook_infeasible(self):
        self._plan_infusion_window(end=datetime(2026, 9, 22, 23, 0, tzinfo=CST))
        alternatives = self.world.disruptions.handle_flight_delay(
            "C1", datetime(2026, 9, 23, 6, 0, tzinfo=CST)
        )
        self.assertFalse(alternatives[0].feasible)
        event = self.world.audit.find("disruption.flight_delay")[0]
        self.assertEqual(event.basis["alternatives"][0]["action"], "rebook_flight")
        self.assertFalse(event.basis["alternatives"][0]["feasible"])

    def test_exam_anomaly_holds_donor_and_promotes_substitute(self):
        outcome = self.world.disruptions.handle_exam_anomaly("C1", self.d2, "血象异常")
        self.assertEqual(outcome.previous_donor, self.d2)
        self.assertEqual(outcome.new_donor, self.d4)  # 乙之后本案最高分可用者
        self.assertIsNone(self.world.commitments.active_case(self.d2))
        eligibility = self.cases.eligibility(self.d2)
        self.assertTrue(any(reason.startswith("hold:exam_anomaly") for reason in eligibility))
        substituted = self.world.audit.find("case.substituted")[0]
        self.assertEqual(substituted.basis["reason"], "exam_anomaly:血象异常")
        self.assertIn("algorithm_version", substituted.basis)

    def test_withdrawal_cools_donor_and_promotes_substitute(self):
        outcome = self.world.disruptions.handle_withdrawal("C1", self.d2)
        self.assertEqual(outcome.new_donor, self.d4)
        ok, why = self.world.consent.permits(self.d2, "recontact")
        self.assertFalse(ok)
        self.assertEqual(why, "cooling_off")

    def test_substitution_fails_when_no_eligible_candidate(self):
        for alias in (self.d2, self.d4, self.d5, self.world.donor_alias["志愿者丙"]):
            self.world.consent.set_cooling(alias, T0 + timedelta(days=365), "withdrawal")
        outcome = self.world.disruptions.handle_withdrawal("C1", self.d2)
        self.assertIsNone(outcome.new_donor)
        self.assertEqual(len(self.world.audit.find("case.substitution_failed")), 1)


if __name__ == "__main__":
    unittest.main()
