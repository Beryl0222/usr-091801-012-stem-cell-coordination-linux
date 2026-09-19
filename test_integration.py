"""联调验收：双患者流程并行、跨时区延误、重试幂等与审计依据核对。

对应验收口径：
1. 两个患者流程同时推进并注入跨时区延误；
2. 确认重试不会重复通知或重复采集；
3. 从审计导出核对每次匹配、替补和身份解封的依据；
4. 捐患身份长期隔离，跨病例视图互不泄露。
"""

import unittest
from collections import Counter

from coordination.matching import (
    ALGORITHM_VERSION,
    EXCL_MEDICAL_DEFERRAL,
    EXCL_RECENT_DONATION,
    EXCL_TYPING_INSUFFICIENT,
)
from coordination.workflow import (
    PLAN_ESCORT_HAND_CARRY,
    PLAN_GROUND_EMERGENCY,
    PLAN_REBOOK_FLIGHT,
    PLAN_SUBSTITUTE_BACKUP,
    PLAN_TARGETED_REEXAM,
    PLAN_RERANK_SEARCH,
    TRIGGER_EXAM_ABNORMAL,
    TRIGGER_FLIGHT_DELAY,
    TRIGGER_PRODUCT_WINDOW_RISK,
)
from selftest import run_scenario


class DualPatientIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc, cls.facts = run_scenario()
        cls.s = cls.svc.store

    def setUp(self):
        self.f = self.facts

    # ---- 1. 匹配：算法版本、分级、排除理由 --------------------------------

    def test_rankings_record_algorithm_version_and_grades(self):
        rank_a = self.f["rank_a"]
        rank_b = self.f["rank_b"]
        self.assertEqual(rank_a["algorithm_version"], ALGORITHM_VERSION)
        self.assertEqual(rank_b["algorithm_version"], ALGORITHM_VERSION)
        grades_a = {c["alias"]: c["grade"] for c in rank_a["candidates"]}
        self.assertEqual(grades_a[self.f["aliases"]["a_d01"]], "10/10")
        self.assertEqual(grades_a[self.f["aliases"]["a_d02"]], "9/10")
        # 排序：全合且配合度高的 D01 先于同分但配合度低的 D03
        self.assertEqual(rank_a["candidates"][0]["alias"], self.f["aliases"]["a_d01"])
        self.assertEqual(rank_b["candidates"][0]["alias"], self.f["aliases"]["b_d06"])

    def test_exclusion_reasons_kept_for_every_ranked_out_donor(self):
        excluded = {e["alias"]: e["reasons"] for e in self.f["rank_a"]["excluded"]}
        # D04 体检暂缓、D05 分型不完整；都在 A 病例有别名和理由
        reasons_flat = {r for reasons in excluded.values() for r in reasons}
        self.assertIn(EXCL_MEDICAL_DEFERRAL, reasons_flat)
        self.assertIn(EXCL_TYPING_INSUFFICIENT, reasons_flat)

        # 审计中每次匹配的依据可核对：算法版本 + 排除映射
        ranked_events = [e for e in self.f["audit_all"]["events"]
                         if e["action"] == "candidates_ranked"]
        self.assertEqual(len(ranked_events), 2)
        for event in ranked_events:
            self.assertEqual(event["details"]["algorithm_version"], ALGORITHM_VERSION)
            self.assertIn("basis", event["details"])
        # D04/D05 出现在某条匹配事件的 excluded 明细中且理由正确
        all_excluded = {}
        for event in ranked_events:
            all_excluded.update(event["details"]["excluded"])
        self.assertIn(EXCL_MEDICAL_DEFERRAL, all_excluded.get("D04", []))
        self.assertIn(EXCL_TYPING_INSUFFICIENT, all_excluded.get("D05", []))

    # ---- 2. 身份隔离：别名、跨病例、视图 -----------------------------------

    def test_aliases_are_case_scoped_and_views_are_alias_only(self):
        aliases = self.f["aliases"]
        self.assertNotEqual(aliases["a_d01"], aliases["b_d01"])
        for alias in aliases.values():
            self.assertRegex(alias, r"^[DP]-[A-F0-9]+$")
        # 病例 A 的审计事件不含病例 B 的别名，也不含任何真实姓名
        import json
        events_a = self.f["audit_a"]["events"]
        blob_a = json.dumps(events_a, ensure_ascii=False)
        self.assertNotIn(aliases["b_d06"], blob_a)
        self.assertNotIn(aliases["b_d07"], blob_a)
        for name in ("张伟", "李娜", "王强", "赵敏", "陈杰", "林芳", "周磊"):
            self.assertNotIn(name, blob_a)
        self.assertFalse(self.f["leak_a_has_b_aliases"])

    # ---- 3. 重试幂等：不重复通知、不重复采集 -------------------------------

    def test_retries_never_duplicate_notification_or_collection(self):
        dedup = self.f["dedup"]
        for key in ("initial_retry", "confirm_retry", "collection_retry",
                    "delay_retry", "delivery_retry", "backup_retry"):
            self.assertTrue(dedup[key], f"{key} 未被识别为重试")

        # 联络账本：同模板只有一条真实发送，重试只产生去重审计
        for donor_id in ("D01", "D06", "D07"):
            templates = Counter(e["template"] for e in self.s.contacts._by_donor[donor_id])
            for template, count in templates.items():
                self.assertEqual(count, 1, f"{donor_id} 的 {template} 重复发送 {count} 次")
        dedup_events = [e for e in self.f["audit_all"]["events"]
                        if e["action"] == "notification_dedup"]
        self.assertTrue(dedup_events)

        # 全局采集登记：每名志愿者至多一次实际采集
        collected = self.s.commitments._collections
        self.assertEqual(set(collected), {"D01", "D07"})
        self.assertEqual(collected["D01"]["case_id"], self.f["case_a"])
        self.assertEqual(collected["D07"]["case_id"], self.f["case_b"])

    # ---- 4. 同意范围与冷静期 ----------------------------------------------

    def test_consent_scope_and_reflection_rejections_recorded(self):
        codes = {k: v["code"] for k, v in self.f["rejections"].items() if v}
        self.assertEqual(codes["confirm_in_reflection"], "reflection_period")
        self.assertEqual(codes["d07_reflection"], "reflection_period")
        self.assertEqual(codes["consent_withdrawn_blocks"], "consent_out_of_scope")

    # ---- 5. 跨时区延误与产品时效窗 ----------------------------------------

    def test_cross_timezone_milestone_normalized_to_utc(self):
        # 东京 19:00+09:00 规范化为 UTC 10:00
        self.assertEqual(self.f["handover_utc"], "2026-01-10T10:00:00.000Z")
        window = self.f["product_window_a"]
        self.assertEqual(window["start"], "2026-01-10T08:00:00.000Z")
        self.assertEqual(window["deadline"], "2026-01-11T08:00:00.000Z")
        # 里程碑保留提交时区
        handover = [m for m in self.s.cases[self.f["case_a"]]["milestones"]
                    if m["type"] == "airport_handover"][0]
        self.assertEqual(handover["details"]["submitted_tz"], "+09:00")

    def test_delay_contingencies_have_priority_and_escalate_on_breach(self):
        first = self.f["contingencies"]["a_delay1"]
        breach = self.f["contingencies"]["a_delay2_breach"]
        self.assertEqual(first["trigger"], TRIGGER_FLIGHT_DELAY)
        plans = [p["plan"] for p in first["plans"]]
        self.assertEqual(plans, [PLAN_REBOOK_FLIGHT, PLAN_ESCORT_HAND_CARRY,
                                 PLAN_GROUND_EMERGENCY])
        priorities = [p["priority"] for p in first["plans"]]
        self.assertEqual(priorities, [1, 2, 3])
        # 预计到达超过有效期 -> 升级为产品时效风险
        self.assertEqual(breach["trigger"], TRIGGER_PRODUCT_WINDOW_RISK)
        self.assertTrue(breach["delay"]["breach"])
        self.assertLess(breach["delay"]["window_remaining_minutes_at_arrival"], 0)

    # ---- 6. 体检异常与替补 -------------------------------------------------

    def test_exam_abnormal_contingency_and_backup_substitution(self):
        exam = self.f["contingencies"]["b_exam_fail"]
        self.assertEqual(exam["trigger"], TRIGGER_EXAM_ABNORMAL)
        plans = [p["plan"] for p in exam["plans"]]
        self.assertEqual(plans, [PLAN_TARGETED_REEXAM, PLAN_SUBSTITUTE_BACKUP,
                                 PLAN_RERANK_SEARCH])

        sub = self.f["substitution_b"]
        self.assertEqual(sub["to_alias"], self.f["aliases"]["b_d07"])
        self.assertEqual(sub["algorithm_version"], ALGORITHM_VERSION)
        self.assertEqual(sub["to_rank_position"], 2)
        self.assertEqual(sub["plan"], PLAN_SUBSTITUTE_BACKUP)

        # B 病例旧候选处于 substituted，新候选最终 completed
        case_b = self.s.cases[self.f["case_b"]]
        self.assertEqual(case_b["candidates"]["D06"]["state"], "substituted")
        self.assertEqual(case_b["candidates"]["D07"]["state"], "completed")
        # D06 的承诺已释放，不影响其后续安排
        self.assertFalse(any(c["case_id"] == self.f["case_b"]
                             for c in self.s.commitments._commitments.get("D06", [])))

    # ---- 7. 审计依据：每次匹配、替补、解封 ----------------------------------

    def test_audit_trails_document_every_basis(self):
        audit = self.f["audit_all"]
        self.assertTrue(audit["chain_valid"])
        events = audit["events"]
        by_action = {}
        for event in events:
            by_action.setdefault(event["action"], []).append(event)

        # 每次匹配
        self.assertEqual(len(by_action["candidates_ranked"]), 2)
        # 每次替补（B 病例一次）：含来自谁、换成谁、顺位、算法版本
        backups = by_action["backup_activated"]
        self.assertEqual(len(backups), 1)
        detail = backups[0]["details"]
        self.assertEqual(detail["from_donor"], "D06")
        self.assertEqual(detail["to_donor"], "D07")
        self.assertEqual(detail["algorithm_version"], ALGORITHM_VERSION)
        self.assertIn("reason",
                      next(e for e in events if e["action"] == "candidate_transition"
                           and e["details"].get("to") == "substituted")["details"])

        # 身份解封全生命周期：请求 -> 两次批准 -> 读取 -> 自动到期
        self.assertEqual(len(by_action["unseal_request"]), 1)
        self.assertEqual(len(by_action["unseal_approved"]), 2)
        reveals = by_action["identity_revealed"]
        self.assertEqual(len(reveals), 1)
        self.assertNotIn("张伟", str(reveals[0]["details"]))  # 审计只记依据不记明文
        expired = by_action["unseal_expired"]
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["actor"], "system")
        self.assertEqual(expired[0]["at"], self.f["grant_expires_at"])

        # 状态迁移链完整：B 病例 D06 与 D07 的迁移序列可回放
        b_transitions = [e for e in by_action["candidate_transition"]
                         if e["case_id"] == self.f["case_b"]]
        d06_states = [(e["details"]["from"], e["details"]["to"])
                      for e in b_transitions if e["subject"] == "D06"]
        self.assertIn(("exam_scheduled", "exam_abnormal"), d06_states)
        self.assertIn(("exam_abnormal", "substituted"), d06_states)

    def test_breakglass_rejections(self):
        codes = {k: v["code"] for k, v in self.f["rejections"].items() if v}
        self.assertEqual(codes["reveal_with_one_approval"], "forbidden")
        self.assertEqual(codes["duplicate_approval"], "already_approved")
        self.assertEqual(codes["reveal_cross_case"], "forbidden")
        self.assertEqual(codes["reveal_after_ttl"], "forbidden")
        self.assertEqual(codes["coordinator_list_grants"], "forbidden")
        self.assertEqual(codes["wrong_hospital"], "forbidden")
        self.assertEqual(self.f["grants"]["status_after_ttl"], "expired")
        self.assertEqual(self.f["revealed_name"], "张伟")
        self.assertTrue(self.f["grants_view_clean"])

    # ---- 8. 采集后冷却：重排排除已采集志愿者 -------------------------------

    def test_rerank_after_collection_excludes_collected_donors(self):
        rerank = self.f["rerank_b_after_collection"]
        excluded = {e["alias"]: e["reasons"] for e in rerank["excluded"]}
        # D01/D07 已采集，必须在重排中被捐献后冷却排除（且不暴露是哪个病例采集的）
        flat = {r for reasons in excluded.values() for r in reasons}
        self.assertIn(EXCL_RECENT_DONATION, flat)
        # 排除明细不带其他病例 ID
        import json
        self.assertNotIn(self.f["case_a"], json.dumps(rerank))


if __name__ == "__main__":
    unittest.main()
