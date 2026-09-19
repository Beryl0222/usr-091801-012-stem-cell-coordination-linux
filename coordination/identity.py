"""身份隔离与紧急解封。

两条硬规则：

1. 身份密钥与业务数据分离。志愿者/患者的真实身份（姓名、证件、联系方式）
   只存在于 IdentityVault；业务流程（匹配、联络、里程碑）里出现的全部是
   按病例派生的别名。同一志愿者进入两个患者流程时得到不同别名，
   别名由 HMAC(部署密钥, 病例ID + 内部ID) 生成，不可反查、不可跨病例关联。
2. 普通协调员永远拿不到真实身份。紧急解封（break-glass）必须由两名
   身份官各自批准（请求人不能批准自己的请求），批准后授予带作用域和
   TTL 的许可，到期自动失效，每次取用真实身份都写审计。
"""

import hashlib
import hmac
import re

from .clock import iso
from .errors import Conflict, DomainError, PermissionDenied

ROLE_COORDINATOR = "coordinator"
ROLE_IDENTITY_OFFICER = "identity_officer"
ROLE_AUDITOR = "auditor"
ROLE_COLLECTION_HOSPITAL = "collection_hospital"
ROLE_TRANSPLANT_HOSPITAL = "transplant_hospital"
ROLE_COURIER = "courier"

ALL_ROLES = frozenset(
    {
        ROLE_COORDINATOR,
        ROLE_IDENTITY_OFFICER,
        ROLE_AUDITOR,
        ROLE_COLLECTION_HOSPITAL,
        ROLE_TRANSPLANT_HOSPITAL,
        ROLE_COURIER,
    }
)

DEFAULT_GRANT_TTL_MINUTES = 60
MAX_GRANT_TTL_MINUTES = 240


class Actor:
    """调用方身份。roles 决定可见视图与可执行操作。"""

    def __init__(self, actor_id, name, roles):
        roles = frozenset(roles)
        unknown = roles - ALL_ROLES
        if unknown:
            raise ValueError(f"未知角色: {sorted(unknown)}")
        self.id = actor_id
        self.name = name
        self.roles = roles

    def has(self, role):
        return role in self.roles

    def require(self, role):
        if role not in self.roles:
            raise PermissionDenied(f"需要角色 {role}")

    def to_dict(self):
        return {"id": self.id, "name": self.name, "roles": sorted(self.roles)}


def _hmac_alias(secret, namespace, internal_id):
    digest = hmac.new(secret.encode("utf-8"), f"{namespace}|{internal_id}".encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


def donor_alias(secret, case_id, donor_id):
    """志愿者在某一患者病例内的别名：同志愿者跨病例别名不同。"""
    return "D-" + _hmac_alias(secret, f"case:{case_id}:donor", donor_id)[:12].upper()


def patient_alias(secret, patient_id):
    """患者对外（医院视角之外）的别名。"""
    return "P-" + _hmac_alias(secret, "patient", patient_id)[:12].upper()


def mask_name(name):
    if not name:
        return ""
    if len(name) == 1:
        return name + "**"
    return name[0] + "*" * (len(name) - 1)


def mask_phone(phone):
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) >= 7:
        return digits[:3] + "****" + digits[-4:]
    return "****"


class IdentityVault:
    """真实身份保险库：与业务库分开快照，访问必须经过解封许可。"""

    def __init__(self):
        # internal_id -> {kind, real_name, id_number, phone, ...}
        self._records = {}

    def put(self, internal_id, kind, real_name, id_number, phone, **extra):
        if internal_id in self._records:
            raise Conflict("identity_exists", f"身份记录已存在: {internal_id}")
        record = {
            "internal_id": internal_id,
            "kind": kind,
            "real_name": real_name,
            "id_number": id_number,
            "phone": phone,
        }
        record.update(extra)
        self._records[internal_id] = record
        return internal_id

    def resolve(self, internal_id):
        """仅供已授权的内部调用路径使用（调用方负责先校验解封许可）。"""
        record = self._records.get(internal_id)
        if record is None:
            raise DomainError("identity_missing", f"身份记录不存在: {internal_id}", status=404)
        return dict(record)

    def masked_view(self, internal_id):
        record = self.resolve(internal_id)
        return {
            "internal_id": internal_id,
            "kind": record["kind"],
            "real_name": mask_name(record["real_name"]),
            "phone": mask_phone(record["phone"]),
        }

    def snapshot(self):
        return {"records": {k: dict(v) for k, v in self._records.items()}}

    def restore(self, data):
        self._records = {k: dict(v) for k, v in (data.get("records") or {}).items()}


class Grant:
    """一次生效中的紧急解封许可。"""

    def __init__(self, request):
        self.request = request

    def is_active(self, clock):
        req = self.request
        if req["status"] != "active":
            return False
        from .clock import as_utc

        return clock.now() < as_utc(req["expires_at"])

    def covers(self, case_id, subject):
        if req_case := self.request["case_id"]:
            if req_case != case_id:
                return False
        return subject in self.request["subjects"]

    def to_dict(self):
        return dict(self.request)


class BreakGlassManager:
    """紧急解封请求的双人批准、作用域限制与自动到期。"""

    def __init__(self, on_expire=None):
        self._requests = {}
        self._seq = 0
        self.on_expire = on_expire

    def request(self, clock, actor, case_id, subjects, reason, ttl_minutes=DEFAULT_GRANT_TTL_MINUTES):
        actor.require(ROLE_COORDINATOR)
        if not reason or not reason.strip():
            raise DomainError("reason_required", "紧急解封必须填写依据")
        if not subjects:
            raise DomainError("scope_required", "解封必须限定具体对象")
        ttl_minutes = int(ttl_minutes)
        if ttl_minutes <= 0 or ttl_minutes > MAX_GRANT_TTL_MINUTES:
            raise DomainError("bad_ttl", f"有效期必须在 1-{MAX_GRANT_TTL_MINUTES} 分钟之间")
        self._seq += 1
        grant_id = f"BG-{self._seq:04d}"
        req = {
            "grant_id": grant_id,
            "case_id": case_id,
            "subjects": sorted(set(subjects)),
            "reason": reason.strip(),
            "requested_by": actor.id,
            "requested_at": iso(clock.now()),
            "approvals": [],
            "status": "pending",
            "expires_at": None,
            "activated_at": None,
            "closed_at": None,
            "ttl_minutes": ttl_minutes,
            "uses": [],
        }
        self._requests[grant_id] = req
        return grant_id, dict(req)

    def approve(self, clock, actor, grant_id):
        actor.require(ROLE_IDENTITY_OFFICER)
        req = self._get(grant_id)
        if req["status"] != "pending":
            raise Conflict("grant_not_pending", f"解封请求 {grant_id} 状态为 {req['status']}，不可再批准")
        if actor.id == req["requested_by"]:
            raise PermissionDenied("请求人不能批准自己发起的解封请求")
        if any(a["approver"] == actor.id for a in req["approvals"]):
            raise Conflict("already_approved", f"{actor.name} 已批准过该请求")
        req["approvals"].append({"approver": actor.id, "approver_name": actor.name, "at": iso(clock.now())})
        if len(req["approvals"]) >= 2:
            req["status"] = "active"
            req["activated_at"] = iso(clock.now())
            from datetime import timedelta

            ttl = self._ttl_of(req)
            req["expires_at"] = iso(clock.now() + timedelta(minutes=ttl))
        return dict(req)

    @staticmethod
    def _ttl_of(req):
        # TTL 在请求时固定，保存在 reason 之外的字段里
        return req.get("ttl_minutes", DEFAULT_GRANT_TTL_MINUTES)

    def revoke(self, clock, actor, grant_id):
        actor.require(ROLE_IDENTITY_OFFICER)
        req = self._get(grant_id)
        if req["status"] not in ("pending", "active"):
            raise Conflict("grant_closed", f"解封请求 {grant_id} 已结束")
        req["status"] = "revoked"
        req["closed_at"] = iso(clock.now())
        return dict(req)

    def status_of(self, clock, grant_id):
        req = self._get(grant_id)
        self._refresh(clock, req)
        return dict(req)

    def _refresh(self, clock, req):
        """惰性到期：到达 expires_at 后状态自动转为 expired。"""
        if req["status"] == "active" and req["expires_at"] is not None:
            from .clock import as_utc

            if clock.now() >= as_utc(req["expires_at"]):
                req["status"] = "expired"
                req["closed_at"] = req["expires_at"]
                if self.on_expire:
                    self.on_expire(req)

    def authorize(self, clock, actor, case_id, subject):
        """返回覆盖该病例+对象的有效许可，否则拒绝。每次授权记录使用。"""
        actor.require(ROLE_COORDINATOR)
        for req in self._requests.values():
            self._refresh(clock, req)
            grant = Grant(req)
            if grant.is_active(clock) and grant.covers(case_id, subject):
                req["uses"].append(
                    {
                        "by": actor.id,
                        "case_id": case_id,
                        "subject": subject,
                        "at": iso(clock.now()),
                    }
                )
                return grant
        raise PermissionDenied(
            "没有覆盖该对象的有效紧急解封许可",
            {"case_id": case_id, "subject": subject},
        )

    def list(self, clock):
        for req in self._requests.values():
            self._refresh(clock, req)
        return [dict(r) for r in self._requests.values()]

    def _get(self, grant_id):
        req = self._requests.get(grant_id)
        if req is None:
            raise DomainError("grant_not_found", f"解封请求不存在: {grant_id}", status=404)
        return req

    def snapshot(self):
        return {"seq": self._seq, "requests": {k: dict(v) for k, v in self._requests.items()}}

    def restore(self, data):
        self._seq = data.get("seq", 0)
        self._requests = {k: dict(v) for k, v in (data.get("requests") or {}).items()}
