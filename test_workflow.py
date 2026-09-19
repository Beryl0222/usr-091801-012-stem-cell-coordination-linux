"""工作流分支测试：撤回替补、跨病例重复采集、超窗送达、承诺互斥等。"""

import unittest

from coordination.app import CoordinationService
from coordination.clock import Clock
from coordination.errors import Conflict, DomainError, PermissionDenied
from coordination.identity import (
    Actor,
    ROLE_COLLECTION_HOSPITAL,
    ROLE_COURIER,
)
from scenario import (
    ALL_CONTACT_SCOPES,
    COLLECTION_HOSPITAL,
    COORD,
    COURIER,
    OFFICER,
    PATIENT_A_TYPING,
    TRANSPLANT_HOSPITAL,
    alias_of,
    at,
)


def expect(fn, code=None):
    try:
        fn()
    except (DomainError, Conflict, PermissionDenied) as error:
        if code is not None:
            assert error.code == code, f"期望 {code}，实际 {error.code}"
        return error
    raise AssertionError("调用本应被拒绝")


class WorkflowBranchTest(unittest.TestCase):
    def setUp(self):
        self.svc = CoordinationService(clock=Clock())
        s = self.svc
        s.enroll_patient(OFFICER, "PAT-A", "患者甲", "ID-A", "13900000001", PATIENT_A_TYPING)
        # 两位与患者 A 全合的候选 + 一位将在第二病例竞争同一人的患者
        s.enroll_donor(OFFICER, "D01", "张伟", "ID-D1", "13800000001",
                       {"age": 28, "typing": PATIENT_A_TYPING, "availability_confidence": 90},
                       ALL_CONTACT_SCOPES)
        s.enroll_donor(OFFICER, "D02", "李娜", "ID-D2", "13800000002",
                       {"age": 30, "typing": PATIENT_A_TYPING, "availability_confidence": 80},
                       ALL_CONTACT_SCOPES)
        s.enroll_patient(OFFICER, "PAT-B", "患者乙", "ID-B", "13900000002", PATIENT_A_TYPING)
        self.case_a = s.open_case(COORD, "PAT-A", at(day=30), "H-CAPEK", "H-TPEK")["case_id"]
        self.case_b = s.open_case(COORD, "PAT-B", at(day=31), "H-CAPEK", "H-TPEK")["case_id"]
        s.search_candidates(COORD, self.case_a, ["D01", "D02"])
        s.search_candidates(COORD, self.case_b, ["D01", "D02"])
        self.a_d01 = alias_of(s, self.case_a, "D01")
        self.a_d02 = alias_of(s, self.case_a, "D02")
        self.b_d01 = alias_of(s, self.case_b, "D01")
        self.b_d02 = alias_of(s, self.case_b, "D02")

    def _to_confirmed(self, case_id, alias, start_day, end_day, key_prefix):
        s = self.svc
        s.notify_initial_match(COORD, case_id, alias, f"{key_prefix}-init")
        s.advance(hours=7)
        s.advance(days=2)
        s.request_confirmation_notice(COORD, case_id, alias, f"{key_prefix}-confirm")
        s.record_donor_decision(COORD, case_id, alias, True,
                                {"start": at(day=start_day), "end": at(day=end_day)},
                                f"{key_prefix}-decision")

    def _pass_exam_and_schedule_collection(self, case_id, alias, collect_day, prefix):
        """确认 -> 体检安排 -> 体检通过 -> 采集安排（含必要的联络间隔推进）。"""
        s = self.svc
        s.advance(days=1)
        s.schedule_exam(COORD, case_id, alias, at(collect_day - 4, hour=2), f"{prefix}-exam-n")
        s.report_exam(COLLECTION_HOSPITAL, case_id, alias, True, [], f"{prefix}-exam-r")
        s.advance(hours=13)
        s.schedule_collection(COORD, case_id, alias, at(collect_day, hour=7), f"{prefix}-cn")

    def test_donor_withdrawal_opens_contingency_and_backup_takes_over(self):
        s = self.svc
        s.notify_initial_match(COORD, self.case_a, self.a_d01, "w-init")
        s.advance(days=3)
        s.request_confirmation_notice(COORD, self.case_a, self.a_d01, "w-confirm")
        result = s.withdraw_donor(COORD, self.case_a, self.a_d01, "志愿者个人原因", "w-withdraw")
        # 撤回触发有优先级的替补方案
        plans = [p["plan"] for p in result["contingency"]["plans"]]
        self.assertEqual(plans, ["substitute_backup_donor", "rerank_broader_search"])
        # 撤回重试幂等，不产生第二次状态迁移
        retry = s.withdraw_donor(COORD, self.case_a, self.a_d01, "志愿者个人原因", "w-withdraw")
        self.assertTrue(retry["deduped"])

        backup = s.activate_backup(COORD, self.case_a, "w-backup")
        self.assertEqual(backup["to_alias"], self.a_d02)
        case = s.store.cases[self.case_a]
        self.assertEqual(case["candidates"]["D01"]["state"], "withdrawn")
        self.assertEqual(case["candidates"]["D02"]["state"], "ranked")
        self.assertEqual(case["active_donor_id"], "D02")

    def test_overlapping_commitment_window_blocks_parallel_case(self):
        s = self.svc
        # 病例 A 先占用 D01 的 day9-11 窗口
        self._to_confirmed(self.case_a, self.a_d01, 9, 11, "A")
        # 病例 B 试图在重叠窗口确认同一人 -> 拒绝，且错误不含病例 A 标识
        s.advance(hours=7)  # 越过跨病例全局联络间隔
        s.notify_initial_match(COORD, self.case_b, self.b_d01, "B-init")
        s.advance(days=2)  # 同时越过考虑期与 confirm 类冷静期（跨病例共享）
        s.request_confirmation_notice(COORD, self.case_b, self.b_d01, "B-confirm")
        error = expect(lambda: s.record_donor_decision(
            COORD, self.case_b, self.b_d01, True,
            {"start": at(day=10), "end": at(day=12)}, "B-decision"),
            code="committed_elsewhere")
        self.assertNotIn(self.case_a, str(error.details))

        # 释放病例 B 的 D01（互斥不可并行），按顺位激活 D02
        released = s.release_active_candidate(COORD, self.case_b, "窗口与其他捐献安排互斥",
                                              "B-release")
        self.assertEqual(released["candidate"]["state"], "substituted")
        backup = s.activate_backup(COORD, self.case_b, "B-backup")
        self.assertEqual(backup["to_alias"], self.b_d02)

        # D02 走完流程在不重叠窗口确认
        s.notify_initial_match(COORD, self.case_b, self.b_d02, "B2-init")
        s.advance(days=3)
        s.request_confirmation_notice(COORD, self.case_b, self.b_d02, "B2-confirm")
        decided = s.record_donor_decision(
            COORD, self.case_b, self.b_d02, True,
            {"start": at(day=13), "end": at(day=15)}, "B2-decision")
        self.assertEqual(decided["candidate"]["state"], "confirmed")
        # 病例 A 的承诺不受影响，病例 B 的 D01 承诺已释放
        self.assertFalse(any(c["case_id"] == self.case_b
                             for c in s.store.commitments._commitments.get("D01", [])))
        self.assertTrue(any(c["case_id"] == self.case_a
                            for c in s.store.commitments._commitments.get("D01", [])))

    def test_double_collection_across_cases_is_rejected(self):
        s = self.svc
        self._to_confirmed(self.case_a, self.a_d01, 9, 11, "A")
        self._pass_exam_and_schedule_collection(self.case_a, self.a_d01, 9, "A")
        s.assign_courier(COORD, self.case_a, "CR-1")
        s.advance(days=5)
        s.record_collection(COLLECTION_HOSPITAL, self.case_a, self.a_d01,
                            at(day=9, hour=8), "A-collect")

        # 病例 B 的 D01 即便通过流程走到采集，也必须被全局唯一拦截
        s.store.cases[self.case_b]["active_donor_id"] = "D01"
        error = expect(lambda: s.record_collection(
            COLLECTION_HOSPITAL, self.case_b, self.b_d01, at(day=10), "B-collect"),
            code="donor_already_collected")
        self.assertNotIn(self.case_a, str(error.details))

    def test_delivery_after_product_window_is_rejected(self):
        s = self.svc
        self._to_confirmed(self.case_a, self.a_d01, 9, 11, "A")
        self._pass_exam_and_schedule_collection(self.case_a, self.a_d01, 9, "A")
        s.assign_courier(COORD, self.case_a, "CR-1")
        s.advance(days=5)
        s.record_collection(COLLECTION_HOSPITAL, self.case_a, self.a_d01,
                            at(day=9, hour=8), "A-collect")
        s.record_pickup(COURIER, self.case_a, at(day=9, hour=9), "A-pickup")
        # 超过 24h 产品窗（截止 1/11 08:00 UTC）
        expect(lambda: s.record_delivery(
            TRANSPLANT_HOSPITAL, self.case_a, at(day=10, hour=9), "A-late"),
            code="product_window_expired")

    def test_flight_delay_before_collection_rejected(self):
        s = self.svc
        s.assign_courier(COORD, self.case_a, "CR-1")
        expect(lambda: s.report_flight_delay(
            COURIER, self.case_a, "CA1", 60, at(day=10), "early-delay"),
            code="no_product_window")

    def test_non_active_candidate_cannot_be_advanced(self):
        s = self.svc
        # D02 是顺位候选而非活跃候选
        expect(lambda: s.notify_initial_match(
            COORD, self.case_a, self.a_d02, "x-init"), code="not_active_candidate")

    def test_party_assignment_enforced_on_milestones(self):
        s = self.svc
        other = Actor("H-OTHER", "其他采集医院", [ROLE_COLLECTION_HOSPITAL])
        expect(lambda: s.report_exam(other, self.case_a, self.a_d01, True),
               code="forbidden")
        rogue_courier = Actor("CR-9", "其他运输方", [ROLE_COURIER])
        expect(lambda: s.record_pickup(rogue_courier, self.case_a),
               code="forbidden")

    def test_backup_chain_exhausted_requires_rerank(self):
        s = self.svc
        # 只有一个候选的病例（新建），撤回后无替补
        s.enroll_donor(OFFICER, "D99", "陈九", "ID-D99", "13800000099",
                       {"age": 29, "typing": PATIENT_A_TYPING}, ALL_CONTACT_SCOPES)
        case_c = s.open_case(COORD, "PAT-A", at(day=40), "H-CAPEK", "H-TPEK")["case_id"]
        s.search_candidates(COORD, case_c, ["D99"])
        alias = alias_of(s, case_c, "D99")
        s.notify_initial_match(COORD, case_c, alias, "C-init")
        s.advance(days=3)
        s.request_confirmation_notice(COORD, case_c, alias, "C-confirm")
        s.withdraw_donor(COORD, case_c, alias, "撤回", "C-withdraw")
        expect(lambda: s.activate_backup(COORD, case_c, "C-backup"),
               code="no_backup_candidate")

    def test_audit_chain_remains_valid_throughout_branches(self):
        # 触发上述各分支后链仍可校验
        self.test_donor_withdrawal_opens_contingency_and_backup_takes_over()
        self.assertTrue(self.svc.store.audit.verify_chain())


if __name__ == "__main__":
    unittest.main()
