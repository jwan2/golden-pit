"""把存储、缓存、扫描、追踪、AI 组装在一起。网页、命令行、MCP 三个入口共用同一个 Engine。"""
from __future__ import annotations

from typing import Callable, Optional

from .ai import AIConfig, AIError, AIService
from .analyze import analyze_ticker
from .config import Settings, load_overrides
from .models import RawStock
from .providers.cache import CachedProvider
from .scanner import Scanner
from .store import Store
from .track import Tracker


class Engine:
    def __init__(self, settings: Settings, *, ai: Optional[AIService] = None,
                 fetch: Optional[Callable[[str], RawStock]] = None,
                 price: Optional[Callable[[str], Optional[float]]] = None):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(settings.data_dir)
        self.thresholds, self.screener = load_overrides(settings.data_dir)
        self.provider = CachedProvider(settings.data_dir / "cache", settings.quote_ttl, settings.fundamentals_ttl)
        self.tracker = Tracker(settings.data_dir)
        self.ai = ai or AIService(AIConfig.from_env(), settings.data_dir)
        self.scanner = Scanner(
            self.store, self.thresholds, self.screener, workers=settings.workers,
            fetch=fetch or self.provider.raw, price=price or self.provider.price,
            tracker=self.tracker, on_complete=self._auto_ai)

    def analyze(self, ticker: str) -> dict:
        return analyze_ticker(ticker, self.thresholds, self.scanner.fetch)

    def track_report(self) -> dict:
        return Tracker.report(self.tracker.load())

    def _auto_ai(self, scan: dict) -> None:
        """扫描结束后，对最有看点的前 N 只自动生成 AI 研判（GOLDEN_PIT_AI_AUTO_TOP>0 时）。"""
        if not (self.ai.cfg.enabled and self.ai.cfg.auto_top):
            return
        picks = [r for r in scan["results"] if r["verdict"]["tier"] in ("golden_pit", "quality_wait")]
        for r in picks[: self.ai.cfg.auto_top]:
            try:
                self.ai.generate(r, self.settings.lang)
            except AIError:
                continue
