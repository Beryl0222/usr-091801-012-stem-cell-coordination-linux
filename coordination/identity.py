"""身份保险库：身份密钥与业务数据分离保存。

- 业务流程只接触 HMAC 别名，别名本身不可逆推真实身份；
- 真实身份仅保存在本保险库内，与业务数据分离；
- 紧急解封需两名授权人批准（请求人不能批准自己的请求）；
- 解封授权自动到期，每次反查都写入审计。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .errors import IdentityError, UnsealDenied

DOMAIN_PREFIX = {"donor": "D", "patient": "P", "staff": "S"}


@dataclass(frozen=True)
class IdentityRecord:
    subject_id: str
    domain: str
    real_name: str
    contact: str


@dataclass
class UnsealRequest:
    request_id: str
    alias: str
    requester: str
    reason: str
    approvals: set[str] = field(default_factory=set)
    status: str = "pending"  # pending / approved


@dataclass(frozen=True)
class UnsealGrant:
    token: str
    request_id: str
    alias: str
    expires_at: datetime


class IdentityVault:
    def __init__(
        self,
        master_key: bytes,
        audit,
        clock,
        *,
        approvers: set[str],
        unseal_ttl: timedelta = timedelta(minutes=30),
        required_approvals: int = 2,
    ):
        if not master_key:
            raise IdentityError("身份保险库需要独立的主密钥")
        if required_approvals < 2:
            raise IdentityError("紧急解封至少需要双人批准")
        self._key = master_key
        self._audit = audit
        self._clock = clock
        self._approvers = set(approvers)
        self._ttl = unseal_ttl
        self._required = required_approvals
        self._records: dict[str, IdentityRecord] = {}
        self._aliases: dict[str, str] = {}
        self._requests: dict[str, UnsealRequest] = {}
        self._grants: dict[str, UnsealGrant] = {}
        self._seq = 0
        self._req_seq = 0

    # ---- 身份登记与别名 ----

    def enroll(self, domain: str, real_name: str, contact: str) -> str:
        """登记真实身份，返回内部主体编号；业务侧只应使用 alias_for 的别名。"""
        if domain not in DOMAIN_PREFIX:
            raise IdentityError(f"未知身份域: {domain}")
        self._seq += 1
        subject_id = f"{domain}-{self._seq:05d}"
        self._records[subject_id] = IdentityRecord(subject_id, domain, real_name, contact)
        self._aliases[self._derive_alias(subject_id, domain)] = subject_id
        return subject_id

    def alias_for(self, subject_id: str) -> str:
        """生成不可反查的业务别名（HMAC 单向派生）。"""
        try:
            record = self._records[subject_id]
        except KeyError:
            raise IdentityError(f"未知主体: {subject_id}") from None
        return self._derive_alias(subject_id, record.domain)

    def _derive_alias(self, subject_id: str, domain: str) -> str:
        digest = hmac.new(
            self._key, f"alias:{subject_id}".encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{DOMAIN_PREFIX[domain]}-{digest[:12]}"

    # ---- 紧急解封：双人批准 + 自动到期 ----

    def request_unseal(self, requester: str, alias: str, reason: str) -> str:
        if alias not in self._aliases:
            raise IdentityError("别名不存在")
        self._req_seq += 1
        request_id = f"UR-{self._req_seq:05d}"
        self._requests[request_id] = UnsealRequest(request_id, alias, requester, reason)
        self._audit.record(
            "identity.unseal.requested",
            actor=requester,
            subject=alias,
            basis={"request_id": request_id, "reason": reason},
        )
        return request_id

    def approve_unseal(self, request_id: str, approver: str) -> UnsealGrant | None:
        """累计授权批准；达到双人要求后签发限时令牌，否则返回 None。"""
        request = self._requests.get(request_id)
        if request is None:
            raise IdentityError(f"未知解封请求: {request_id}")
        if request.status != "pending":
            raise UnsealDenied("解封请求已结案")
        if approver not in self._approvers:
            raise UnsealDenied("批准人不在授权名单")
        if approver == request.requester:
            raise UnsealDenied("请求人不能批准自己的解封请求")
        request.approvals.add(approver)
        self._audit.record(
            "identity.unseal.approved",
            actor=approver,
            subject=request.alias,
            basis={
                "request_id": request_id,
                "approvals": len(request.approvals),
                "required": self._required,
            },
        )
        if len(request.approvals) < self._required:
            return None
        request.status = "approved"
        grant = UnsealGrant(
            token=secrets.token_hex(16),
            request_id=request_id,
            alias=request.alias,
            expires_at=self._clock.now() + self._ttl,
        )
        self._grants[grant.token] = grant
        self._audit.record(
            "identity.unseal.granted",
            actor="system",
            subject=request.alias,
            basis={
                "request_id": request_id,
                "approvers": sorted(request.approvals),
                "expires_at": grant.expires_at,
            },
        )
        return grant

    def resolve(self, token: str) -> IdentityRecord:
        """凭限时令牌按别名反查真实身份；每次反查（含失败）都留审计。"""
        grant = self._grants.get(token)
        if grant is None:
            raise UnsealDenied("解封令牌无效")
        if self._clock.now() > grant.expires_at:
            self._audit.record(
                "identity.unseal.denied",
                actor="system",
                subject=grant.alias,
                basis={"request_id": grant.request_id, "reason": "grant_expired"},
            )
            raise UnsealDenied("解封授权已自动到期")
        subject_id = self._aliases[grant.alias]
        self._audit.record(
            "identity.unseal.resolved",
            actor="system",
            subject=grant.alias,
            basis={"request_id": grant.request_id},
        )
        return self._records[subject_id]
