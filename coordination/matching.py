"""患者检索与候选排序。

患者侧只提交分型与临床时限；每次检索固定记录算法版本，被排除的
候选保留排除理由，供事后从审计导出中逐条核对。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

ALGORITHM_VERSION = "hla-match-v1.3"


@dataclass(frozen=True)
class SearchRequest:
    """患者侧检索请求：仅分型与临床时限，不含患者身份信息。"""

    case_id: str
    patient_alias: str
    hla: dict[str, tuple[str, ...]]
    deadline: datetime
    created_at: datetime


@dataclass(frozen=True)
class DonorProfile:
    donor_alias: str
    hla: dict[str, tuple[str, ...]]
    registered_at: datetime


@dataclass(frozen=True)
class Assessment:
    donor_alias: str
    score: int
    max_score: int
    excluded: bool
    reasons: tuple[str, ...]
    algorithm_version: str


def score_hla(patient_hla: dict, donor_hla: dict) -> tuple[int, int]:
    """按位点统计匹配等位基因数（纯合子按两个等位基因计）。"""
    score = 0
    max_score = 0
    for locus, patient_alleles in patient_hla.items():
        max_score += 2
        donor_counter = Counter(donor_hla.get(locus, ()))
        for allele, copies in Counter(patient_alleles).items():
            score += min(copies, donor_counter.get(allele, 0))
    return score, max_score


class Matcher:
    def __init__(self, version: str = ALGORITHM_VERSION):
        self.version = version

    def rank(
        self,
        request: SearchRequest,
        donors: Iterable[DonorProfile],
        eligibility: Callable[[str], Iterable[str]],
    ) -> list[Assessment]:
        """候选排序：可用者在前按匹配度降序，被排除者保留理由列于其后。

        donors 需按入库时间顺序传入，同分时先入库者优先（排序稳定）。
        """
        assessments = []
        for donor in donors:
            score, max_score = score_hla(request.hla, donor.hla)
            reasons = tuple(eligibility(donor.donor_alias))
            assessments.append(
                Assessment(
                    donor_alias=donor.donor_alias,
                    score=score,
                    max_score=max_score,
                    excluded=bool(reasons),
                    reasons=reasons,
                    algorithm_version=self.version,
                )
            )
        assessments.sort(key=lambda item: (item.excluded, -item.score))
        return assessments
