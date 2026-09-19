"""领域单元测试：匹配算法、同意/冷静期、别名、解封、占用账本。"""

import unittest
from datetime import timedelta

from coordination.clock import TIME_ZERO, Clock
from coordination.comms import (
    GLOBAL_CONTACT_INTERVAL_HOURS,
    REFLECTION_HOURS_BEFORE_CONFIRM,
    ConsentRegistry,
    ContactLedger,
    ContactPolicy,
    SCOPE_COLLECTION,
    SCOPE_CONFIRM_REQUEST,
    SCOPE_INITIAL_CONTACT,
    SCOPE_MEDICAL,
    default_renderer,
)
from coordination.audit import AuditLog
from coordination.errors import Conflict, DomainError, PermissionDenied
from coordination.identity import (
    Actor,
    BreakGlassManager,
    ROLE_COORDINATOR,
    ROLE_IDENTITY_OFFICER,
    donor_alias,
    patient_alias,
)
from coordination.matching import (
    ALGORITHM_VERSION,
    EXCL_COMMITTED_ELSEWHERE,
    EXCL_CONSENT_SCOPE,
    EXCL_MEDICAL_DEFERRAL,
    EXCL_RECENT_DONATION,
    EXCL_TYPING_INSUFFICIENT,
    compare_typing,
    rank_candidates,
)
from coordination.workflow import (
    POST_DONATION_COOLDOWN_DAYS,
    CommitmentLedger,
)

OFFICER = Actor("IO-1", "高怡", [ROLE_IDENTITY_OFFICER])
OFFICER2 = Actor("IO-2", "裴准", [ROLE_IDENTITY_OFFICER])
COORD = Actor("CO-1", "林岚", [ROLE_COORDINATOR])


def locus(a1, a2):
    return {"alleles": [a1, a2]}


class MatchingTest(unittest.TestCase):
    BASE = {
        "HLA-A": locus("A*02:01", "A*11:01"),
        "HLA-B": locus("B*46:01", "B*58:01"),
        "HLA-C": locus("C*01:02", "C*03:03"),
        "HLA-DRB1": locus("DRB1*09:01", "DRB1*12:02"),
        "HLA-DQB1": locus("DQB1*03:01", "DQB1*03:03"),
    }

    def _donor(self, donor_id, typing=None, **extra):
        return {"donor_id": donor_id, "age": 30, "typing": typing or self.BASE,
                "typing_complete": True, **extra}

    def test_full_match_is_ten_of_ten(self):
        matched, total, high, partial, _ = compare_typing(self.BASE, self.BASE)
        self.assertEqual((matched, total, high, partial), (10, 10, 10, 0))

    def test_one_mismatch_is_nine_of_ten(self):
        donor = dict(self.BASE)
        donor["HLA-A"] = locus("A*02:01", "A*24:02")
        matched, total, high, partial, _ = compare_typing(self.BASE, donor)
        self.assertEqual((matched, total, high, partial), (9, 10, 9, 0))

    def test_low_resolution_prefix_scores_partial(self):
        donor = dict(self.BASE)
        donor["HLA-A"] = locus("A*02", "A*11:01")  # 低分辨前缀相容
        matched, total, high, partial, _ = compare_typing(self.BASE, donor)
        self.assertEqual((matched, high, partial), (9.5, 9, 1))  # 一个全合 + 一个半合

    def test_ranking_keeps_version_grades_and_exclusion_reasons(self):
        clock = Clock()
        commitments = CommitmentLedger()
        case = {"case_id": "C1", "patient_typing": self.BASE,
                "clinical_deadline": "2026-03-01T00:00:00Z"}
        d1 = self._donor("D1", availability_confidence=90)
        d2_typing = dict(self.BASE)
        d2_typing["HLA-B"] = locus("B*46:01", "B*51:01")
        d2 = self._donor("D2", d2_typing, availability_confidence=95)
        d3 = self._donor("D3", medical_deferral=True)
        d4 = self._donor("D4", typing_complete=False,
                         typing={k: v for k, v in self.BASE.items() if k != "HLA-DQB1"})
        snapshot = rank_candidates(clock, case, [d2, d1, d3, d4], commitments, ALGORITHM_VERSION)
        self.assertEqual(snapshot["algorithm_version"], ALGORITHM_VERSION)
        # D1 全合排首位
        self.assertEqual(snapshot["candidates"][0]["donor_id"], "D1")
        self.assertEqual(snapshot["candidates"][0]["grade"], "10/10")
        reasons = {e["donor_id"]: e["reasons"] for e in snapshot["excluded"]}
        self.assertIn(EXCL_MEDICAL_DEFERRAL, reasons["D3"])
        self.assertIn(EXCL_TYPING_INSUFFICIENT, reasons["D4"])

    def test_unknown_algorithm_rejected(self):
        clock = Clock()
        with self.assertRaises(DomainError) as ctx:
            rank_candidates(clock, {"case_id": "C", "patient_typing": self.BASE,
                                    "clinical_deadline": "2026-03-01T00:00:00Z"},
                            [], CommitmentLedger(), "future-9.9")
        self.assertEqual(ctx.exception.code, "unknown_algorithm")

    def test_eligibility_exclusions_for_consent_commitment_and_cooldown(self):
        from coordination.comms import ConsentRegistry
        from coordination.clock import TIME_ZERO
        from datetime import timedelta
        clock = Clock()
        commitments = CommitmentLedger()
        consents = ConsentRegistry()
        deadline = "2026-03-01T00:00:00Z"

        # D-COOL：已在其他病例采集，处于捐献后冷却
        d_cool = self._donor("D-COOL")
        commitments.mark_collected(clock, "D-COOL", "CASE-OTHER",
                                   TIME_ZERO + timedelta(days=10))
        # D-BUSY：对其他病例有重叠承诺
        d_busy = self._donor("D-BUSY")
        commitments.register_commitment("D-BUSY", "CASE-OTHER",
                                        TIME_ZERO + timedelta(days=50),
                                        TIME_ZERO + timedelta(days=52))
        # D-NOCONSENT：无任何同意范围
        d_no = self._donor("D-NOCONSENT")
        consents.register("D-COOL", ["initial_contact"])
        consents.register("D-BUSY", ["initial_contact"])
        consents.register("D-NOCONSENT", [])
        case = {"case_id": "C1", "patient_typing": self.BASE, "clinical_deadline": deadline}
        snapshot = rank_candidates(
            clock, case, [d_cool, d_busy, d_no], commitments,
            ALGORITHM_VERSION, consents=consents)
        reasons = {e["donor_id"]: e["reasons"] for e in snapshot["excluded"]}
        self.assertIn(EXCL_RECENT_DONATION, reasons["D-COOL"])
        self.assertIn(EXCL_COMMITTED_ELSEWHERE, reasons["D-BUSY"])
        self.assertIn(EXCL_CONSENT_SCOPE, reasons["D-NOCONSENT"])
        self.assertEqual(snapshot["candidates"], [])


class IdentityAliasTest(unittest.TestCase):
    SECRET = "k-test"

    def test_same_donor_differs_across_cases_and_is_uncorrelatable(self):
        a1 = donor_alias(self.SECRET, "C1", "D1")
        a2 = donor_alias(self.SECRET, "C2", "D1")
        self.assertNotEqual(a1, a2)
        self.assertTrue(a1.startswith("D-") and a2.startswith("D-"))
        # 别名中不出现内部 ID
        self.assertNotIn("D1", a1)
        # 患者别名同样稳定但不可反推
        p = patient_alias(self.SECRET, "PAT-9")
        self.assertEqual(p, patient_alias(self.SECRET, "PAT-9"))

    def test_secret_change_breaks_alias(self):
        self.assertNotEqual(
            donor_alias("secret-1", "C1", "D1"),
            donor_alias("secret-2", "C1", "D1"),
        )


class BreakGlassTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.bg = BreakGlassManager()
        self.audit = AuditLog()

    def _request(self):
        grant_id, _ = self.bg.request(self.clock, COORD, "C1", ["D-ALIAS"],
                                      "紧急医疗需要", ttl_minutes=60)
        return grant_id

    def test_requires_two_distinct_officers(self):
        grant_id = self._request()
        self.bg.approve(self.clock, OFFICER, grant_id)
        # 一人批准不生效
        self.assertEqual(self.bg.status_of(self.clock, grant_id)["status"], "pending")
        with self.assertRaises(Conflict):
            self.bg.approve(self.clock, OFFICER, grant_id)
        req = self.bg.approve(self.clock, OFFICER2, grant_id)
        self.assertEqual(req["status"], "active")
        self.assertIsNotNone(req["expires_at"])

    def test_coordinator_cannot_approve(self):
        grant_id = self._request()
        with self.assertRaises(PermissionDenied):
            self.bg.approve(self.clock, COORD, grant_id)

    def test_scope_is_case_and_subject_bound(self):
        grant_id = self._request()
        self.bg.approve(self.clock, OFFICER, grant_id)
        self.bg.approve(self.clock, OFFICER2, grant_id)
        grant = self.bg.authorize(self.clock, COORD, "C1", "D-ALIAS")
        self.assertTrue(grant.is_active(self.clock))
        # 作用域外的病例
        with self.assertRaises(PermissionDenied):
            self.bg.authorize(self.clock, COORD, "C2", "D-ALIAS")
        # 作用域外的对象
        with self.assertRaises(PermissionDenied):
            self.bg.authorize(self.clock, COORD, "C1", "D-OTHER")

    def test_grant_expires_automatically(self):
        grant_id = self._request()
        self.bg.approve(self.clock, OFFICER, grant_id)
        self.bg.approve(self.clock, OFFICER2, grant_id)
        self.clock.advance(minutes=61)
        self.assertEqual(self.bg.status_of(self.clock, grant_id)["status"], "expired")
        with self.assertRaises(PermissionDenied):
            self.bg.authorize(self.clock, COORD, "C1", "D-ALIAS")

    def test_reason_and_subjects_required(self):
        with self.assertRaises(DomainError):
            self.bg.request(self.clock, COORD, "C1", ["D-ALIAS"], "   ")
        with self.assertRaises(DomainError):
            self.bg.request(self.clock, COORD, "C1", [], "理由")

    def test_revocation_blocks_use(self):
        grant_id = self._request()
        self.bg.revoke(self.clock, OFFICER, grant_id)
        with self.assertRaises(Conflict):
            self.bg.approve(self.clock, OFFICER2, grant_id)


class ConsentAndCoolingTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.consents = ConsentRegistry()
        self.ledger = ContactLedger()
        self.audit = AuditLog()
        self.policy = ContactPolicy(self.clock, self.consents, self.ledger,
                                    self.audit, default_renderer)
        self.consents.register("D1", [SCOPE_INITIAL_CONTACT, SCOPE_CONFIRM_REQUEST,
                                      SCOPE_MEDICAL, SCOPE_COLLECTION])

    def _send(self, template, key):
        return self.policy.send(COORD, "D1", "C1", template, key,
                                context={"donor_alias": "D-ALIAS"})

    def test_idempotent_retry_does_not_send_again(self):
        first = self._send("initial_match_notice", "k1")
        retry = self._send("initial_match_notice", "k1")
        self.assertFalse(first["deduped"])
        self.assertTrue(retry["deduped"])
        # 实际只落了一条联络
        self.assertEqual(len(self.ledger._by_donor["D1"]), 1)

    def test_reflection_period_before_confirmation(self):
        self._send("initial_match_notice", "k-init")
        self.clock.advance(hours=REFLECTION_HOURS_BEFORE_CONFIRM - 1)
        with self.assertRaises(DomainError) as ctx:
            self._send("confirm_request", "k-confirm")
        self.assertEqual(ctx.exception.code, "reflection_period")
        self.clock.advance(hours=2)
        sent = self._send("confirm_request", "k-confirm")
        self.assertFalse(sent["deduped"])

    def test_withdrawn_consent_blocks_future_contact(self):
        self._send("initial_match_notice", "k-init")
        self.consents.withdraw(self.clock, "D1", [SCOPE_INITIAL_CONTACT])
        self.clock.advance(days=2)
        with self.assertRaises(DomainError) as ctx:
            self._send("initial_match_notice", "k-init-2")
        self.assertEqual(ctx.exception.code, "consent_out_of_scope")

    def test_global_interval_and_scope_cooling_enforced(self):
        # 初始告知 2 小时后发体检通知：全局 6 小时间隔先拦截
        self._send("initial_match_notice", "k1")
        self.clock.advance(hours=2)
        with self.assertRaises(DomainError) as ctx:
            self._send("exam_schedule", "k-med")
        self.assertEqual(ctx.exception.code, "global_contact_interval")
        # 越过全局间隔后放行（medical 类此前无记录）
        self.clock.advance(hours=GLOBAL_CONTACT_INTERVAL_HOURS)
        self._send("exam_schedule", "k-med")
        # 1 小时后再次发体检通知：同类 12 小时冷静期拦截（先于全局间隔判定）
        self.clock.advance(hours=1)
        with self.assertRaises(DomainError) as ctx:
            self._send("exam_schedule", "k-med-2")
        self.assertEqual(ctx.exception.code, "cooling_period")
        # 越过 12 小时冷静期后可以再次联络
        self.clock.advance(hours=12)
        self.assertFalse(self._send("exam_schedule", "k-med-3")["deduped"])

    def test_pending_decision_in_other_case_hides_case_identity(self):
        self._send("initial_match_notice", "k-init")
        self.clock.advance(days=3)
        self.policy.send(COORD, "D1", "C1", "confirm_request", "k-c1",
                         context={"donor_alias": "D-A"})
        # 越过确认类冷静期后，另一病例发起确认：待决定互斥拦截，且错误不含 C1
        self.clock.advance(days=3)
        with self.assertRaises(Conflict) as ctx:
            self.policy.send(COORD, "D1", "C2", "confirm_request", "k-c2",
                             context={"donor_alias": "D-B"})
        self.assertEqual(ctx.exception.code, "donor_decision_pending")
        self.assertNotIn("C1", str(ctx.exception.details))

    def test_notification_text_never_names_patient(self):
        text = default_renderer("confirm_request", {"donor_alias": "D-X",
                                                    "patient_alias": "P-SECRET"})
        self.assertNotIn("P-SECRET", text)


class CommitmentLedgerTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.ledger = CommitmentLedger()

    def test_commitment_mutex_only_concerns_other_cases(self):
        self.ledger.register_commitment("D1", "C1", TIME_ZERO + timedelta(days=10),
                                        TIME_ZERO + timedelta(days=12))
        # 同病例窗口内不互斥
        self.assertFalse(self.ledger.is_in_post_donation_cooldown(
            "D1", TIME_ZERO + timedelta(days=11)))
        # 其他病例落在承诺窗口 -> 互斥，但结论不含病例信息
        self.assertTrue(self.ledger.has_overlapping_commitment(
            "D1", "C2", TIME_ZERO + timedelta(days=11)))
        self.assertFalse(self.ledger.has_overlapping_commitment(
            "D1", "C1", TIME_ZERO + timedelta(days=11)))

    def test_collection_is_globally_once_and_cools_down(self):
        self.ledger.mark_collected(self.clock, "D1", "C1")
        with self.assertRaises(Conflict) as ctx:
            self.ledger.mark_collected(self.clock, "D1", "C2")
        self.assertEqual(ctx.exception.code, "donor_already_collected")
        self.assertTrue(self.ledger.is_in_post_donation_cooldown(
            "D1", self.clock.now() + timedelta(days=POST_DONATION_COOLDOWN_DAYS - 1)))
        self.assertFalse(self.ledger.is_in_post_donation_cooldown(
            "D1", self.clock.now() + timedelta(days=POST_DONATION_COOLDOWN_DAYS + 1)))

    def test_blackout_window(self):
        self.ledger.add_blackout("D1", TIME_ZERO + timedelta(days=5),
                                 TIME_ZERO + timedelta(days=6))
        self.assertTrue(self.ledger.is_in_blackout("D1", TIME_ZERO + timedelta(days=5, hours=12)))
        self.assertFalse(self.ledger.is_in_blackout("D1", TIME_ZERO + timedelta(days=7)))


if __name__ == "__main__":
    unittest.main()
