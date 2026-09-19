"""只增审计日志。

每次匹配、排除、通知、状态迁移、替补、身份解封请求/批准/到期都落一条
不可改写的审计事件。事件按序号追加并以 prev_hash 串链，导出时可校验
完整性。审计视图里对普通协调员仍不出现真实身份（见 identity 模块的
脱敏规则），解封事件只记录依据与批准人。
"""

import copy
import hashlib
import json as _json

from .clock import as_utc, iso


def _digest(previous, payload):
    linked = (previous or "") + _json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(linked.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self):
        self._events = []

    def record(self, clock, action, actor=None, case_id=None, subject=None, details=None, at=None):
        """追加一条审计事件。subject 为被操作对象（如志愿者病例别名）。

        at 用于系统事件（如许可自动到期）在非交互时刻落痕。
        """
        previous = self._events[-1]["hash"] if self._events else None
        seq = len(self._events) + 1
        # 深拷贝：调用方在记录后继续修改入参对象（如延误事件补 breach）不得影响审计
        safe_details = copy.deepcopy(details or {})
        payload = {
            "seq": seq,
            "at": iso(clock.now()) if at is None and clock is not None else iso(as_utc(at)),
            "action": action,
            "actor": actor,
            "case_id": case_id,
            "subject": subject,
            "details": safe_details,
        }
        event = dict(payload)
        event["prev_hash"] = previous
        event["hash"] = _digest(previous, payload)
        self._events.append(event)
        return event

    def export(self, case_id=None, action=None):
        """按病例/动作过滤导出，返回深拷贝避免调用方篡改。"""
        result = self._events
        if case_id is not None:
            result = [e for e in result if e["case_id"] == case_id]
        if action is not None:
            if isinstance(action, str):
                action = [action]
            result = [e for e in result if e["action"] in action]
        return [dict(e) for e in result]

    def verify_chain(self):
        """重算整条哈希链，供审计导出后核对完整性。"""
        previous = None
        for event in self._events:
            payload = {k: event[k] for k in ("seq", "at", "action", "actor", "case_id", "subject", "details")}
            expected = _digest(previous, payload)
            if event["hash"] != expected or event["prev_hash"] != previous:
                return False
            previous = event["hash"]
        return True

    def __len__(self):
        return len(self._events)
