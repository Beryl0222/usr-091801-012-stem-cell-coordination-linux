"""志愿者同意范围与冷静期管理。

每次联络、体检或采集前都必须以最新同意范围为准；处于冷静期的
志愿者不会被再次联络。同意记录只引用别名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .errors import ConsentError

KNOWN_ACTIONS = ("search", "recontact", "exam", "collection")


@dataclass
class ConsentRecord:
    donor_alias: str
    scope: frozenset[str]
    updated_at: datetime
    granted_at: dict[str, datetime] = field(default_factory=dict)
    cooling_until: datetime | None = None
    cooling_reason: str | None = None


class ConsentBook:
    def __init__(self, clock, audit):
        self._clock = clock
        self._audit = audit
        self._records: dict[str, ConsentRecord] = {}

    def update_scope(self, donor_alias: str, scope, *, actor: str) -> ConsentRecord:
        """以最新提交覆盖同意范围；保留仍有效项的首次授权时间。"""
        scope = frozenset(scope)
        unknown = scope - set(KNOWN_ACTIONS)
        if unknown:
            raise ConsentError(f"未知同意动作: {sorted(unknown)}")
        now = self._clock.now()
        previous = self._records.get(donor_alias)
        granted_at = dict(previous.granted_at) if previous else {}
        for action in list(granted_at):
            if action not in scope:
                del granted_at[action]
        for action in scope:
            granted_at.setdefault(action, now)
        record = ConsentRecord(
            donor_alias=donor_alias,
            scope=scope,
            updated_at=now,
            granted_at=granted_at,
            cooling_until=previous.cooling_until if previous else None,
            cooling_reason=previous.cooling_reason if previous else None,
        )
        self._records[donor_alias] = record
        self._audit.record(
            "consent.scope_updated",
            actor=actor,
            subject=donor_alias,
            basis={"scope": sorted(scope)},
        )
        return record

    def set_cooling(self, donor_alias: str, until: datetime, reason: str, *, actor: str = "system") -> None:
        """设置冷静期；已存在更晚的冷静期时不会被提前。"""
        record = self._records.get(donor_alias)
        if record is None:
            raise ConsentError("志愿者尚无同意记录")
        if record.cooling_until is None or until > record.cooling_until:
            record.cooling_until = until
            record.cooling_reason = reason
        self._audit.record(
            "consent.cooling_set",
            actor=actor,
            subject=donor_alias,
            basis={"cooling_until": record.cooling_until, "reason": reason},
        )

    def permits(self, donor_alias: str, action: str, at: datetime | None = None) -> tuple[bool, str | None]:
        """检查当前是否允许对志愿者执行动作；返回 (是否允许, 拒绝理由)。"""
        record = self._records.get(donor_alias)
        if record is None:
            return False, "consent_missing"
        if action not in record.scope:
            return False, f"scope_lacks:{action}"
        moment = at or self._clock.now()
        if record.cooling_until is not None and moment < record.cooling_until:
            return False, "cooling_off"
        return True, None

    def granted_at(self, donor_alias: str, action: str) -> datetime | None:
        record = self._records.get(donor_alias)
        if record is None:
            return None
        return record.granted_at.get(action)

    def record(self, donor_alias: str) -> ConsentRecord | None:
        return self._records.get(donor_alias)
