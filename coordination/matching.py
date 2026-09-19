"""分型匹配、候选排序与排除理由。

患者侧只提交检索所需的分型与临床时限；排序结果保留算法版本、
每位候选的匹配分级、得分与排除理由，供审计回溯"为什么选他/为什么排除"。
"""

from .clock import iso
from .comms import SCOPE_INITIAL_CONTACT
from .errors import DomainError, NotFound

ALGORITHM_VERSION = "hla-match-1.2.0"
_LEGACY_VERSIONS = frozenset({"hla-match-1.0.0", "hla-match-1.1.0"})

LOCI = ("HLA-A", "HLA-B", "HLA-C", "HLA-DRB1", "HLA-DQB1")

# 排除理由代码对审计稳定，不要随意改名
EXCL_MEDICAL_DEFERRAL = "medical_deferral"
EXCL_TYPING_INSUFFICIENT = "typing_insufficient"
EXCL_COMMITTED_ELSEWHERE = "committed_elsewhere"
EXCL_RECENT_DONATION = "recent_donation_cooldown"
EXCL_CONSENT_SCOPE = "consent_scope_missing"
EXCL_UNAVAILABLE_WINDOW = "unavailable_window"
EXCL_DEFERRED_AGE = "age_out_of_range"

EXCLUSION_REASONS = frozenset(
    {
        EXCL_MEDICAL_DEFERRAL,
        EXCL_TYPING_INSUFFICIENT,
        EXCL_COMMITTED_ELSEWHERE,
        EXCL_RECENT_DONATION,
        EXCL_CONSENT_SCOPE,
        EXCL_UNAVAILABLE_WINDOW,
        EXCL_DEFERRED_AGE,
    }
)

MIN_DONOR_AGE = 18
MAX_DONOR_AGE = 45


def compare_typing(patient_typing, donor_typing):
    """逐位点比较，返回 (等效相合等位数, 总等位数, 高分辨相合数, 明细)。

    5 个位点 × 2 个等位基因 = 满分 10（业界 10/10 口径）。
    高分辨完全一致记 1；仅字段前缀相容（高分辨对低分辨检索）记 0.5；不合记 0。
    """
    detail = {}
    total_pairs = 0
    matched = 0.0
    partial_pairs = 0
    high_res_pairs = 0
    for locus in LOCI:
        p = patient_typing.get(locus)
        d = donor_typing.get(locus)
        if not p or not d:
            detail[locus] = {"matched": 0, "reason": EXCL_TYPING_INSUFFICIENT}
            continue
        p_alleles = list(p.get("alleles", []))
        d_alleles = list(d.get("alleles", []))
        if len(p_alleles) != 2 or len(d_alleles) != 2:
            detail[locus] = {"matched": 0, "reason": EXCL_TYPING_INSUFFICIENT}
            continue
        total_pairs += 2
        locus_matched = 0.0
        used = set()
        for pa in p_alleles:
            best = 0
            best_idx = None
            for idx, da in enumerate(d_alleles):
                if idx in used:
                    continue
                score = _allele_pair_score(pa, da)
                if score > best:
                    best, best_idx = score, idx
            if best > 0:
                used.add(best_idx)
                if best == 2:
                    locus_matched += 1
                    high_res_pairs += 1
                else:
                    locus_matched += 0.5
                    partial_pairs += 1
        matched += locus_matched
        detail[locus] = {"matched": locus_matched, "p_alleles": p_alleles, "d_alleles": d_alleles}
    return matched, total_pairs, high_res_pairs, partial_pairs, detail


def _allele_pair_score(patient_allele, donor_allele):
    """2=高分辨全合；1=字段前缀相容（高分辨对低分辨检索）；0=不合。"""
    p = patient_allele.upper().replace(" ", "")
    d = donor_allele.upper().replace(" ", "")
    if not p or not d:
        return 0
    if p == d:
        return 2
    p_fields = p.split(":")
    d_fields = d.split(":")
    if p_fields[: len(d_fields)] == d_fields or d_fields[: len(p_fields)] == p_fields:
        return 1
    return 0


def grade_for(matched, total_pairs):
    if total_pairs == 0:
        return "ungraded", 0.0
    if float(matched).is_integer():
        grade = f"{int(matched)}/{total_pairs}"
    else:
        grade = f"{matched:g}/{total_pairs}"
    return grade, matched / total_pairs


def check_eligibility(donor, patient_deadline, case_id, commitments, consents=None):
    """返回排除理由列表（为空即合格）。

    commitments 提供该志愿者在其他病例的占用情况，但只回传布尔结论，
    不暴露任何其他病例的信息。consents 为同意范围注册表。
    """
    reasons = []
    age = donor.get("age")
    if age is None or age < MIN_DONOR_AGE or age > MAX_DONOR_AGE:
        reasons.append(EXCL_DEFERRED_AGE)
    if donor.get("medical_deferral"):
        reasons.append(EXCL_MEDICAL_DEFERRAL)
    if not donor.get("typing_complete"):
        reasons.append(EXCL_TYPING_INSUFFICIENT)
    if consents is not None and not consents.is_within(donor["donor_id"], SCOPE_INITIAL_CONTACT):
        reasons.append(EXCL_CONSENT_SCOPE)
    if commitments.is_in_post_donation_cooldown(donor["donor_id"], patient_deadline):
        reasons.append(EXCL_RECENT_DONATION)
    if commitments.has_overlapping_commitment(donor["donor_id"], case_id, patient_deadline):
        reasons.append(EXCL_COMMITTED_ELSEWHERE)
    if commitments.is_in_blackout(donor["donor_id"], patient_deadline):
        reasons.append(EXCL_UNAVAILABLE_WINDOW)
    return reasons


def rank_candidates(clock, case, donors, commitments, algorithm_version=ALGORITHM_VERSION,
                    consents=None):
    """执行排序，产出不可变快照：算法版本、依据、候选与排除者。"""
    if algorithm_version != ALGORITHM_VERSION and algorithm_version not in _LEGACY_VERSIONS:
        raise DomainError("unknown_algorithm", f"未知算法版本: {algorithm_version}")
    ranked = []
    excluded = []
    deadline = case["clinical_deadline"]
    for donor in donors:
        reasons = check_eligibility(donor, deadline, case["case_id"], commitments, consents)
        entry_base = {"donor_id": donor["donor_id"], "registry_ref": donor.get("registry_ref")}
        if reasons:
            excluded.append({**entry_base, "reasons": reasons})
            continue
        matched, total_pairs, high_res_pairs, partial_pairs, detail = compare_typing(
            case["patient_typing"], donor["typing"])
        grade, score = grade_for(matched, total_pairs)
        # 高分辨全合优先；同分按既往配合度与年龄确定性打破平局，排序规则入快照
        tie_break = (donor.get("availability_confidence", 0), -donor.get("age", 99))
        ranked.append(
            {
                **entry_base,
                "grade": grade,
                "score": round(score, 4),
                "matched_pairs": matched,
                "total_pairs": total_pairs,
                "high_res_pairs": high_res_pairs,
                "partial_pairs": partial_pairs,
                "tie_break": list(tie_break),
                "locus_detail": detail,
            }
        )
    ranked.sort(key=lambda r: (r["score"], r["high_res_pairs"], r["tie_break"]), reverse=True)
    for position, row in enumerate(ranked, start=1):
        row["rank_position"] = position
    snapshot = {
        "case_id": case["case_id"],
        "algorithm_version": algorithm_version,
        "ranked_at": iso(clock.now()),
        "clinical_deadline": deadline,
        "candidates": ranked,
        "excluded": excluded,
        "basis": "score=匹配等位比例; 次序=score,高分辨对数,配合度,年龄; 排除理由随快照保留",
    }
    return snapshot


def require_ranking(store, case_id):
    case = store.cases.get(case_id)
    if case is None:
        raise NotFound("病例", case_id)
    ranking = case.get("last_ranking")
    if ranking is None:
        raise DomainError("ranking_missing", f"病例 {case_id} 尚未完成候选排序")
    return case, ranking
