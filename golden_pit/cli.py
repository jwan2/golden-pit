"""命令行入口：golden-pit serve | scan | analyze | track | mcp

语言：--lang zh|en > GOLDEN_PIT_LANG > 系统区域设置。
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from dataclasses import replace

from . import __version__, universe
from .compact import compact
from .config import Settings
from .i18n import env_lang, normalize_lang, render, tr

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


def print_table(results: list[dict], lang: str) -> None:
    cols = [("ticker", 6, False), ("verdict", 18, False), ("price", 9, True), ("drop", 7, True),
            ("fin", 9, True), ("mgmt", 10, True), ("unknown", 7, True), ("timing", 10, False), ("company", 0, False)]
    print("  ".join(_pad(tr(f"cli.col.{n}", lang), w, r) for n, w, r in cols).rstrip())
    for r in results:
        c = compact(r, detail=False, lang=lang)
        drop = "—" if c["drop_from_52w_high_pct"] is None else f"{c['drop_from_52w_high_pct']:.1f}%"
        price = "—" if c["price"] is None else f"{c['price']:.2f}"
        row = [c["ticker"], c["verdict"], price, drop, c["financial_pass"], c["management_pass"],
               c["unknown_checks"], c["timing"] or "—", (c["name"] or "")[:30]]
        print("  ".join(_pad(v, w, rt) for v, (_, w, rt) in zip(row, cols)).rstrip())


def print_detail(r: dict, lang: str) -> None:
    c = compact(r, lang=lang)
    print(f"\n{c['ticker']}  {c['name']}  —  [{c['verdict']}]")
    if c["error"]:
        print("  " + str(c["error"]))
        return
    print("  " + tr("cli.price_line", lang, price=c["price"], ccy=c["currency"],
                    drop=c["drop_from_52w_high_pct"], timing=c["timing"]))
    print("\n" + tr("cli.why", lang))
    for line in c["reasons"]:
        print("  " + line)
    if c["timing_reason"]:
        print("  " + tr("cli.timing", lang, r=c["timing_reason"]))
    print("\n" + tr("cli.checks", lang))
    needs = tr("cli.needs", lang)
    for ck in c["checks"]:
        note = f"  ({ck['note']})" if ck["note"] else ""
        print(f"  {STATUS_ICON[ck['status']]} {_pad(ck['label'], 26)} {_pad(ck['display'], 18)} {needs} {ck['rule']}{note}")
    if c["entry_plan"] and c["tier"] in ("golden_pit", "quality_wait"):
        print("\n" + tr("cli.plan", lang))
        for lv in c["entry_plan"]:
            reached = tr("cli.reached", lang) if lv["reached"] else ""
            print(f"  -{lv['drop_pct']:.0f}%  ${lv['price']:.2f}  {tr('cli.weight', lang)} {lv['weight_pct']}%  {reached}")
    for w in c["warnings"]:
        print(f"\n⚠ {w}")
    if c["moat_view"]:
        print("\n" + tr("cli.moat", lang, v=c["moat_view"]))


def main(argv: list[str] | None = None) -> None:
    _utf8_stdio()
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--lang")
    known, _ = pre.parse_known_args(argv)
    lang = normalize_lang(known.lang) if known.lang else env_lang()

    p = argparse.ArgumentParser(prog="golden-pit", description=tr("cli.desc", lang))
    p.add_argument("--version", action="version", version=f"golden-pit {__version__}")
    p.add_argument("--lang", choices=["zh", "en"], help=tr("cli.h.lang", lang))
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help=tr("cli.h.serve", lang))
    s.add_argument("--host")
    s.add_argument("--port", type=int)

    s = sub.add_parser("scan", help=tr("cli.h.scan", lang))
    s.add_argument("tickers", nargs="*", help=tr("cli.h.tickers", lang))
    s.add_argument("--mode", choices=["live", "curated", "sp500", "custom"])
    s.add_argument("--json", action="store_true", help=tr("cli.h.json", lang))

    s = sub.add_parser("analyze", help=tr("cli.h.analyze", lang))
    s.add_argument("ticker")
    s.add_argument("--json", action="store_true", help=tr("cli.h.json", lang))

    sub.add_parser("track", help=tr("cli.h.track", lang))
    s = sub.add_parser("universe", help=tr("cli.h.universe", lang))
    s.add_argument("action", choices=["update"], help=tr("cli.h.universe_update", lang))
    sub.add_parser("mcp", help=tr("cli.h.mcp", lang))

    args = p.parse_args(argv)
    settings = replace(Settings.from_env(), lang=lang)

    if args.cmd in (None, "serve"):
        from .server import create_app
        settings = replace(settings, host=getattr(args, "host", None) or settings.host,
                           port=getattr(args, "port", None) or settings.port)
        tok = tr("cli.tok_set" if settings.admin_token else "cli.tok_unset", lang)
        print(tr("cli.serve", lang, v=__version__, host=settings.host, port=settings.port,
                 dir=settings.data_dir, tok=tok))
        create_app(settings).run(host=settings.host, port=settings.port, threaded=True)
        return

    if args.cmd == "mcp":
        from .mcp_server import run
        run(settings)
        return

    if args.cmd == "universe":
        n, as_of = universe.update_sp500(settings.data_dir)
        print(tr("cli.universe_updated", lang, n=n, date=as_of, path=settings.data_dir / universe.SP500_FILE))
        return

    from .engine import Engine
    engine = Engine(settings)

    if args.cmd == "scan":
        mode = "custom" if args.tickers else (args.mode or "curated")
        tickers = universe.parse_tickers(args.tickers) if args.tickers else []
        print(tr("cli.scanning", lang, mode=mode), file=sys.stderr)
        scan = engine.scanner.run_sync(mode, tickers, "cli")
        if args.json:
            body = render({**scan, "results": []}, lang)
            body["results"] = [compact(r, lang=lang) for r in scan["results"]]
            print(json.dumps(body, ensure_ascii=False, indent=1))
            return
        print("\n" + tr("cli.done", lang, note=scan["universe"]["note"], time=scan["finished_at"]) + "\n")
        print_table(scan["results"], lang)
        print("\n" + tr("cli.golden", lang, n=scan["summary"]["golden_pit"]))
    elif args.cmd == "analyze":
        r = engine.analyze(args.ticker)
        if args.json:
            print(json.dumps(compact(r, lang=lang), ensure_ascii=False, indent=1))
        else:
            print_detail(r, lang)
    elif args.cmd == "track":
        rep = render(engine.track_report(), lang)
        s = rep["summary"]
        print(tr("cli.track_head", lang, total=s["total"], open=s["open"], mature=s["mature_90d"], bench=rep["benchmark"]))
        for key in ("return_since", "excess_since", "excess_during"):
            st = s[key]
            if st["n"]:
                print(tr("cli.stat", lang, label=tr(f"cli.stat.{key}", lang), mean=f"{st['mean']:+.2f}%",
                         median=f"{st['median']:+.2f}%", win=st["win_rate"], n=st["n"]))
        print("\n" + rep["caveat"])


if __name__ == "__main__":
    main()
