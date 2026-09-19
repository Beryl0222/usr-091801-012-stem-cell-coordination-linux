"""个案流程：排他承诺、联络幂等、冷静期确认与跨案隔离。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from coordination import CaseStateError, ContactBlocked, DonorConflict

from tests.helpers import PATIENT_JIA_HLA, PATIENT_YI_HLA, T0, World

UTC = timezone.utc


class CaseFlowTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.enroll_donors()
        self.patient1 = self.world.enroll_patient("患者甲")
        self.patient2 = self.world.enroll_patient("患者乙")
        self.cases = self.world.cases
        self.d2 = self.world.donor_alias["志愿者乙"]
        self.d4 = self.world.donor_alias["志愿者丁"]
        self.cases.open_case("C1", self.patient1, PATIENT_JIA_HLA, datetime(2026, 10, 1, tzinfo=UTC))
        self.cases.open_case("C2", self.patient2, PATIENT_YI_HLA, datetime(2026, 10, 5, tzinfo=UTC))
        self.cases.run_search("C1", self.world.donor_pool())

    def test_contact_retry_is_idempotent_and_cooling_blocks_new_round(self):
        self.assertTrue(self.cases.contact_donor("C1", self.d2, round_no=1))
        # 同一轮重试：不重复通知
        self.assertFalse(self.cases.contact_donor("C1", self.d2, round_no=1))
        self.assertEqual(self.world.notifier.sent_count, 1)
        # 冷静期内新一轮联络被拦截
        with self.assertRaises(ContactBlocked):
            self.cases.contact_donor("C1", self.d2, round_no=2)
        self.assertEqual(self.world.notifier.sent_count, 1)
        # 冷静期过后可以再次联络
        self.world.clock.advance(timedelta(hours=12))
        self.assertTrue(self.cases.contact_donor("C1", self.d2, round_no=2))
        self.assertEqual(self.world.notifier.sent_count, 2)

    def test_confirm_requires_collection_cooling(self):
        self.world.consent.update_scope(
            self.d2, {"search", "recontact", "exam", "collection"}, actor="coordinator"
        )
        self.cases.assign_donor("C1", self.d2)
        with self.assertRaises(CaseStateError):
            self.cases.confirm_donation("C1")
        self.world.clock.advance(timedelta(hours=48))
        self.cases.confirm_donation("C1")
        self.assertEqual(self.cases.get("C1").status, "confirmed")

    def test_donor_cannot_enter_two_cases_and_error_hides_other_case(self):
        self.world.consent.update_scope(
            self.d2, {"search", "recontact", "exam", "collection"}, actor="coordinator"
        )
        self.cases.assign_donor("C1", self.d2)
        self.cases.run_search("C2", self.world.donor_pool())
        # C2 的候选记录保留排除理由，但不含对方个案编号
        excluded = {a.donor_alias: a for a in self.cases.get("C2").assessments if a.excluded}
        self.assertIn(self.d2, excluded)
        self.assertIn("committed_elsewhere", excluded[self.d2].reasons)
        with self.assertRaises(CaseStateError):
            self.cases.assign_donor("C2", self.d2)
        # 直接占用承诺时，异常信息不泄露对方个案
        with self.assertRaises(DonorConflict) as ctx:
            self.world.commitments.reserve(self.d2, "C2")
        self.assertNotIn("C1", str(ctx.exception))

    def test_case_view_has_no_cross_case_leakage(self):
        self.world.consent.update_scope(
            self.d2, {"search", "recontact", "exam", "collection"}, actor="coordinator"
        )
        self.cases.assign_donor("C1", self.d2)
        self.cases.run_search("C2", self.world.donor_pool())
        view_c2 = json.dumps(self.cases.case_view("C2"), ensure_ascii=False)
        self.assertNotIn('"C1"', view_c2)
        self.assertNotIn(self.patient1, view_c2)
        # 但本案自己的数据与别名可见
        self.assertIn(self.patient2, view_c2)

    def test_assign_requires_eligible_assessment(self):
        with self.assertRaises(CaseStateError):
            self.cases.assign_donor("C1", "D-notacandidate")
        d1 = self.world.donor_alias["志愿者甲"]  # 仅同意检索，未同意联络
        with self.assertRaises(CaseStateError):
            self.cases.assign_donor("C1", d1)

    def test_release_assignment_frees_commitment(self):
        self.world.consent.update_scope(
            self.d2, {"search", "recontact", "exam", "collection"}, actor="coordinator"
        )
        self.cases.assign_donor("C1", self.d2)
        released = self.cases.release_assignment("C1", "donor_withdrawal")
        self.assertEqual(released, self.d2)
        self.assertIsNone(self.world.commitments.active_case(self.d2))
        self.assertEqual(self.cases.get("C1").status, "candidates_ready")


if __name__ == "__main__":
    unittest.main()
