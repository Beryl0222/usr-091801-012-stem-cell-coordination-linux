"""志愿者联络：同意范围、冷静期与通知幂等去重。

规则：
- 每次联络必须落在志愿者最新同意范围内；撤回同意立即对未来联络生效。
- 冷静期：同类联络有最小间隔；初次告知到请求确认之间有强制考虑期。
- 通知按 (病例, 志愿者, 模板, 业务事件) 幂等：协调员/系统重试同一动作
  不会发出第二次通知，重复尝试写入审计可查。
- 志愿者同时在多个病例流程中时：全局联络节流与"待决定"互斥，
  返回的拒绝理由不包含其他病例的任何信息（防相互泄露）。
"""

from datetime import timedelta

from .clock import iso
from .errors import Conflict, DomainError, NotFound

# 同意范围
SCOPE_INITIAL_CONTACT = "initial_contact"      # 初次检索告知
SCOPE_CONFIRM_REQUEST = "confirm_request"      # 请求确认捐献
SCOPE_MEDICAL = "medical"                      # 体检安排相关
SCOPE_COLLECTION = "collection"                # 采集安排相关
SCOPE_FOLLOWUP = "followup"                    # 捐献后随访

CONTACT_SCOPES = frozenset(
    {SCOPE_INITIAL_CONTACT, SCOPE_CONFIRM_REQUEST, SCOPE_MEDICAL, SCOPE_COLLECTION, SCOPE_FOLLOWUP}
)

# 模板（通道）需要的同意范围
TEMPLATE_SCOPE = {
    "initial_match_notice": SCOPE_INITIAL_CONTACT,
    "confirm_request": SCOPE_CONFIRM_REQUEST,
    "exam_schedule": SCOPE_MEDICAL,
    "collection_schedule": SCOPE_COLLECTION,
    "followup_checkin": SCOPE_FOLLOWUP,
}

# 同病例同类联络的最小冷静间隔（小时）
COOLING_HOURS_BY_SCOPE = {
    SCOPE_INITIAL_CONTACT: 24,
    SCOPE_CONFIRM_REQUEST: 48,
    SCOPE_MEDICAL: 12,
    SCOPE_COLLECTION: 12,
    SCOPE_FOLLOWUP: 72,
}

# 初次告知之后，请求确认前的强制考虑期（小时）
REFLECTION_HOURS_BEFORE_CONFIRM = 48

# 跨病例全局节流：不论病例，同一志愿者两次联络的最小间隔（小时）
GLOBAL_CONTACT_INTERVAL_HOURS = 6


class ConsentRegistry:
    """志愿者同意范围与志愿者自设的勿扰时间。"""

    def __init__(self):
        self._donors = {}

    def register(self, donor_id, scopes, granted_at=None, do_not_contact_until=None):
        self._donors[donor_id] = {
            "scopes": set(scopes),
            "history": [
                {
                    "scopes": sorted(scopes),
                    "at": granted_at,
                    "change": "grant",
                }
            ],
            "do_not_contact_until": do_not_contact_until,
        }

    def grant(self, clock, donor_id, scopes):
        record = self._require(donor_id)
        added = sorted(set(scopes) - record["scopes"])
        record["scopes"].update(scopes)
        if added:
            record["history"].append({"scopes": added, "at": iso(clock.now()), "change": "grant"})
        return added

    def withdraw(self, clock, donor_id, scopes):
        record = self._require(donor_id)
        removed = sorted(set(scopes) & record["scopes"])
        record["scopes"].difference_update(scopes)
        if removed:
            record["history"].append({"scopes": removed, "at": iso(clock.now()), "change": "withdraw"})
        return removed

    def set_do_not_contact_until(self, clock, donor_id, until):
        record = self._require(donor_id)
        record["do_not_contact_until"] = until
        record["history"].append(
            {"scopes": [], "at": iso(clock.now()), "change": "do_not_contact", "until": until}
        )

    def scopes_of(self, donor_id):
        return set(self._require(donor_id)["scopes"])

    def is_within(self, donor_id, scope):
        return scope in self._require(donor_id)["scopes"]

    def do_not_contact_until(self, donor_id):
        return self._require(donor_id).get("do_not_contact_until")

    def _require(self, donor_id):
        record = self._donors.get(donor_id)
        if record is None:
            raise NotFound("志愿者同意记录", donor_id)
        return record

    def snapshot(self):
        return {
            "donors": {
                k: {"scopes": sorted(v["scopes"]), "history": list(v["history"]),
                    "do_not_contact_until": v.get("do_not_contact_until")}
                for k, v in self._donors.items()
            }
        }

    def restore(self, data):
        for k, v in (data.get("donors") or {}).items():
            self._donors[k] = {
                "scopes": set(v.get("scopes", [])),
                "history": list(v.get("history", [])),
                "do_not_contact_until": v.get("do_not_contact_until"),
            }


class ContactLedger:
    """联络事实账本：病例内冷静期、跨病例节流、待决定互斥、幂等去重。"""

    def __init__(self):
        # donor_id -> [contact event...]（跨病例共享，事件只存最小必要字段）
        self._by_donor = {}
        # idempotency_key -> dispatch
        self._dispatches = {}
        # donor_id -> case_id：该志愿者在某病例存在待答复的确认请求
        self._pending_decisions = {}

    # -- 查询 ----------------------------------------------------------------

    def last_contact(self, donor_id, scope=None):
        events = self._by_donor.get(donor_id, [])
        if scope is not None:
            events = [e for e in events if e["scope"] == scope]
        return events[-1] if events else None

    def pending_decision_case(self, donor_id):
        """返回志愿者当前存在待决定的病例；调用方不得把它泄露给其他病例。"""
        case_id = self._pending_decisions.get(donor_id)
        return case_id

    def get_dispatch(self, idempotency_key):
        return self._dispatches.get(idempotency_key)

    # -- 写入（由 ContactPolicy 调用） ---------------------------------------

    def _append(self, clock, donor_id, case_id, scope, template, idempotency_key, channel="sms"):
        event = {
            "donor_id": donor_id,
            "case_id": case_id,
            "scope": scope,
            "template": template,
            "channel": channel,
            "idempotency_key": idempotency_key,
            "at": iso(clock.now()),
        }
        self._by_donor.setdefault(donor_id, []).append(event)
        return event

    def mark_decision_pending(self, donor_id, case_id):
        self._pending_decisions[donor_id] = case_id

    def clear_decision_pending(self, donor_id, case_id):
        if self._pending_decisions.get(donor_id) == case_id:
            self._pending_decisions.pop(donor_id, None)

    def snapshot(self):
        return {
            "by_donor": {k: [dict(e) for e in v] for k, v in self._by_donor.items()},
            "dispatches": {k: dict(v) for k, v in self._dispatches.items()},
            "pending_decisions": dict(self._pending_decisions),
        }

    def restore(self, data):
        self._by_donor = {k: [dict(e) for e in v] for k, v in (data.get("by_donor") or {}).items()}
        self._dispatches = {k: dict(v) for k, v in (data.get("dispatches") or {}).items()}
        self._pending_decisions = dict(data.get("pending_decisions") or {})


class ContactPolicy:
    """把同意/冷静期规则与账本绑定，产出可发送或拒绝结论。"""

    def __init__(self, clock, consents, ledger, audit, renderer):
        self.clock = clock
        self.consents = consents
        self.ledger = ledger
        self.audit = audit
        self.renderer = renderer

    def evaluate(self, donor_id, case_id, template):
        """纯校验，返回 scope；不满足时抛 DomainError（错误不含其他病例信息）。"""
        scope = TEMPLATE_SCOPE.get(template)
        if scope is None:
            raise DomainError("unknown_template", f"未知通知模板: {template}")
        if not self.consents.is_within(donor_id, scope):
            raise DomainError("consent_out_of_scope", "该联络不在志愿者最新同意范围内")
        until = self.consents.do_not_contact_until(donor_id)
        if until and self.clock.now() < _as_dt(until):
            raise DomainError("do_not_contact_window", "志愿者处于勿扰期")

        last_same_scope = self.ledger.last_contact(donor_id, scope)
        if last_same_scope:
            hours = COOLING_HOURS_BY_SCOPE[scope]
            if self.clock.now() < _as_dt(last_same_scope["at"]) + timedelta(hours=hours):
                raise DomainError("cooling_period", f"该联络处于 {hours} 小时冷静期内", {"cooling_hours": hours})

        last_any = self.ledger.last_contact(donor_id)
        if last_any and self.clock.now() < _as_dt(last_any["at"]) + timedelta(
            hours=GLOBAL_CONTACT_INTERVAL_HOURS
        ):
            raise DomainError("global_contact_interval", "联络过于频繁，请稍后再试")

        if template == "confirm_request":
            initial = self.ledger.last_contact(donor_id, SCOPE_INITIAL_CONTACT)
            if initial is None or self.clock.now() < _as_dt(initial["at"]) + timedelta(
                hours=REFLECTION_HOURS_BEFORE_CONFIRM
            ):
                raise DomainError(
                    "reflection_period",
                    f"初次告知后须满 {REFLECTION_HOURS_BEFORE_CONFIRM} 小时考虑期才能请求确认",
                )
            other_case = self.ledger.pending_decision_case(donor_id)
            if other_case and other_case != case_id:
                # 不返回 other_case，避免向本病例泄露另一患者流程
                raise Conflict("donor_decision_pending", "志愿者有待答复的决定，暂不能发起新的确认请求")
        return scope

    def send(self, actor, donor_id, case_id, template, idempotency_key, context=None):
        """发送通知；同一 idempotency_key 的重试只返回原结果。"""
        existing = self.ledger.get_dispatch(idempotency_key)
        if existing is not None:
            self.audit.record(
                self.clock,
                "notification_dedup",
                actor=actor.id,
                case_id=case_id,
                subject=donor_id,
                details={"template": template, "idempotency_key": idempotency_key,
                         "original_at": existing["at"]},
            )
            return {**dict(existing), "deduped": True}

        scope = self.evaluate(donor_id, case_id, template)
        content = self.renderer(template, context or {})
        dispatch = self.ledger._append(
            self.clock, donor_id, case_id, scope, template, idempotency_key
        )
        dispatch["content_preview"] = content
        self.ledger._dispatches[idempotency_key] = dict(dispatch)
        if template == "confirm_request":
            self.ledger.mark_decision_pending(donor_id, case_id)
        self.audit.record(
            self.clock,
            "notification_sent",
            actor=actor.id,
            case_id=case_id,
            subject=donor_id,
            details={"template": template, "scope": scope, "idempotency_key": idempotency_key},
        )
        return {**dict(dispatch), "deduped": False}


def _as_dt(value):
    from .clock import as_utc

    return as_utc(value)


def default_renderer(template, context):
    """渲染脱敏内容：志愿者侧通知绝不出现任何患者标识（含别名）。"""
    alias = context.get("donor_alias", "志愿者")
    texts = {
        "initial_match_notice": f"【骨髓库】{alias}您好，您与一位等待移植的患者初分辨相合，欢迎了解捐献流程。",
        "confirm_request": "【骨髓库】您好，充分知情并经过考虑期后，请回复是否确认捐献。",
        "exam_schedule": f"【骨髓库】{alias}您好，您的高分辨确认/体检已安排，请按约定时间到达。",
        "collection_schedule": f"【骨髓库】{alias}您好，采集计划已确定，请按约定入院。",
        "followup_checkin": f"【骨髓库】{alias}您好，捐献后随访关怀，请方便时回复。",
    }
    return texts[template]
