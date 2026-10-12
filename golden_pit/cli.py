"""命令行入口：golden-pit serve | scan | analyze | track | mcp"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata

from . import __version__, universe
from .compact import compact
from .config import Settings

STATUS_ICON = {"pass": "✓", "fail": "✗", "unknown": "?", "na": "–"}


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _w(s: str) -> int:
    """终端显示宽度（中文占 2 格）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(s))


def _pad(s, width: int, right: bool = False) -> str:
    s = str(s)
    gap = max(0, width - _w(s))
    return " " * gap + s if right else s + " " * gap


def print_table(results: list[dict]) -> None:
    cols = [("代码", 6, False), ("结论", 14, False), ("现价", 9, True), ("回撤", 7, True),
            ("财务", 5, True), ("管理层", 6, True), ("未知", 4, True), ("走势", 8, False), ("公司", 0, False)]
    print("  ".join(_pad(n, w, r) for n, w, r in cols).rstrip())
    for r in results:
        c = compact(r, detail=False)
        drop = "—" if c["drop_from_52w_high_pct"] is None else f"{c['drop_from_52w_high_pct']:.1f}%"
        price = "—" if c["price"] is None else f"{c['price']:.2f}"
        row = [c["ticker"], c["verdict"], price, drop, c["financial_pass"], c["management_pass"],
               c["unknown_checks"], c["timing"] or "—", (c["name"] or "")[:30]]
        print("  ".join(_pad(v, w, rt) for v, (_, w, rt) in zip(row, cols)).rstrip())


def print_detail(r: dict) -> None:
    c = compact(r)
    print(f"\n{c['ticker']}  {c['name']}  —  【{c['verdict']}】")
    if c["error"]:
        print("  " + c["error"])
        return
    print(f"  现价 {c['price']} {c['currency']} · 距 52 周高点回撤 {c['drop_from_52w_high_pct']}% · 走势：{c['timing']}")
    print("\n为什么：")
    for line in c["reasons"]:
        print("  " + line)
    if c["timing_reason"]:
        print(f"  走势：{c['timing_reason']}")
    print("\n指标：")
    for ck in c["checks"]:
        note = f"  （{ck['note']}）" if ck["note"] else ""
        print(f"  {STATUS_ICON[ck['status']]} {_pad(ck['label'], 18)} {_pad(ck['display'], 16)} 要求 {ck['rule']}{note}")
    if c["entry_plan"] and c["tier"] in ("golden_pit", "quality_wait"):
        print("\n分批价位（参考模板，非建议）：")
        for lv in c["entry_plan"]:
            print(f"  -{lv['drop_pct']:.0f}%  ${lv['price']:.2f}  占比 {lv['weight_pct']}%  {'✓ 已到达' if lv['reached'] else ''}")
    for w in c["warnings"]:
        print(f"\n⚠ {w}")
    if c["moat_view"]:
        print(f"\n行业观点（人工知识库，不计分）：{c['moat_view']}")


def main(argv: list[str] | None = None) -> None:
    _utf8_stdio()
    p = argparse.ArgumentParser(prog="golden-pit", description="美股黄金坑助手：质量门槛 × 坑位条件，缺失数据绝不当作通过。")
    p.add_argument("--version", action="version", version=f"golden-pit {__version__}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="启动网页（默认 http://127.0.0.1:5000）")
    s.add_argument("--host")
    s.add_argument("--port", type=int)

    s = sub.add_parser("scan", help="运行一次扫描并保存结果")
    s.add_argument("tickers", nargs="*", help="自定义代码（给了代码就是 custom 模式）")
    s.add_argument("--mode", choices=["live", "curated", "custom"])
    s.add_argument("--json", action="store_true", help="输出 JSON")

    s = sub.add_parser("analyze", help="即时分析单只股票（不保存）")
    s.add_argument("ticker")
    s.add_argument("--json", action="store_true")

    sub.add_parser("track", help="查看战绩追踪（前向验证）")
    sub.add_parser("mcp", help="以 MCP server 方式运行（stdio），供 Claude Code 等 AI 客户端调用")

    args = p.parse_args(argv)
    settings = Settings.from_env()

    if args.cmd in (None, "serve"):
        from dataclasses import replace
        from .server import create_app
        settings = replace(settings, host=getattr(args, "host", None) or settings.host,
                           port=getattr(args, "port", None) or settings.port)
        print(f"Golden Pit {__version__} → http://{settings.host}:{settings.port}  数据目录 {settings.data_dir}"
              f"（管理员令牌：{'已配置' if settings.admin_token else '未配置，仅本机可操作'}）")
        create_app(settings).run(host=settings.host, port=settings.port, threaded=True)
        return

    if args.cmd == "mcp":
        from .mcp_server import run
        run(settings)
        return

    from .engine import Engine
    engine = Engine(settings)

    if args.cmd == "scan":
        mode = "custom" if args.tickers else (args.mode or "curated")
        tickers = universe.parse_tickers(args.tickers) if args.tickers else []
        print(f"扫描中（{mode}）… 首次运行需要抓取数据，之后有缓存会快很多", file=sys.stderr)
        scan = engine.scanner.run_sync(mode, tickers, "cli")
        if args.json:
            print(json.dumps({**scan, "results": [compact(r) for r in scan["results"]]}, ensure_ascii=False, indent=1))
            return
        print(f"\n{scan['universe']['note']} · 完成于 {scan['finished_at']}\n")
        print_table(scan["results"])
        print(f"\n黄金坑 {scan['summary']['golden_pit']} 只。详情：golden-pit analyze <代码>，或 golden-pit serve 打开网页。")
    elif args.cmd == "analyze":
        r = engine.analyze(args.ticker)
        if args.json:
            print(json.dumps(compact(r), ensure_ascii=False, indent=1))
        else:
            print_detail(r)
    elif args.cmd == "track":
        rep = engine.track_report()
        s = rep["summary"]
        print(f"追踪 {s['total']} 条（仍在坑里 {s['open']}），满 90 天的 {s['mature_90d']} 条。基准 {rep['benchmark']}。")
        for key, label in (("return_since", "入选至今收益"), ("excess_since", "入选至今超额"), ("excess_during", "入选期间超额")):
            st = s[key]
            if st["n"]:
                print(f"  {label}：均值 {st['mean']:+.2f}%  中位数 {st['median']:+.2f}%  胜率 {st['win_rate']}%  (n={st['n']})")
        print(f"\n{rep['caveat']}")


if __name__ == "__main__":
    main()
