"""时钟抽象：线上使用真实时钟，测试与演示使用可推进的 FakeClock。"""
from datetime import datetime, timedelta

FMT = "%Y-%m-%dT%H:%M:%S"


class Clock:
    def now(self) -> datetime:
        return datetime.now().replace(microsecond=0)


class FakeClock:
    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 10, 1, 9, 50, 0)

    def now(self) -> datetime:
        return self.t

    def advance(self, **kwargs) -> datetime:
        self.t += timedelta(**kwargs)
        return self.t

    def set(self, t: datetime) -> None:
        self.t = t


def ts(dt: datetime) -> str:
    return dt.strftime(FMT)


def parse(s: str) -> datetime:
    return datetime.strptime(s, FMT)


def dur_str(seconds: float) -> str:
    """封控计时展示：HH:MM:SS / MM:SS。"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
