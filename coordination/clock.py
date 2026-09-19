"""可注入的时钟，便于在测试中联调跨时区与重试场景。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def require_aware(moment: datetime, name: str) -> datetime:
    """业务时间一律要求带时区，避免跨时区接力时产生歧义。"""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} 必须携带时区信息")
    return moment


def to_utc(moment: datetime) -> datetime:
    """把任意时区的时刻归一到 UTC。"""
    return require_aware(moment, "moment").astimezone(timezone.utc)


class Clock:
    """时钟接口：领域服务只依赖该接口获取当前时间。"""

    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock(Clock):
    """测试时钟：可手动推进，用于冷静期、自动到期与跨时区重放。"""

    def __init__(self, start: datetime):
        self._now = require_aware(start, "start")

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = require_aware(moment, "moment")

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now
