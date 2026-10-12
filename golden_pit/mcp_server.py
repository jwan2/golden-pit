"""MCP server：让 Claude Code / Claude Desktop 等 AI 客户端直接调用黄金坑助手。

安装：pip install "golden-pit[mcp]"
Claude Code：claude mcp add golden-pit -- golden-pit mcp

注意：stdio 模式下 stdout 是协议通道，这里不能 print。
"""
from __future__ import annotations

from typing import Optional

from . import __version__, universe
from .compact import compact
from .config import Settings
from .engine import Engine
from .i18n import render
from .scoring import TIERS

INSTRUCTIONS = """Golden Pit finds US stocks where a *quality* company is *genuinely* in a price pit.
A stock is a "golden_pit" only if BOTH the quality gate (financial 4/5, management 2/3) AND the pit
condition (>=30% below 52-week high plus a valuation anchor) pass. Missing data is "unknown" and never
counts as a pass. Results are research aids, not investment advice. Quote numbers exactly as returned;
do not invent figures. Output language follows GOLDEN_PIT_LANG (zh or en)."""


def build_tools(engine: Engine) -> dict:
    """返回 {name: function}。单独拆出来便于测试，也便于兼容不同版本的 MCP SDK。"""
    lang = engine.settings.lang

    def scan_stocks(mode: str = "curated", tickers: Optional[list[str]] = None) -> dict:
        """Run a scan and save it. mode: "curated" (82 high-moat names), "live" (Yahoo screener of
        beaten-down US stocks), "sp500" (all ~500 S&P 500 members — slow on the first run) or "custom"
        (pass tickers). Returns a ranked summary."""
        if tickers:
            mode, tickers = "custom", universe.parse_tickers(tickers)
        if mode not in universe.MODES:
            return {"error": "mode must be live, curated, sp500 or custom"}
        scan = engine.scanner.run_sync(mode, tickers or [], "mcp")
        return render({"finished_at": scan["finished_at"], "universe": scan["universe"]["note"],
                       "summary": {k: n for k, n in scan["summary"].items() if n},
                       "results": [compact(r, detail=False, lang=lang) for r in scan["results"]]}, lang)

    def analyze_stock(ticker: str) -> dict:
        """Analyze one US stock now: verdict with reasons, all 11 checks (value / rule / status),
        price-timing state, staged entry levels and data warnings. Not saved to the scan history."""
        t = universe.parse_tickers([ticker])
        if not t:
            return {"error": f"invalid ticker: {ticker}"}
        return compact(engine.analyze(t[0]), lang=lang)

    def latest_results(tier: Optional[str] = None, limit: int = 20) -> dict:
        """Read the most recent saved scan. tier filter: golden_pit, quality_wait, value_trap, fail,
        insufficient, n_a, error."""
        scan = engine.store.latest()
        if not scan:
            return {"error": "no scan yet — call scan_stocks first"}
        rows = [r for r in scan["results"] if not tier or r["verdict"]["tier"] == tier]
        return render({"finished_at": scan["finished_at"], "universe": scan["universe"]["note"],
                       "changes_vs_previous": scan.get("changes"),
                       "results": [compact(r, detail=False, lang=lang) for r in rows[: max(1, min(limit, 100))]]}, lang)

    def track_record() -> dict:
        """Forward-tracking performance of every past golden_pit call versus SPY (price return).
        Small samples are not statistically meaningful — say so when reporting."""
        rep = render(engine.track_report(), lang)
        return {"summary": rep["summary"], "caveat": rep["caveat"], "benchmark": rep["benchmark"],
                "recent": [{k: e.get(k) for k in ("ticker", "status", "flagged_at", "flag_price", "last_price",
                                                   "return_since_pct", "excess_since_pct", "days")}
                           for e in rep["entries"][:30]]}

    def methodology() -> dict:
        """Explain the rules and the current thresholds."""
        return {"tiers": render(TIERS, lang), "thresholds": engine.thresholds.to_dict(), "principles": INSTRUCTIONS}

    return {f.__name__: f for f in (scan_stocks, analyze_stock, latest_results, track_record, methodology)}


def run(settings: Optional[Settings] = None) -> None:
    engine = Engine(settings or Settings.from_env())
    try:
        from mcp.server.mcpserver import MCPServer                     # mcp >= 2
        server = MCPServer(name="golden-pit", instructions=INSTRUCTIONS, version=__version__)
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP                      # mcp 1.x
        except ImportError as e:
            raise SystemExit('需要安装 MCP 依赖：pip install "golden-pit[mcp]"') from e
        server = FastMCP("golden-pit", instructions=INSTRUCTIONS)
    for fn in build_tools(engine).values():
        server.tool()(fn)
    server.run("stdio")
