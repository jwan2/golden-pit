"""配置：阈值（单一事实来源）与运行时环境设置。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from .i18n import env_lang

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Thresholds:
    # —— 财务抗压（5 项）——
    interest_coverage_min: float = 2.0       # EBIT / 利息支出
    current_ratio_min: float = 1.0
    net_debt_to_ebitda_max: float = 5.0
    altman_z_min: float = 1.8
    # FCF > 0 为固定规则，不可配置

    # —— 管理层资本配置（3 项）——
    shares_reduction_min_pct: float = 1.0    # 窗口内总股本净减少 ≥ 1%
    buyback_yield_min_pct: float = 1.0       # 净回购 / 市值 ≥ 1%
    capex_to_revenue_max_pct: float = 15.0

    # —— 坑位（估值）——
    drop_from_high_min_pct: float = 30.0     # 距 52 周高点回撤 ≥ 30%（必需）
    pb_max: float = 2.0                      # P/B ∈ (0, 2]
    forward_pe_max: float = 25.0             # Forward P/E ∈ (0, 25]

    # —— 判定规则 ——
    financial_min_pass: int = 4              # 财务 5 项至少通过几项
    management_min_pass: int = 2             # 管理层 3 项至少通过几项
    pit_min_anchors: int = 1                 # P/B、Forward P/E 至少几项合格
    max_unknown_checks: int = 2              # 财务+管理层 8 项里未知超过几项 → 数据不足

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ScreenerConfig:
    """Yahoo 实时筛选器的"初筛"条件。真正的判定在 scoring 里，这里只是缩小候选池。"""
    min_market_cap_billions: float = 1.0
    max_52w_change_pct: float = -15.0        # 52 周涨跌幅 ≤ -15%
    max_pb: float = 4.0
    require_positive_fcf: bool = True
    size: int = 60                          # 候选数量上限（Yahoo 单次查询最多 250）

    def to_dict(self) -> dict:
        return asdict(self)


def _coerce(cls, base, raw: dict):
    """只接受已知字段、且类型能转换的覆盖值；其余忽略。"""
    updates = {}
    for f in fields(cls):
        if f.name not in raw:
            continue
        current = getattr(base, f.name)
        try:
            if isinstance(current, bool):
                updates[f.name] = bool(raw[f.name])
            elif isinstance(current, int):
                updates[f.name] = int(raw[f.name])
            else:
                updates[f.name] = float(raw[f.name])
        except (TypeError, ValueError):
            continue
    return replace(base, **updates)


def load_overrides(data_dir: Path) -> tuple[Thresholds, ScreenerConfig]:
    """data/settings.json 可以覆盖默认阈值，例如 {"thresholds": {"pb_max": 3}}。"""
    th, sc = Thresholds(), ScreenerConfig()
    path = data_dir / "settings.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            th = _coerce(Thresholds, th, raw.get("thresholds", {}))
            sc = _coerce(ScreenerConfig, sc, raw.get("screener", {}))
        except (OSError, ValueError):
            pass
    return th, sc


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    admin_token: str
    host: str
    port: int
    scheduler_enabled: bool
    workers: int
    slots: tuple[str, ...]          # 美东时间 HH:MM
    quote_ttl: float = 3600         # 行情缓存秒数
    lang: str = "en"                # 默认输出语言（网页按浏览器语言自动选择）
    fundamentals_ttl: float = 7 * 86400

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        slots = tuple(s.strip() for s in env("GOLDEN_PIT_SLOTS", "08:30,16:15").split(",") if s.strip())
        return cls(
            data_dir=Path(env("GOLDEN_PIT_DATA", str(Path.home() / ".golden-pit"))).expanduser(),
            admin_token=env("GOLDEN_PIT_ADMIN_TOKEN", ""),
            host=env("GOLDEN_PIT_HOST", "127.0.0.1"),
            port=int(env("GOLDEN_PIT_PORT", "5000")),
            scheduler_enabled=env("GOLDEN_PIT_SCHEDULER", "0") == "1",
            workers=max(1, min(8, int(env("GOLDEN_PIT_WORKERS", "4")))),
            slots=slots,
            quote_ttl=float(env("GOLDEN_PIT_QUOTE_TTL", "3600")),
            fundamentals_ttl=float(env("GOLDEN_PIT_FUNDAMENTALS_TTL", str(7 * 86400))),
            lang=env_lang(),
        )
