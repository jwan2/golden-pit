"""访问统计：只数首页打开次数（浏览）和当天去重的人数（访客）。

隐私：不存 IP。访客 = sha256(当天随机盐 + IP + UA) 的前 16 位，只在当天用来去重；
第二天换新盐、清掉前一天的指纹，之后只留每天的两个数字。爬虫和命令行工具不计。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

TZ = timezone(timedelta(hours=8))           # 按北京时间算"今天"（访客大多在国内，且没有夏令时）
TZ_LABEL = "UTC+8"
BOT = re.compile(r"bot|spider|crawl|slurp|curl|wget|python|httpx|okhttp|go-http|java/|libwww|headless|"
                 r"facebookexternalhit|preview|monitor|uptime|scan", re.I)


class Visits:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time, keep_days: int = 400):
        self.path, self.clock, self.keep_days = Path(path), clock, keep_days
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d.get("days"), dict):
                d.setdefault("today", {})
                return d
        except (OSError, ValueError):
            pass
        return {"days": {}, "today": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)

    def _date(self, offset: int = 0) -> str:
        return (datetime.fromtimestamp(self.clock(), TZ) + timedelta(days=offset)).date().isoformat()

    def _roll(self) -> dict:
        """换天：新盐、清空昨天的指纹，旧的日数据只保留 keep_days 天。"""
        today = self._date()
        cur = self.data["today"]
        if cur.get("date") != today:
            cur = self.data["today"] = {"date": today, "salt": secrets.token_hex(16), "seen": []}
            cutoff = self._date(-self.keep_days)
            self.data["days"] = {k: v for k, v in self.data["days"].items() if k >= cutoff}
        return cur

    def hit(self, ip: str, ua: str) -> bool:
        """记一次首页打开。返回 True 表示计入（爬虫等返回 False）。"""
        if not ua or BOT.search(ua):
            return False
        with self.lock:
            cur = self._roll()
            day = self.data["days"].setdefault(cur["date"], {"pv": 0, "uv": 0})
            day["pv"] += 1
            fp = hashlib.sha256(f"{cur['salt']}|{ip}|{ua}".encode()).hexdigest()[:16]
            if fp not in cur["seen"]:
                cur["seen"].append(fp)
                day["uv"] += 1
            try:
                self._save()
            except OSError:
                pass                    # 统计写不进去不能影响首页
        return True

    def stats(self) -> dict:
        with self.lock:
            days = dict(self.data["days"])
        today, yday = self._date(), self._date(-1)
        week = [self._date(-i) for i in range(7)]
        zero = {"pv": 0, "uv": 0}
        return {
            "tz": TZ_LABEL,
            "today": {"date": today, **days.get(today, zero)},
            "yesterday": {"date": yday, **days.get(yday, zero)},
            "d7": {"pv": sum(days.get(d, zero)["pv"] for d in week), "uv": sum(days.get(d, zero)["uv"] for d in week)},
            "total": {"pv": sum(v["pv"] for v in days.values()), "uv": sum(v["uv"] for v in days.values()),
                      "since": min(days) if days else None},
            "series": [{"date": d, **days.get(d, zero)} for d in reversed([self._date(-i) for i in range(30)])],
        }
