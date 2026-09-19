"""候选排序：算法版本固定、排除理由保留、同分按入库先后。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from coordination import ALGORITHM_VERSION, DonorProfile, Matcher, SearchRequest, score_hla

from tests.helpers import PATIENT_JIA_HLA, T0

UTC = timezone.utc


def donor(alias, hla, days=0):
    return DonorProfile(alias, hla, T0 + timedelta(days=days))


class ScoreTest(unittest.TestCase):
    def test_perfect_match_scores_ten(self):
        score, max_score = score_hla(PATIENT_JIA_HLA, PATIENT_JIA_HLA)
        self.assertEqual((score, max_score), (10, 10))

    def test_partial_match_counts_alleles(self):
        donor_hla = dict(PATIENT_JIA_HLA)
        donor_hla["DQB1"] = ("DQB1*05:01", "DQB1*06:01")
        score, _ = score_hla(PATIENT_JIA_HLA, donor_hla)
        self.assertEqual(score, 8)

    def test_homozygous_donor_matches_both_copies(self):
        patient = {"A": ("A*01:01", "A*01:01")}
        self.assertEqual(score_hla(patient, {"A": ("A*01:01", "A*01:01")}), (2, 2))
        self.assertEqual(score_hla(patient, {"A": ("A*01:01", "A*02:01")}), (1, 2))

    def test_missing_locus_scores_zero(self):
        patient = {"A": ("A*01:01", "A*02:01")}
        self.assertEqual(score_hla(patient, {}), (0, 2))


class RankTest(unittest.TestCase):
    def setUp(self):
        self.matcher = Matcher()
        self.request = SearchRequest(
            case_id="C1",
            patient_alias="P-x",
            hla=PATIENT_JIA_HLA,
            deadline=datetime(2026, 10, 1, tzinfo=UTC),
            created_at=T0,
        )

    def test_eligible_first_then_excluded_with_reasons(self):
        pool = [
            donor("D-low", {"A": ("A*09:01", "A*09:02")}, days=0),
            donor("D-perfect", PATIENT_JIA_HLA, days=1),
            donor("D-blocked", PATIENT_JIA_HLA, days=2),
        ]
        eligibility = lambda alias: ["cooling_off"] if alias == "D-blocked" else []
        ranked = self.matcher.rank(self.request, pool, eligibility)
        self.assertEqual([a.donor_alias for a in ranked], ["D-perfect", "D-low", "D-blocked"])
        blocked = ranked[-1]
        self.assertTrue(blocked.excluded)
        self.assertEqual(blocked.reasons, ("cooling_off",))

    def test_version_is_pinned_on_every_assessment(self):
        pool = [donor("D-1", PATIENT_JIA_HLA)]
        ranked = self.matcher.rank(self.request, pool, lambda alias: [])
        self.assertEqual(ranked[0].algorithm_version, ALGORITHM_VERSION)
        custom = Matcher(version="hla-match-v9.9")
        ranked = custom.rank(self.request, pool, lambda alias: [])
        self.assertEqual(ranked[0].algorithm_version, "hla-match-v9.9")

    def test_tie_breaks_by_registration_order(self):
        pool = [
            donor("D-late", PATIENT_JIA_HLA, days=5),
            donor("D-early", PATIENT_JIA_HLA, days=1),
        ]
        # 调用方按入库顺序传入；同分时保持稳定
        pool.sort(key=lambda d: d.registered_at)
        ranked = self.matcher.rank(self.request, pool, lambda alias: [])
        self.assertEqual([a.donor_alias for a in ranked], ["D-early", "D-late"])


if __name__ == "__main__":
    unittest.main()
