"""联调：同时推进两个患者流程，注入跨时区航班延误。

核对点：
- 重试不会重复通知、不会重复采集；
- 一名志愿者同时进入两个患者流程时不泄露、不重复采集；
- 撤回触发替补，体检/同意/冷静期以最新状态为准；
- 紧急解封双人批准并自动到期；
- 审计导出可逐条核对每次匹配、替补与解封的依据，且不含真实身份。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta

from coordination import ALGORITHM_VERSION, CaseStateError, ContactBlocked, TimeWindow, UnsealDenied

from tests.helpers import CST, EDT, PATIENT_JIA_HLA, PATIENT_YI_HLA, UTC, World


def cst(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CST)


class JointFlowTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.enroll_donors()
        self.patient1 = self.world.enroll_patient("患者甲")
        self.patient2 = self.world.enroll_patient("患者乙")
        w = self.world
        self.d1 = w.donor_alias["志愿者甲"]
        self.d2 = w.donor_alias["志愿者乙"]
        self.d3 = w.donor_alias["志愿者丙"]
        self.d4 = w.donor_alias["志愿者丁"]
        self.d5 = w.donor_alias["志愿者戊"]
        w.cases.open_case("C1", self.patient1, PATIENT_JIA_HLA, datetime(2026, 10, 1, tzinfo=UTC))
        w.cases.open_case("C2", self.patient2, PATIENT_YI_HLA, datetime(2026, 10, 5, tzinfo=UTC))

    def test_two_cases_with_delay_and_audit(self):
        w = self.world

        # ---- 个案一：检索、联络、锁定、确认 ----
        ranked_c1 = w.cases.run_search("C1", w.donor_pool())
        self.assertEqual(ranked_c1[0].donor_alias, self.d2)
        self.assertEqual((ranked_c1[0].score, ranked_c1[0].max_score), (10, 10))
        self.assertEqual(ranked_c1[0].algorithm_version, ALGORITHM_VERSION)
        excluded_c1 = {a.donor_alias: a for a in ranked_c1 if a.excluded}
        self.assertIn("scope_lacks:recontact", excluded_c1[self.d1].reasons)

        self.assertTrue(w.cases.contact_donor("C1", self.d2, round_no=1))
        self.assertFalse(w.cases.contact_donor("C1", self.d2, round_no=1))  # 重试不重复通知
        self.assertEqual(w.notifier.sent_count, 1)
        with self.assertRaises(ContactBlocked):
            w.cases.contact_donor("C1", self.d2, round_no=2)  # 联络冷静期

        w.clock.advance(timedelta(hours=12))
        w.consent.update_scope(self.d2, {"search", "recontact", "exam", "collection"}, actor="coordinator")
        w.cases.assign_donor("C1", self.d2)
        with self.assertRaises(CaseStateError):
            w.cases.confirm_donation("C1")  # 采集冷静期未满
        w.clock.advance(timedelta(hours=48))
        w.cases.confirm_donation("C1")

        # 采集排程：重试返回同一计划，不重复采集
        collection_window = TimeWindow(cst(2026, 9, 22, 8), cst(2026, 9, 22, 16))
        plan = w.scheduler.schedule("C1", self.d2, collection_window)
        retry = w.scheduler.schedule("C1", self.d2, collection_window)
        self.assertEqual(plan.plan_id, retry.plan_id)
        w.cases.mark_collection_scheduled("C1")

        # ---- 个案二：同一志愿者不可重复进入，撤回后替补 ----
        ranked_c2 = w.cases.run_search("C2", w.donor_pool())
        excluded_c2 = {a.donor_alias: a for a in ranked_c2 if a.excluded}
        self.assertIn("committed_elsewhere", excluded_c2[self.d2].reasons)
        with self.assertRaises(CaseStateError):
            w.cases.assign_donor("C2", self.d2)
        self.assertEqual(ranked_c2[0].donor_alias, self.d4)  # 乙不可用后丁为最高分

        w.cases.assign_donor("C2", self.d4)
        substitution = w.disruptions.handle_withdrawal("C2", self.d4)
        self.assertEqual(substitution.previous_donor, self.d4)
        self.assertEqual(substitution.new_donor, self.d5)
        ok, why = w.consent.permits(self.d4, "recontact")
        self.assertFalse(ok)  # 撤回后进入冷静期
        self.assertEqual(why, "cooling_off")

        # 个案二视图不出现个案一的任何信息
        view_c2 = json.dumps(w.cases.case_view("C2"), ensure_ascii=False)
        self.assertNotIn('"C1"', view_c2)
        self.assertNotIn(self.patient1, view_c2)

        # ---- 个案一：里程碑与跨时区航班延误 ----
        w.board.plan(
            "C1",
            [
                ("collection_hospital", "collection_completed",
                 TimeWindow(cst(2026, 9, 22, 6), cst(2026, 9, 22, 12))),
                ("transport", "departed",
                 TimeWindow(cst(2026, 9, 22, 8), cst(2026, 9, 22, 10))),
                ("transport", "arrived",
                 TimeWindow(cst(2026, 9, 22, 18), cst(2026, 9, 22, 23))),
                ("transplant_hospital", "infusion_started",
                 TimeWindow(cst(2026, 9, 22, 20), cst(2026, 9, 23, 8))),
            ],
        )
        self.assertEqual(
            w.board.report("C1", "collection_hospital", "collection_completed", cst(2026, 9, 22, 10)).status,
            "met",
        )
        self.assertEqual(
            w.board.report("C1", "transport", "departed", cst(2026, 9, 22, 9, 30)).status,
            "met",
        )

        # 航班延误：新预计到达 09-23 02:00（北京），超出原到达窗口但仍在输注窗口内
        alternatives = w.disruptions.handle_flight_delay("C1", cst(2026, 9, 23, 2))
        self.assertEqual(alternatives[0].action, "rebook_flight")
        self.assertTrue(alternatives[0].feasible)
        w.board.adjust_window(
            "C1", "arrived",
            TimeWindow(cst(2026, 9, 22, 18), cst(2026, 9, 23, 4)),
            "航班延误改签",
        )
        # 运输方在目的地以美东时间上报：09-22 15:40（UTC-4）= UTC 19:40
        arrived = w.board.report("C1", "transport", "arrived", datetime(2026, 9, 22, 15, 40, tzinfo=EDT))
        self.assertEqual(arrived.status, "met")
        self.assertEqual(
            w.board.report("C1", "transplant_hospital", "infusion_started", cst(2026, 9, 22, 23, 30)).status,
            "met",
        )
        self.assertEqual(w.board.breaches("C1"), [])

        # ---- 紧急身份解封：双人批准、自动到期 ----
        request_id = w.vault.request_unseal("coord-1", self.d2, "运输交接身份核对")
        self.assertIsNone(w.vault.approve_unseal(request_id, "sec-a"))
        grant = w.vault.approve_unseal(request_id, "sec-b")
        self.assertIsNotNone(grant)
        self.assertEqual(w.vault.resolve(grant.token).real_name, "志愿者乙")
        w.clock.advance(timedelta(minutes=21))
        with self.assertRaises(UnsealDenied):
            w.vault.resolve(grant.token)

        # ---- 审计导出核对 ----
        export = w.audit.export()
        self.assertTrue(export)
        self.assertEqual([e["seq"] for e in export], list(range(1, len(export) + 1)))

        # 每次匹配都带算法版本与排除理由
        ranked_events = [e for e in export if e["action"] == "match.ranked"]
        self.assertEqual({e["case_id"] for e in ranked_events}, {"C1", "C2"})
        for event in ranked_events:
            self.assertEqual(event["basis"]["algorithm_version"], ALGORITHM_VERSION)
        self.assertIn(self.d2, ranked_events[1]["basis"]["excluded"])

        # 替补依据完整
        substituted = [e for e in export if e["action"] == "case.substituted"]
        self.assertEqual(len(substituted), 1)
        self.assertEqual(substituted[0]["basis"]["reason"], "donor_withdrawal")
        self.assertEqual(substituted[0]["basis"]["previous_donor"], self.d4)
        self.assertEqual(substituted[0]["subject"], self.d5)
        self.assertEqual(substituted[0]["basis"]["algorithm_version"], ALGORITHM_VERSION)

        # 解封依据：双人批准与自动到期时间
        granted = [e for e in export if e["action"] == "identity.unseal.granted"]
        self.assertEqual(len(granted), 1)
        self.assertEqual(granted[0]["basis"]["approvers"], ["sec-a", "sec-b"])
        self.assertIn("expires_at", granted[0]["basis"])
        self.assertEqual(len([e for e in export if e["action"] == "identity.unseal.denied"]), 1)

        # 重试未造成重复通知与重复采集
        self.assertEqual(len([e for e in export if e["action"] == "notification.sent"]), 1)
        self.assertEqual(len([e for e in export if e["action"] == "notification.deduplicated"]), 1)
        self.assertEqual(len([e for e in export if e["action"] == "collection.scheduled"]), 1)

        # 延误与窗口调整留痕
        self.assertEqual(len([e for e in export if e["action"] == "disruption.flight_delay"]), 1)
        adjusted = [e for e in export if e["action"] == "milestone.window_adjusted"]
        self.assertEqual(adjusted[0]["basis"]["reason"], "航班延误改签")

        # 审计导出中绝不出现真实身份
        blob = json.dumps(export, ensure_ascii=False)
        for name, (real_name, contact) in w.donor_identity.items():
            self.assertNotIn(real_name, blob)
            self.assertNotIn(contact, blob)
        self.assertNotIn("患者甲", blob)
        self.assertNotIn("患者乙", blob)


if __name__ == "__main__":
    unittest.main()
