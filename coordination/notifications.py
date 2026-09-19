"""通知发件箱：以幂等键去重，重试不会产生重复通知。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Notification:
    key: str
    case_id: str
    donor_alias: str
    kind: str
    detail: str
    sent_at: datetime


class Notifier:
    def __init__(self, clock, audit):
        self._clock = clock
        self._audit = audit
        self._outbox: dict[str, Notification] = {}

    def has(self, key: str) -> bool:
        return key in self._outbox

    def notify(
        self,
        key: str,
        *,
        case_id: str,
        donor_alias: str,
        kind: str,
        detail: str = "",
    ) -> tuple[Notification, bool]:
        """返回 (通知, 本次是否真正发出)；幂等键命中时不重复发送。"""
        existing = self._outbox.get(key)
        if existing is not None:
            self._audit.record(
                "notification.deduplicated",
                actor="notifier",
                case_id=case_id,
                subject=donor_alias,
                basis={"key": key, "kind": kind, "first_sent_at": existing.sent_at},
            )
            return existing, False
        notification = Notification(key, case_id, donor_alias, kind, detail, self._clock.now())
        self._outbox[key] = notification
        self._audit.record(
            "notification.sent",
            actor="notifier",
            case_id=case_id,
            subject=donor_alias,
            basis={"key": key, "kind": kind, "detail": detail},
        )
        return notification, True

    @property
    def sent_count(self) -> int:
        return len(self._outbox)

    def sent(self, kind: str | None = None) -> list[Notification]:
        return [
            note for note in self._outbox.values() if kind is None or note.kind == kind
        ]
