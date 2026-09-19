"""可注入时钟。

领域服务不直接读取系统时间：联调需要在不睡眠的情况下推进时间，
并模拟跨时区延误。所有时间统一为时区感知的 UTC ``datetime``；
跨时区展示/比较时以 UTC 绝对值计算冷静期与产品时效窗。
"""

from datetime import datetime, timedelta, timezone

TIME_ZERO = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Clock:
    """以固定起点开始、可显式推进的虚拟时钟。"""

    def __init__(self, start=None):
        self._now = start or TIME_ZERO

    def now(self):
        return self._now

    def advance(self, **kwargs):
        delta = timedelta(**kwargs)
        if delta <= timedelta(0):
            raise ValueError("时钟只能向前推进")
        self._now += delta
        return self._now

    def set(self, value):
        value = as_utc(value)
        if value < self._now:
            raise ValueError("时钟不能回拨")
        self._now = value
        return self._now


class SystemClock(Clock):
    """真实时钟，advance 仅用于接口一致（直接返回当前时间）。"""

    def __init__(self):
        super().__init__(start=datetime.now(timezone.utc))

    def advance(self, **kwargs):
        self._now = datetime.now(timezone.utc)
        return self._now

    def set(self, value):
        self._now = as_utc(value)
        return self._now


def as_utc(value):
    """把 ISO8601 字符串或 naive datetime 规范为 UTC 感知时间。"""
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(value):
    """统一序列化为毫秒精度的 UTC ISO 字符串。"""
    return as_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")
