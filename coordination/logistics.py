"""采集排程与里程碑协同。

确认捐献后，采集医院、移植医院与运输方围绕有效时间窗上报里程碑；
所有时刻统一归一到 UTC，跨时区上报不会错位。采集排程对同案重试
幂等，且一名志愿者不得同时存在两份有效采集计划（防止重复采集）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .clock import require_aware
from .errors import CollectionConflict, MilestoneError


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime

    def __post_init__(self):
        require_aware(self.start, "window.start")
        require_aware(self.end, "window.end")
        if not self.start < self.end:
            raise ValueError("时间窗起点必须早于终点")

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment <= self.end

    def as_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True)
class CollectionPlan:
    plan_id: str
    case_id: str
    donor_alias: str
    window: TimeWindow
    created_at: datetime


class CollectionScheduler:
    """采集排程：同案重试幂等，同人不得重复采集。"""

    def __init__(self, clock, audit):
        self._clock = clock
        self._audit = audit
        self._by_case: dict[str, CollectionPlan] = {}
        self._by_donor: dict[str, CollectionPlan] = {}

    def schedule(self, case_id: str, donor_alias: str, window: TimeWindow) -> CollectionPlan:
        existing = self._by_case.get(case_id)
        if existing is not None:
            if existing.donor_alias != donor_alias:
                raise CollectionConflict("个案已排定其他志愿者的采集计划")
            return existing  # 重试返回原计划，不重复采集
        other = self._by_donor.get(donor_alias)
        if other is not None and other.case_id != case_id:
            raise CollectionConflict("志愿者已存在其他个案的采集计划")
        plan = CollectionPlan(f"CP-{case_id}", case_id, donor_alias, window, self._clock.now())
        self._by_case[case_id] = plan
        self._by_donor[donor_alias] = plan
        self._audit.record(
            "collection.scheduled",
            actor="coordinator",
            case_id=case_id,
            subject=donor_alias,
            basis={"plan_id": plan.plan_id, "window": window.as_dict()},
        )
        return plan

    def cancel(self, case_id: str, reason: str, *, actor: str = "system") -> CollectionPlan | None:
        plan = self._by_case.pop(case_id, None)
        if plan is None:
            return None
        self._by_donor.pop(plan.donor_alias, None)
        self._audit.record(
            "collection.cancelled",
            actor=actor,
            case_id=case_id,
            subject=plan.donor_alias,
            basis={"plan_id": plan.plan_id, "reason": reason},
        )
        return plan

    def plan_for_case(self, case_id: str) -> CollectionPlan | None:
        return self._by_case.get(case_id)

    def plan_for_donor(self, donor_alias: str) -> CollectionPlan | None:
        return self._by_donor.get(donor_alias)


MILESTONE_OWNERS = {
    "collection_hospital": ("exam_cleared", "collection_started", "collection_completed"),
    "transport": ("picked_up", "departed", "arrived", "delivered"),
    "transplant_hospital": ("received", "infusion_started", "infusion_completed"),
}


@dataclass
class Milestone:
    case_id: str
    party: str
    kind: str
    window: TimeWindow
    reported_at: datetime | None = None
    status: str = "pending"  # pending / met / breached


class MilestoneBoard:
    def __init__(self, clock, audit):
        self._clock = clock
        self._audit = audit
        self._plans: dict[str, dict[str, Milestone]] = {}

    def plan(self, case_id: str, specs) -> None:
        """规划个案里程碑：specs 为 (责任方, 里程碑, 有效时间窗) 列表。"""
        board = self._plans.setdefault(case_id, {})
        for party, kind, window in specs:
            if kind not in MILESTONE_OWNERS.get(party, ()):
                raise MilestoneError(f"{party} 无权上报里程碑 {kind}")
            board[kind] = Milestone(case_id, party, kind, window)
        self._audit.record(
            "milestone.planned",
            actor="coordinator",
            case_id=case_id,
            basis={"kinds": [kind for _, kind, _ in specs]},
        )

    def find(self, case_id: str, kind: str) -> Milestone | None:
        return self._plans.get(case_id, {}).get(kind)

    def report(self, case_id: str, party: str, kind: str, at: datetime) -> Milestone:
        """上报里程碑；at 可带任意时区，归一 UTC 后按有效时间窗判定。"""
        require_aware(at, "at")
        milestone = self.find(case_id, kind)
        if milestone is None:
            raise MilestoneError(f"个案 {case_id} 未规划里程碑 {kind}")
        if milestone.party != party:
            raise MilestoneError("里程碑归属方不符")
        if milestone.reported_at is not None:
            raise MilestoneError("里程碑已上报，重复上报被拒绝")
        at_utc = at.astimezone(timezone.utc)
        milestone.reported_at = at_utc
        milestone.status = "met" if milestone.window.contains(at_utc) else "breached"
        self._audit.record(
            "milestone.reported",
            actor=party,
            case_id=case_id,
            basis={
                "kind": kind,
                "reported_at": at_utc,
                "status": milestone.status,
                "window": milestone.window.as_dict(),
            },
        )
        return milestone

    def adjust_window(
        self, case_id: str, kind: str, new_window: TimeWindow, reason: str, *, actor: str = "coordinator"
    ) -> Milestone:
        """在替代方案确定后调整有效时间窗，保留调整依据。"""
        milestone = self.find(case_id, kind)
        if milestone is None:
            raise MilestoneError(f"个案 {case_id} 未规划里程碑 {kind}")
        old_window = milestone.window
        milestone.window = new_window
        if milestone.reported_at is not None:
            milestone.status = "met" if new_window.contains(milestone.reported_at) else "breached"
        self._audit.record(
            "milestone.window_adjusted",
            actor=actor,
            case_id=case_id,
            basis={
                "kind": kind,
                "reason": reason,
                "old_window": old_window.as_dict(),
                "new_window": new_window.as_dict(),
            },
        )
        return milestone

    def timeline(self, case_id: str) -> list[Milestone]:
        return list(self._plans.get(case_id, {}).values())

    def breaches(self, case_id: str) -> list[Milestone]:
        return [item for item in self.timeline(case_id) if item.status == "breached"]
