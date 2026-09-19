"""航班延误、体检异常与志愿者撤回触发的有优先级替代方案。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .clock import require_aware
from .errors import CaseStateError


@dataclass(frozen=True)
class Alternative:
    rank: int
    action: str
    feasible: bool
    reason: str


@dataclass(frozen=True)
class Substitution:
    case_id: str
    previous_donor: str | None
    new_donor: str | None
    basis: dict


class DisruptionService:
    def __init__(
        self,
        *,
        cases,
        consent,
        scheduler,
        board,
        audit,
        clock,
        viability: timedelta = timedelta(hours=36),
        withdrawal_cooling: timedelta = timedelta(days=180),
        ground_transfer: timedelta = timedelta(hours=10),
    ):
        self._cases = cases
        self._consent = consent
        self._scheduler = scheduler
        self._board = board
        self._audit = audit
        self._clock = clock
        self._viability = viability
        self._withdrawal_cooling = withdrawal_cooling
        self._ground_transfer = ground_transfer

    # ---- 航班延误 ----

    def handle_flight_delay(self, case_id: str, new_eta: datetime, *, actor: str = "transport") -> list[Alternative]:
        """按有效时间窗生成有优先级的替代方案。"""
        require_aware(new_eta, "new_eta")
        eta_utc = new_eta.astimezone(timezone.utc)
        deadline = self._infusion_deadline(case_id)
        ground_eta = self._clock.now() + self._ground_transfer
        alternatives = [
            Alternative(
                1,
                "rebook_flight",
                eta_utc <= deadline,
                f"改签后预计 {eta_utc.isoformat()} 送达，有效时间窗截止 {deadline.isoformat()}",
            ),
            Alternative(
                2,
                "reroute_ground_transfer",
                ground_eta <= deadline,
                f"改陆空联运预计 {ground_eta.isoformat()} 送达",
            ),
            Alternative(
                3,
                "extend_transplant_window",
                True,
                "申请移植医院延长有效时间窗，需书面确认，优先级最低",
            ),
        ]
        self._audit.record(
            "disruption.flight_delay",
            actor=actor,
            case_id=case_id,
            basis={
                "new_eta": eta_utc,
                "deadline": deadline,
                "alternatives": [
                    {"rank": item.rank, "action": item.action, "feasible": item.feasible}
                    for item in alternatives
                ],
            },
        )
        return alternatives

    def _infusion_deadline(self, case_id: str) -> datetime:
        infusion = self._board.find(case_id, "infusion_started")
        if infusion is not None:
            return infusion.window.end
        plan = self._scheduler.plan_for_case(case_id)
        if plan is not None:
            return plan.window.end + self._viability
        raise CaseStateError("个案尚无有效时间窗，无法评估延误影响")

    # ---- 体检异常 ----

    def handle_exam_anomaly(self, case_id: str, donor_alias: str, detail: str) -> Substitution:
        """体检异常：志愿者转健康暂挂，释放承诺并按候选顺序替补。"""
        reason = f"exam_anomaly:{detail}"
        self._cases.mark_hold(donor_alias, reason, actor="collection_hospital")
        previous = self._cases.release_assignment(case_id, reason)
        self._scheduler.cancel(case_id, reason, actor="collection_hospital")
        self._audit.record(
            "disruption.exam_anomaly",
            actor="collection_hospital",
            case_id=case_id,
            subject=donor_alias,
            basis={"detail": detail},
        )
        return self._promote_substitute(case_id, reason, previous or donor_alias)

    # ---- 志愿者撤回 ----

    def handle_withdrawal(self, case_id: str, donor_alias: str, reason: str = "donor_withdrawal") -> Substitution:
        """撤回：进入长期冷静期，释放承诺与采集计划，并按候选顺序替补。"""
        until = self._clock.now() + self._withdrawal_cooling
        self._consent.set_cooling(donor_alias, until, "withdrawal", actor="system")
        previous = self._cases.release_assignment(case_id, reason)
        self._scheduler.cancel(case_id, reason)
        self._audit.record(
            "disruption.donor_withdrawal",
            actor="system",
            case_id=case_id,
            subject=donor_alias,
            basis={"cooling_until": until},
        )
        return self._promote_substitute(case_id, reason, previous or donor_alias)

    # ---- 替补 ----

    def _promote_substitute(self, case_id: str, reason: str, previous_donor: str | None) -> Substitution:
        case = self._cases.get(case_id)
        for assessment in case.assessments:
            if assessment.excluded:
                continue
            donor = assessment.donor_alias
            if donor in case.prior_donors:
                continue
            if self._cases.eligibility(donor):
                continue  # 以最新同意范围、冷静期与承诺为准
            self._cases.assign_donor(case_id, donor)
            basis = {
                "reason": reason,
                "previous_donor": previous_donor,
                "algorithm_version": assessment.algorithm_version,
                "score": assessment.score,
                "max_score": assessment.max_score,
            }
            self._audit.record(
                "case.substituted", actor="system", case_id=case_id, subject=donor, basis=basis
            )
            return Substitution(case_id, previous_donor, donor, basis)
        self._audit.record(
            "case.substitution_failed",
            actor="system",
            case_id=case_id,
            basis={"reason": reason},
        )
        return Substitution(case_id, previous_donor, None, {"reason": reason})
