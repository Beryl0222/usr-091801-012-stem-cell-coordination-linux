"""捐献工作流：状态机、里程碑时效窗、跨时区运输与优先级替代方案。

- 每个病例按候选记录状态；活跃候选人退出（撤回/体检异常/运输超时）时，
  按排序快照激活下一顺位候选，替补依据（理由、优先级、算法版本）入审计。
- 里程碑由对应角色上报，时间按 UTC 绝对值比较，支持跨时区（偏移量保留）。
- 产品时效窗自采集完成起算；航班延误实时重算剩余时效，预测超时即生成
  有优先级的替代方案。
- 采集全局幂等且全局唯一：同一病例重试不重复采集；志愿者在任何病例
  实际采集后进入捐献后冷却，不可能在另一病例被二次采集。
"""

from datetime import timedelta

from .clock import as_utc, iso
from .errors import Conflict, DomainError, NotFound
from .matching import ALGORITHM_VERSION

# ---- 候选状态 --------------------------------------------------------------

ST_RANKED = "ranked"
ST_CONTACTED = "contacted"
ST_CONFIRMATION_PENDING = "confirmation_pending"
ST_CONFIRMED = "confirmed"
ST_EXAM_SCHEDULED = "exam_scheduled"
ST_EXAM_PASSED = "exam_passed"
ST_EXAM_ABNORMAL = "exam_abnormal"
ST_COLLECTION_SCHEDULED = "collection_scheduled"
ST_COLLECTED = "collected"
ST_IN_TRANSIT = "in_transit"
ST_DELIVERED = "delivered"
ST_INFUSED = "infused"
ST_COMPLETED = "completed"
ST_WITHDRAWN = "withdrawn"
ST_SUBSTITUTED = "substituted"

TERMINAL_STATES = frozenset({ST_COMPLETED, ST_WITHDRAWN, ST_SUBSTITUTED})

ALLOWED_TRANSITIONS = {
    ST_RANKED: {ST_CONTACTED, ST_SUBSTITUTED},
    ST_CONTACTED: {ST_CONFIRMATION_PENDING, ST_WITHDRAWN, ST_SUBSTITUTED},
    ST_CONFIRMATION_PENDING: {ST_CONFIRMED, ST_WITHDRAWN, ST_SUBSTITUTED},
    ST_CONFIRMED: {ST_EXAM_SCHEDULED, ST_WITHDRAWN, ST_SUBSTITUTED},
    ST_EXAM_SCHEDULED: {ST_EXAM_PASSED, ST_EXAM_ABNORMAL, ST_WITHDRAWN, ST_SUBSTITUTED},
    ST_EXAM_ABNORMAL: {ST_EXAM_SCHEDULED, ST_SUBSTITUTED},
    ST_EXAM_PASSED: {ST_COLLECTION_SCHEDULED, ST_WITHDRAWN, ST_SUBSTITUTED},
    ST_COLLECTION_SCHEDULED: {ST_COLLECTED, ST_WITHDRAWN, ST_SUBSTITUTED},
    ST_COLLECTED: {ST_IN_TRANSIT},
    ST_IN_TRANSIT: {ST_DELIVERED},
    ST_DELIVERED: {ST_INFUSED},
    ST_INFUSED: {ST_COMPLETED},
}

# 产品离体后有效时间窗（小时）：外周血造血干细胞冷藏运输的常用时限
PRODUCT_WINDOW_HOURS = 24
# 采集后捐献冷却期（天）：冷却期内不可能在任何病例再次采集
POST_DONATION_COOLDOWN_DAYS = 90
# 已承诺捐献的窗口向外扩展的互斥余量（天）
COMMITMENT_MARGIN_DAYS = 14

# ---- 事件触发的优先级替代方案 ----------------------------------------------

TRIGGER_FLIGHT_DELAY = "flight_delay"
TRIGGER_EXAM_ABNORMAL = "exam_abnormal"
TRIGGER_DONOR_WITHDRAWAL = "donor_withdrawal"
TRIGGER_PRODUCT_WINDOW_RISK = "product_window_risk"

PLAN_REBOOK_FLIGHT = "rebook_next_flight"          # 优先级1：改签最近航班
PLAN_ESCORT_HAND_CARRY = "medical_escort_handcarry"  # 优先级2：医护手提改签他司
PLAN_GROUND_EMERGENCY = "ground_emergency_transport"  # 优先级3：紧急地面转运
PLAN_TARGETED_REEXAM = "targeted_reexam"           # 优先级1：针对性复检
PLAN_SUBSTITUTE_BACKUP = "substitute_backup_donor"  # 优先级2：激活顺位候选
PLAN_RERANK_SEARCH = "rerank_broader_search"       # 优先级3：扩大检索重排

CONTINGENCY_PLANS = {
    TRIGGER_FLIGHT_DELAY: [PLAN_REBOOK_FLIGHT, PLAN_ESCORT_HAND_CARRY, PLAN_GROUND_EMERGENCY],
    TRIGGER_PRODUCT_WINDOW_RISK: [PLAN_REBOOK_FLIGHT, PLAN_ESCORT_HAND_CARRY, PLAN_GROUND_EMERGENCY],
    TRIGGER_EXAM_ABNORMAL: [PLAN_TARGETED_REEXAM, PLAN_SUBSTITUTE_BACKUP, PLAN_RERANK_SEARCH],
    TRIGGER_DONOR_WITHDRAWAL: [PLAN_SUBSTITUTE_BACKUP, PLAN_RERANK_SEARCH],
}


class CommitmentLedger:
    """跨病例的志愿者占用：防止相互干扰与重复采集，只暴露布尔结论。"""

    def __init__(self):
        # donor_id -> [{case_id, window_start, window_end}]
        self._commitments = {}
        # donor_id -> 实际采集记录（全局唯一，含病例与时间）
        self._collections = {}
        # donor_id -> [(start, end)] 志愿者自报不可用窗口
        self._blackouts = {}

    def add_blackout(self, donor_id, start, end):
        start, end = as_utc(start), as_utc(end)
        if end <= start:
            raise DomainError("bad_window", "不可用窗口结束时间必须晚于开始时间")
        self._blackouts.setdefault(donor_id, []).append((start, end))

    def register_commitment(self, donor_id, case_id, window_start, window_end):
        window_start, window_end = as_utc(window_start), as_utc(window_end)
        if window_end <= window_start:
            raise DomainError("bad_window", "采集窗口结束时间必须晚于开始时间")
        self._commitments.setdefault(donor_id, []).append(
            {"case_id": case_id, "window_start": window_start, "window_end": window_end}
        )

    def release_commitment(self, donor_id, case_id):
        """志愿者撤回/替补后释放承诺，使其可以进入其他病例流程。"""
        self._commitments[donor_id] = [
            c for c in self._commitments.get(donor_id, []) if c["case_id"] != case_id
        ]

    def mark_collected(self, clock, donor_id, case_id, at=None):
        """登记实际采集；任何病例已采集则拒绝二次采集（不回传另一病例信息）。"""
        prior = self._collections.get(donor_id)
        if prior is not None:
            raise Conflict(
                "donor_already_collected",
                "该志愿者已完成采集，不能重复采集",
                {"cooldown_days": POST_DONATION_COOLDOWN_DAYS},
            )
        when = as_utc(at) if at else clock.now()
        self._collections[donor_id] = {"case_id": case_id, "at": iso(when)}

    def is_in_post_donation_cooldown(self, donor_id, at):
        prior = self._collections.get(donor_id)
        if prior is None:
            return False
        return as_utc(at) < as_utc(prior["at"]) + timedelta(days=POST_DONATION_COOLDOWN_DAYS)

    def has_overlapping_commitment(self, donor_id, case_id, at):
        """at 落在该志愿者对*其他*病例的承诺窗口（含余量）内即互斥。"""
        at = as_utc(at)
        margin = timedelta(days=COMMITMENT_MARGIN_DAYS)
        for c in self._commitments.get(donor_id, []):
            if c["case_id"] == case_id:
                continue
            if c["window_start"] - margin <= at <= c["window_end"] + margin:
                return True
        return False

    def is_in_blackout(self, donor_id, at):
        at = as_utc(at)
        return any(start <= at <= end for start, end in self._blackouts.get(donor_id, []))

    def collection_of(self, donor_id):
        record = self._collections.get(donor_id)
        return dict(record) if record else None

    def rollback_collection(self, donor_id, case_id):
        """采集状态迁移失败时回滚占用登记（仅限本病例刚写入的记录）。"""
        record = self._collections.get(donor_id)
        if record and record["case_id"] == case_id:
            self._collections.pop(donor_id, None)

    def snapshot(self):
        return {
            "commitments": {
                k: [
                    {"case_id": c["case_id"], "window_start": iso(c["window_start"]),
                     "window_end": iso(c["window_end"])}
                    for c in v
                ]
                for k, v in self._commitments.items()
            },
            "collections": {k: dict(v) for k, v in self._collections.items()},
            "blackouts": {
                k: [[iso(s), iso(e)] for s, e in v] for k, v in self._blackouts.items()
            },
        }

    def restore(self, data):
        self._commitments = {
            k: [
                {"case_id": c["case_id"], "window_start": as_utc(c["window_start"]),
                 "window_end": as_utc(c["window_end"])}
                for c in v
            ]
            for k, v in (data.get("commitments") or {}).items()
        }
        self._collections = {k: dict(v) for k, v in (data.get("collections") or {}).items()}
        self._blackouts = {
            k: [(as_utc(s), as_utc(e)) for s, e in v]
            for k, v in (data.get("blackouts") or {}).items()
        }


class CaseWorkflow:
    """单个病例的候选状态、里程碑与替代方案。"""

    def __init__(self, clock, audit, commitments):
        self.clock = clock
        self.audit = audit
        self.commitments = commitments

    # -- 候选初始化 ----------------------------------------------------------

    def seed_candidates(self, case, ranking):
        case["candidates"] = {
            row["donor_id"]: {
                "donor_id": row["donor_id"],
                "state": ST_RANKED,
                "rank_position": row["rank_position"],
                "grade": row["grade"],
                "history": [],
                "contingencies": [],
            }
            for row in ranking["candidates"]
        }
        case["excluded_donors"] = list(ranking["excluded"])
        case["active_donor_id"] = ranking["candidates"][0]["donor_id"] if ranking["candidates"] else None
        case["backup_chain"] = [row["donor_id"] for row in ranking["candidates"][1:]]
        case["milestones"] = []
        case["delay_events"] = []
        case["product_window"] = None
        case["action_index"] = {}
        case["collection_window"] = None

    # -- 通用工具 ------------------------------------------------------------

    def _candidate(self, case, donor_id):
        cand = case.get("candidates", {}).get(donor_id)
        if cand is None:
            raise NotFound("病例候选", f"{case['case_id']}/{donor_id}")
        return cand

    def _active(self, case):
        if not case.get("active_donor_id"):
            raise DomainError("no_active_candidate", "该病例没有可推进的活跃候选人")
        return case["active_donor_id"], self._candidate(case, case["active_donor_id"])

    def _transition(self, case, cand, target, actor, reason=None, details=None):
        source = cand["state"]
        allowed = ALLOWED_TRANSITIONS.get(source, set())
        if target not in allowed:
            raise Conflict(
                "illegal_transition",
                f"候选 {cand['donor_id']} 不能从 {source} 迁移到 {target}",
                {"from": source, "to": target, "allowed": sorted(allowed)},
            )
        cand["state"] = target
        event = {
            "from": source,
            "to": target,
            "at": iso(self.clock.now()),
            "by": actor.id,
            "reason": reason,
            "details": details or {},
        }
        cand["history"].append(event)
        self.audit.record(
            self.clock,
            "candidate_transition",
            actor=actor.id,
            case_id=case["case_id"],
            subject=cand["donor_id"],
            details=event,
        )
        return event

    def _idem_action(self, case, idempotency_key):
        """动作幂等：同一键的重试返回首次结果而不重复执行。

        返回 (已有结果 or None, 记录函数)。
        """
        if not idempotency_key:
            return None, lambda result: None
        existing = case["action_index"].get(idempotency_key)
        return existing, lambda result: case["action_index"].update({idempotency_key: dict(result)})

    # -- 推进动作 ------------------------------------------------------------

    def mark_contacted(self, case, actor, donor_id, idempotency_key):
        existing, remember = self._idem_action(case, idempotency_key)
        if existing:
            return {**existing, "deduped": True}
        cand = self._candidate(case, donor_id)
        self._transition(case, cand, ST_CONTACTED, actor, reason="initial_notice_delivered")
        result = {"donor_id": donor_id, "state": cand["state"]}
        remember(result)
        return {**result, "deduped": False}

    def request_confirmation(self, case, actor, donor_id, idempotency_key):
        existing, remember = self._idem_action(case, idempotency_key)
        if existing:
            return {**existing, "deduped": True}
        cand = self._candidate(case, donor_id)
        self._transition(case, cand, ST_CONFIRMATION_PENDING, actor, reason="confirmation_requested")
        result = {"donor_id": donor_id, "state": cand["state"]}
        remember(result)
        return {**result, "deduped": False}

    def confirm_donation(self, case, actor, donor_id, collection_window, idempotency_key):
        existing, remember = self._idem_action(case, idempotency_key)
        if existing:
            return {**existing, "deduped": True}
        cand = self._candidate(case, donor_id)
        window = {"start": iso(as_utc(collection_window["start"])),
                  "end": iso(as_utc(collection_window["end"]))}
        case["collection_window"] = window
        self._transition(case, cand, ST_CONFIRMED, actor, reason="donor_confirmed", details={"window": window})
        self.commitments.register_commitment(
            donor_id, case["case_id"], window["start"], window["end"]
        )
        self.audit.record(
            self.clock, "commitment_registered", actor=actor.id,
            case_id=case["case_id"], subject=donor_id, details={"window": window},
        )
        result = {"donor_id": donor_id, "state": cand["state"], "window": window}
        remember(result)
        return {**result, "deduped": False}

    def withdraw(self, case, actor, donor_id, reason, idempotency_key=None):
        """志愿者撤回（任一非终态）。释放承诺并触发替补方案。"""
        existing, remember = self._idem_action(case, idempotency_key or f"withdraw:{donor_id}")
        if existing:
            return {**existing, "deduped": True}
        cand = self._candidate(case, donor_id)
        self._transition(case, cand, ST_WITHDRAWN, actor, reason=reason)
        self.commitments.release_commitment(donor_id, case["case_id"])
        self.audit.record(
            self.clock, "commitment_released", actor=actor.id,
            case_id=case["case_id"], subject=donor_id, details={"reason": reason},
        )
        result = {"donor_id": donor_id, "state": cand["state"]}
        remember(result)
        if case.get("active_donor_id") == donor_id:
            plans = self.open_contingency(case, actor, TRIGGER_DONOR_WITHDRAWAL, reason)
            result["contingency"] = plans
        return {**result, "deduped": False}

    def schedule_exam(self, case, actor, donor_id, scheduled_at, idempotency_key):
        existing, remember = self._idem_action(case, idempotency_key)
        if existing:
            return {**existing, "deduped": True}
        cand = self._candidate(case, donor_id)
        self._transition(
            case, cand, ST_EXAM_SCHEDULED, actor,
            details={"scheduled_at": iso(as_utc(scheduled_at))},
        )
        result = {"donor_id": donor_id, "state": cand["state"], "scheduled_at": iso(as_utc(scheduled_at))}
        remember(result)
        return {**result, "deduped": False}

    def report_exam(self, case, actor, donor_id, passed, findings=None, idempotency_key=None):
        """采集医院上报体检结论；异常触发有优先级的替代方案。"""
        existing, remember = self._idem_action(
            case, idempotency_key or f"exam:{donor_id}:{iso(self.clock.now())}"
        )
        if existing:
            return {**existing, "deduped": True}
        cand = self._candidate(case, donor_id)
        findings = findings or []
        if passed:
            self._transition(case, cand, ST_EXAM_PASSED, actor, reason="exam_passed", details={"findings": findings})
            result = {"donor_id": donor_id, "state": cand["state"]}
        else:
            self._transition(
                case, cand, ST_EXAM_ABNORMAL, actor, reason="exam_abnormal",
                details={"findings": findings},
            )
            plans = self.open_contingency(case, actor, TRIGGER_EXAM_ABNORMAL, "体检异常", findings=findings)
            result = {"donor_id": donor_id, "state": cand["state"], "contingency": plans}
        remember(result)
        return {**result, "deduped": False}

    def resolve_reexam(self, case, actor, donor_id, passed, findings=None, idempotency_key=None):
        """针对性复检结论：通过则回到体检通过，不通过则该候选标记替补。"""
        cand = self._candidate(case, donor_id)
        if cand["state"] != ST_EXAM_ABNORMAL:
            raise Conflict("not_in_reexam", "候选不处于体检异常待复检状态")
        if passed:
            self._transition(case, cand, ST_EXAM_SCHEDULED, actor, reason="reexam_scheduled_return")
            self._transition(case, cand, ST_EXAM_PASSED, actor, reason="reexam_passed",
                             details={"findings": findings or []})
            return {"donor_id": donor_id, "state": cand["state"], "deduped": False}
        self._transition(case, cand, ST_SUBSTITUTED, actor, reason="reexam_failed",
                         details={"findings": findings or []})
        plans = self.open_contingency(case, actor, TRIGGER_EXAM_ABNORMAL, "复检仍异常")
        return {"donor_id": donor_id, "state": cand["state"], "contingency": plans, "deduped": False}

    def schedule_collection(self, case, actor, donor_id, scheduled_at, idempotency_key):
        existing, remember = self._idem_action(case, idempotency_key)
        if existing:
            return {**existing, "deduped": True}
        cand = self._candidate(case, donor_id)
        self._transition(case, cand, ST_COLLECTION_SCHEDULED, actor,
                         details={"scheduled_at": iso(as_utc(scheduled_at))})
        result = {"donor_id": donor_id, "state": cand["state"], "scheduled_at": iso(as_utc(scheduled_at))}
        remember(result)
        return {**result, "deduped": False}

    def record_collection(self, case, actor, donor_id, collected_at=None, idempotency_key=None):
        """登记采集完成：全局唯一，重试幂等，启动产品时效窗。"""
        key = idempotency_key or f"collect:{case['case_id']}"
        existing, remember = self._idem_action(case, key)
        if existing:
            return {**existing, "deduped": True}
        cand = self._candidate(case, donor_id)
        when = as_utc(collected_at) if collected_at else self.clock.now()
        # 全局二次采集拦截（跨病例），错误不含另一病例任何信息
        self.commitments.mark_collected(self.clock, donor_id, case["case_id"], at=when)
        try:
            self._transition(case, cand, ST_COLLECTED, actor, reason="collection_completed")
        except Conflict:
            # 状态不允许则回滚占用登记，避免半副作用
            self.commitments.rollback_collection(donor_id, case["case_id"])
            raise
        when = as_utc(collected_at) if collected_at else self.clock.now()
        window = {"start": iso(when), "deadline": iso(when + timedelta(hours=PRODUCT_WINDOW_HOURS)),
                  "valid_hours": PRODUCT_WINDOW_HOURS}
        case["product_window"] = window
        milestone = self._milestone(case, actor, "collection_completed", donor_id, when=when,
                                    details={"product_window": window})
        result = {"donor_id": donor_id, "state": cand["state"], "product_window": window,
                  "milestone_id": milestone["milestone_id"]}
        remember(result)
        return {**result, "deduped": False}

    # -- 运输与产品时效窗 -----------------------------------------------------

    def record_pickup(self, case, actor, donor_id, at=None, idempotency_key=None):
        return self._transit_milestone(case, actor, donor_id, ST_IN_TRANSIT, "courier_pickup",
                                       at, idempotency_key)

    def report_flight_delay(self, case, actor, flight_no, delay_minutes, projected_arrival_at,
                            idempotency_key=None):
        """航班延误：重算产品时效余量；预测超时即升级为时效风险方案。"""
        key = idempotency_key or f"delay:{flight_no}:{iso(self.clock.now())}"
        existing, remember = self._idem_action(case, key)
        if existing:
            return {**existing, "deduped": True}
        window = case.get("product_window")
        if window is None:
            raise Conflict("no_product_window", "产品尚未采集，不接受运输延误事件")
        arrival = as_utc(projected_arrival_at)
        remaining = as_utc(window["deadline"]) - arrival
        event = {
            "flight_no": flight_no,
            "delay_minutes": int(delay_minutes),
            "reported_at": iso(self.clock.now()),
            "projected_arrival_at": iso(arrival),
            "window_remaining_minutes_at_arrival": int(remaining.total_seconds() // 60),
        }
        case["delay_events"].append(event)
        self.audit.record(self.clock, "flight_delay", actor=actor.id, case_id=case["case_id"],
                          subject=flight_no, details=event)
        trigger = TRIGGER_FLIGHT_DELAY
        if remaining < timedelta(0):
            trigger = TRIGGER_PRODUCT_WINDOW_RISK
            event["breach"] = True
        plans = self.open_contingency(case, actor, trigger,
                                      f"航班 {flight_no} 延误 {delay_minutes} 分钟",
                                      delay=event)
        result = {"delay": event, "contingency": plans}
        remember(result)
        return {**result, "deduped": False}

    def record_delivery(self, case, actor, at=None, idempotency_key=None):
        donor_id, cand = self._active(case)
        key = idempotency_key or f"delivery:{case['case_id']}"
        existing, remember = self._idem_action(case, key)
        if existing:
            return {**existing, "deduped": True}
        when = as_utc(at) if at else self.clock.now()
        window = case.get("product_window")
        if window and when > as_utc(window["deadline"]):
            raise Conflict(
                "product_window_expired",
                "送达时间已超过产品有效窗，产品不得交付移植，须启动应急处置",
                {"deadline": window["deadline"], "at": iso(when)},
            )
        self._transition(case, cand, ST_DELIVERED, actor, reason="product_delivered")
        milestone = self._milestone(case, actor, "product_delivered", donor_id, when=when)
        result = {"state": cand["state"], "milestone_id": milestone["milestone_id"]}
        remember(result)
        return {**result, "deduped": False}

    def record_infusion(self, case, actor, at=None, idempotency_key=None):
        donor_id, cand = self._active(case)
        key = idempotency_key or f"infusion:{case['case_id']}"
        existing, remember = self._idem_action(case, key)
        if existing:
            return {**existing, "deduped": True}
        when = as_utc(at) if at else self.clock.now()
        self._transition(case, cand, ST_INFUSED, actor, reason="infusion_completed")
        self._milestone(case, actor, "infusion_completed", donor_id, when=when)
        result = {"state": cand["state"]}
        remember(result)
        return {**result, "deduped": False}

    def complete(self, case, actor, idempotency_key=None):
        donor_id, cand = self._active(case)
        self._transition(case, cand, ST_COMPLETED, actor, reason="case_closed")
        return {"state": cand["state"], "deduped": False}

    def _transit_milestone(self, case, actor, donor_id, target, milestone_type, at, key):
        existing, remember = self._idem_action(case, key or f"ms:{milestone_type}:{case['case_id']}")
        if existing:
            return {**existing, "deduped": True}
        cand = self._candidate(case, donor_id)
        when = as_utc(at) if at else self.clock.now()
        self._transition(case, cand, target, actor, reason=milestone_type)
        milestone = self._milestone(case, actor, milestone_type, donor_id, when=when)
        result = {"state": cand["state"], "milestone_id": milestone["milestone_id"]}
        remember(result)
        return {**result, "deduped": False}

    # -- 里程碑 --------------------------------------------------------------

    def _milestone(self, case, actor, milestone_type, donor_id, when, details=None,
                   reporter_role=None):
        when = as_utc(when)
        window = case.get("product_window")
        within_window = None
        if window and milestone_type in ("courier_pickup", "product_delivered"):
            within_window = when <= as_utc(window["deadline"])
        milestone = {
            "milestone_id": f"MS-{len(case['milestones']) + 1:04d}",
            "type": milestone_type,
            "donor_id": donor_id,
            "reported_by": actor.id,
            "reporter_role": reporter_role or sorted(actor.roles),
            "at": iso(when),
            "within_product_window": within_window,
            "details": details or {},
        }
        case["milestones"].append(milestone)
        self.audit.record(self.clock, "milestone_recorded", actor=actor.id,
                          case_id=case["case_id"], subject=milestone_type,
                          details={k: v for k, v in milestone.items() if k != "milestone_id"})
        return milestone

    def submit_milestone(self, case, actor, donor_id, milestone_type, at, tz=None,
                         details=None, idempotency_key=None):
        """医院/运输方上报通用里程碑（体检、交接等），保留时区信息。"""
        key = idempotency_key or f"ms:{milestone_type}:{donor_id}:{iso(as_utc(at))}"
        existing, remember = self._idem_action(case, key)
        if existing:
            return {**existing, "deduped": True}
        cand = self._candidate(case, donor_id)
        milestone = self._milestone(case, actor, milestone_type, donor_id, when=at,
                                    details={**(details or {}), "submitted_tz": tz})
        result = dict(milestone)
        remember(result)
        return {**result, "deduped": False}

    # -- 替代方案与替补 -------------------------------------------------------

    def open_contingency(self, case, actor, trigger, reason, findings=None, delay=None):
        """生成有优先级的替代方案列表（不自动选择）。"""
        plans = [
            {
                "plan": plan,
                "priority": idx + 1,
                "status": "proposed",
            }
            for idx, plan in enumerate(CONTINGENCY_PLANS[trigger])
        ]
        record = {
            "trigger": trigger,
            "reason": reason,
            "opened_at": iso(self.clock.now()),
            "plans": plans,
            "findings": findings or [],
            "delay": delay,
        }
        case.setdefault("contingencies", []).append(record)
        active_id = case.get("active_donor_id")
        if active_id:
            self._candidate(case, active_id)["contingencies"].append(record)
        self.audit.record(self.clock, "contingency_opened", actor=actor.id,
                          case_id=case["case_id"], subject=active_id, details=record)
        return record

    def activate_backup(self, case, actor, contingency_index=-1, plan=PLAN_SUBSTITUTE_BACKUP,
                        idempotency_key=None):
        """按排序快照激活下一顺位候选；记录替补依据，保留算法版本。"""
        key = idempotency_key or f"backup:{case['case_id']}:{len(case.get('backup_chain', []))}"
        existing, remember = self._idem_action(case, key)
        if existing:
            return {**existing, "deduped": True}
        old_active = case.get("active_donor_id")
        chain = case.get("backup_chain", [])
        available = [
            d for d in chain
            if case["candidates"][d]["state"] == ST_RANKED
        ]
        if not available:
            raise Conflict("no_backup_candidate", "排序快照中没有可激活的顺位候选，需扩大检索")
        new_active = available[0]
        if old_active:
            old_cand = self._candidate(case, old_active)
            if old_cand["state"] not in TERMINAL_STATES:
                self._transition(case, old_cand, ST_SUBSTITUTED, actor,
                                 reason=f"backup_activated:{plan}")
            self.commitments.release_commitment(old_active, case["case_id"])
        case["active_donor_id"] = new_active
        case["backup_chain"] = [d for d in chain if d != new_active]
        ranking = case.get("last_ranking") or {}
        record = {
            "from_donor": old_active,
            "to_donor": new_active,
            "to_rank_position": case["candidates"][new_active]["rank_position"],
            "grade": case["candidates"][new_active]["grade"],
            "algorithm_version": ranking.get("algorithm_version", ALGORITHM_VERSION),
            "plan": plan,
            "at": iso(self.clock.now()),
            "by": actor.id,
        }
        case.setdefault("substitutions", []).append(record)
        # 标记替代方案被采纳
        contingencies = case.get("contingencies", [])
        if contingencies:
            for p in contingencies[contingency_index]["plans"]:
                if p["plan"] == plan:
                    p["status"] = "selected"
        self.audit.record(self.clock, "backup_activated", actor=actor.id,
                          case_id=case["case_id"], subject=new_active, details=record)
        remember(record)
        return {**record, "deduped": False}

    def release_active(self, case, actor, reason, trigger=TRIGGER_DONOR_WITHDRAWAL,
                       idempotency_key=None):
        """活跃候选无法继续（非撤回，如跨病例窗口互斥）：释放承诺并开启替补方案。"""
        key = idempotency_key or f"release:{case['case_id']}:{case.get('active_donor_id')}"
        existing, remember = self._idem_action(case, key)
        if existing:
            return {**existing, "deduped": True}
        donor_id, cand = self._active(case)
        self._transition(case, cand, ST_SUBSTITUTED, actor, reason=reason)
        self.commitments.release_commitment(donor_id, case["case_id"])
        plans = self.open_contingency(case, actor, trigger, reason)
        result = {"donor_id": donor_id, "state": cand["state"], "contingency": plans}
        remember(result)
        return {**result, "deduped": False}

    def rerank(self, case, actor, ranking):
        """扩大检索后用新算法快照补充候选（原快照保留以供审计）。"""
        case.setdefault("ranking_history", []).append(case.get("last_ranking"))
        existing = set(case.get("candidates", {}))
        added = 0
        for row in ranking["candidates"]:
            if row["donor_id"] in existing:
                continue
            case["candidates"][row["donor_id"]] = {
                "donor_id": row["donor_id"],
                "state": ST_RANKED,
                "rank_position": row["rank_position"],
                "grade": row["grade"],
                "history": [],
                "contingencies": [],
            }
            case["backup_chain"].append(row["donor_id"])
            added += 1
        case["excluded_donors"].extend(ranking["excluded"])
        case["last_ranking"] = ranking
        self.audit.record(self.clock, "ranking_replaced", actor=actor.id,
                          case_id=case["case_id"], subject=None,
                          details={"algorithm_version": ranking["algorithm_version"],
                                   "added": added})
        return ranking
