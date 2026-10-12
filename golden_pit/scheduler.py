"""定时扫描：美东时间工作日的固定时刻（默认盘前 08:30、收盘后 16:15）。"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _parse(slot: str) -> tuple[int, int]:
    h, m = slot.split(":")
    return int(h), int(m)


def next_run(now_et: datetime, slots: tuple[str, ...]) -> Optional[datetime]:
    """下一个工作日时刻（含今天尚未到的）。"""
    if not slots:
        return None
    for day in range(0, 8):
        d = (now_et + timedelta(days=day)).date()
        if d.weekday() >= 5:
            continue
        for slot in sorted(slots, key=_parse):
            h, m = _parse(slot)
            t = datetime(d.year, d.month, d.day, h, m, tzinfo=ET)
            if t > now_et:
                return t
    return None


class Scheduler:
    def __init__(self, slots: tuple[str, ...], trigger: Callable[[], bool]):
        self.slots, self.trigger = slots, trigger
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def info(self) -> dict:
        nxt = next_run(datetime.now(ET), self.slots)
        return {"enabled": self._thread is not None, "slots_et": list(self.slots),
                "next_run": nxt.isoformat() if nxt else None}

    def start(self) -> None:
        if self._thread or not self.slots:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        target = next_run(datetime.now(ET), self.slots)
        while not self._stop.wait(20):
            now = datetime.now(ET)
            if target and now >= target:
                # 错过超过 30 分钟（例如机器睡眠）就跳过这个时刻，不补跑过期的盘前扫描
                if now - target < timedelta(minutes=30):
                    self.trigger()
                target = next_run(now, self.slots)
