"""患者个案流程：检索、联络、锁定与确认。

一名志愿者同一时间只允许进入一个有效个案承诺；个案视图只暴露
别名，不透露志愿者在其他个案中的状态细节，避免跨案泄露。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .clock import require_aware
from .errors import CaseStateError, ContactBlocked, DonorConflict
from .matching import Assessment, Matcher, SearchRequest

CASE_STATUSES = (
    "searching",
    "candidates_ready",
    "contacting",
    "assigned",
    "confirmed",
    "collection_scheduled",
    "completed",
    "failed",
)


@dataclass
class DonationCase:
    case_id: str
    patient_alias: str
    hla: dict
    deadline: datetime
    created_at: datetime
    status: str = "searching"
    assessments: list[Assessment] = field(default_factory=list)
    assigned_donor: str | None = None
    prior_donors: list[str] = field(default_factory=list)


class DonorCommitments:
    """志愿者 → 个案的有效承诺表，是防止跨案重复采集的唯一依据。"""

    def __init__(self):
        self._active: dict[str, str] = {}

    def active_case(self, donor_alias: str) -> str | None:
        return self._active.get(donor_alias)

    def reserve(self, donor_alias: str, case_id: str) -> None:
        current = self._active.get(donor_alias)
        if current is not None and current != case_id:
            # 不透露对方个案编号，避免跨案泄露
            raise DonorConflict("志愿者已存在有效承诺，不能再进入其他个案")
        self._active[donor_alias] = case_id

    def release(self, donor_alias: str, case_id: str) -> None:
        if self._active.get(donor_alias) == case_id:
            del self._active[donor_alias]


class CaseRegistry:
    def __init__(
        self,
        *,
        consent,
        commitments: DonorCommitments,
        notifier,
        matcher: Matcher,
        audit,
        clock,
        contact_cooling: timedelta = timedelta(hours=24),
        collection_cooling: timedelta = timedelta(hours=72),
    ):
        self._consent = consent
        self._commitments = commitments
        self._notifier = notifier
        self._matcher = matcher
        self._audit = audit
        self._clock = clock
        self._contact_cooling = contact_cooling
        self._collection_cooling = collection_cooling
        self._cases: dict[str, DonationCase] = {}
        self._holds: dict[str, str] = {}

    # ---- 个案生命周期 ----

    def open_case(self, case_id: str, patient_alias: str, hla: dict, deadline: datetime) -> DonationCase:
        require_aware(deadline, "deadline")
        if case_id in self._cases:
            raise CaseStateError(f"个案编号已存在: {case_id}")
        case = DonationCase(case_id, patient_alias, hla, deadline, self._clock.now())
        self._cases[case_id] = case
        self._audit.record(
            "case.opened",
            actor="coordinator",
            case_id=case_id,
            subject=patient_alias,
            basis={"deadline": deadline},
        )
        return case

    def get(self, case_id: str) -> DonationCase:
        try:
            return self._cases[case_id]
        except KeyError:
            raise CaseStateError(f"未知个案: {case_id}") from None

    def eligibility(self, donor_alias: str) -> list[str]:
        """按最新同意范围、冷静期、承诺与健康暂挂计算排除理由。"""
        reasons: list[str] = []
        for action in ("search", "recontact"):
            ok, why = self._consent.permits(donor_alias, action)
            if not ok and why not in reasons:
                reasons.append(why)
        if self._commitments.active_case(donor_alias) is not None:
            reasons.append("committed_elsewhere")
        if donor_alias in self._holds:
            reasons.append(f"hold:{self._holds[donor_alias]}")
        return reasons

    def run_search(self, case_id: str, donor_pool) -> list[Assessment]:
        case = self.get(case_id)
        request = SearchRequest(
            case_id=case.case_id,
            patient_alias=case.patient_alias,
            hla=case.hla,
            deadline=case.deadline,
            created_at=case.created_at,
        )
        assessments = self._matcher.rank(request, donor_pool, self.eligibility)
        case.assessments = assessments
        case.status = "candidates_ready"
        self._audit.record(
            "match.ranked",
            actor="matcher",
            case_id=case_id,
            basis={
                "algorithm_version": self._matcher.version,
                "candidate_count": len(assessments),
                "excluded": {
                    item.donor_alias: list(item.reasons)
                    for item in assessments
                    if item.excluded
                },
            },
        )
        return assessments

    # ---- 联络：遵守最新同意范围与冷静期，重试幂等 ----

    def contact_donor(self, case_id: str, donor_alias: str, round_no: int = 1) -> bool:
        """联络志愿者；返回本次是否真正发出通知（重试返回 False）。"""
        case = self.get(case_id)
        key = f"contact:{case_id}:{donor_alias}:r{round_no}"
        if self._notifier.has(key):
            self._notifier.notify(key, case_id=case_id, donor_alias=donor_alias, kind="contact")
            return False
        ok, why = self._consent.permits(donor_alias, "recontact")
        if not ok:
            self._audit.record(
                "contact.blocked",
                actor="coordinator",
                case_id=case_id,
                subject=donor_alias,
                basis={"reason": why, "round": round_no},
            )
            raise ContactBlocked(why)
        self._notifier.notify(
            key,
            case_id=case_id,
            donor_alias=donor_alias,
            kind="contact",
            detail=f"第{round_no}轮联络",
        )
        self._consent.set_cooling(
            donor_alias,
            self._clock.now() + self._contact_cooling,
            "contact_interval",
            actor="coordinator",
        )
        if case.status == "candidates_ready":
            case.status = "contacting"
        self._audit.record(
            "contact.attempted",
            actor="coordinator",
            case_id=case_id,
            subject=donor_alias,
            basis={"round": round_no},
        )
        return True

    # ---- 锁定与确认 ----

    def assign_donor(self, case_id: str, donor_alias: str) -> None:
        case = self.get(case_id)
        assessment = next(
            (item for item in case.assessments if item.donor_alias == donor_alias), None
        )
        if assessment is None:
            raise CaseStateError("志愿者不在本案候选列表中")
        if assessment.excluded:
            raise CaseStateError("候选存在排除理由: " + ",".join(assessment.reasons))
        fresh = self.eligibility(donor_alias)
        if fresh:
            raise CaseStateError("候选当前不可用: " + ",".join(fresh))
        self._commitments.reserve(donor_alias, case_id)
        case.assigned_donor = donor_alias
        case.prior_donors.append(donor_alias)
        case.status = "assigned"
        self._audit.record(
            "case.donor_assigned",
            actor="coordinator",
            case_id=case_id,
            subject=donor_alias,
            basis={
                "score": assessment.score,
                "max_score": assessment.max_score,
                "algorithm_version": assessment.algorithm_version,
            },
        )

    def confirm_donation(self, case_id: str) -> None:
        """确认捐献：要求采集同意有效且采集冷静期已满。"""
        case = self.get(case_id)
        donor = case.assigned_donor
        if donor is None:
            raise CaseStateError("个案尚未锁定志愿者")
        ok, why = self._consent.permits(donor, "collection")
        if not ok:
            raise CaseStateError(f"采集未获同意: {why}")
        granted = self._consent.granted_at(donor, "collection")
        now = self._clock.now()
        if granted is not None and now < granted + self._collection_cooling:
            self._audit.record(
                "case.confirm_blocked",
                actor="coordinator",
                case_id=case_id,
                subject=donor,
                basis={
                    "reason": "collection_cooling",
                    "cooling_until": granted + self._collection_cooling,
                },
            )
            raise CaseStateError("采集冷静期未满，暂不能确认捐献")
        case.status = "confirmed"
        self._audit.record(
            "case.donation_confirmed",
            actor="coordinator",
            case_id=case_id,
            subject=donor,
            basis={},
        )

    def release_assignment(self, case_id: str, reason: str) -> str | None:
        """解除当前锁定（撤回/体检异常/替补前调用），返回被释放的志愿者别名。"""
        case = self.get(case_id)
        donor = case.assigned_donor
        if donor is None:
            return None
        self._commitments.release(donor, case_id)
        case.assigned_donor = None
        case.status = "candidates_ready"
        self._audit.record(
            "case.assignment_released",
            actor="system",
            case_id=case_id,
            subject=donor,
            basis={"reason": reason},
        )
        return donor

    def mark_hold(self, donor_alias: str, reason: str, *, actor: str = "system") -> None:
        self._holds[donor_alias] = reason
        self._audit.record(
            "donor.hold", actor=actor, subject=donor_alias, basis={"reason": reason}
        )

    def mark_collection_scheduled(self, case_id: str) -> None:
        case = self.get(case_id)
        if case.status != "confirmed":
            raise CaseStateError("个案尚未确认捐献，不能进入采集排程")
        case.status = "collection_scheduled"

    # ---- 视图：只含本案数据与别名 ----

    def case_view(self, case_id: str) -> dict:
        case = self.get(case_id)
        return {
            "case_id": case.case_id,
            "patient_alias": case.patient_alias,
            "status": case.status,
            "deadline": case.deadline.isoformat(),
            "assigned_donor": case.assigned_donor,
            "candidates": [
                {
                    "donor_alias": item.donor_alias,
                    "score": item.score,
                    "max_score": item.max_score,
                    "excluded": item.excluded,
                    "reasons": list(item.reasons),
                    "algorithm_version": item.algorithm_version,
                }
                for item in case.assessments
            ],
        }
