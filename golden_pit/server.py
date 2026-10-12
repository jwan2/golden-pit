"""Flask API + 静态前端。

所有结果在存储里都是与语言无关的（i18n 消息），这里按请求语言渲染：
  ?lang=zh|en  >  Accept-Language  >  服务器默认（GOLDEN_PIT_LANG）
"""
from __future__ import annotations

import csv
import hmac
import io
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from . import SCHEMA_VERSION, __version__, universe
from .ai import AIError
from .config import Settings
from .engine import Engine
from .i18n import LANGS, M, msg_of, normalize_lang, render, tr
from .scheduler import Scheduler
from .scoring import TIER_ORDER, TIERS

WEB_DIR = Path(__file__).resolve().parent / "web"
LOOPBACK = {"127.0.0.1", "::1"}


def create_app(settings: Settings, engine: Engine | None = None, scheduler: Scheduler | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.json.sort_keys = False          # 保持档位等字典的定义顺序
    engine = engine or Engine(settings)
    store, scanner, ai = engine.store, engine.scanner, engine.ai
    th, sc = engine.thresholds, engine.screener
    scheduler = scheduler or Scheduler(settings.slots, lambda: scanner.start("live", trigger="scheduled"))
    if settings.scheduler_enabled:
        scheduler.start()

    def lang() -> str:
        q = request.args.get("lang")
        if q:
            return normalize_lang(q)
        best = request.accept_languages.best_match(["zh", "zh-CN", "en"])
        return normalize_lang(best) if best else settings.lang

    def out(status: int = 200, **payload):
        return jsonify(render(payload, lang())), status

    def err(msg, status: int):
        return out(status, error=msg)

    # ------------------------------------------------------------ 鉴权
    def admin_required(fn):
        """写操作（触发扫描/即时分析/AI）需要管理员令牌。
        没配置令牌时只允许本机访问，绝不会"默认开放"到公网。"""
        @wraps(fn)
        def wrapper(*a, **kw):
            if settings.admin_token:
                given = request.headers.get("X-Admin-Token", "")
                if not hmac.compare_digest(given.encode(), settings.admin_token.encode()):
                    return err(M("e.admin_token"), 403)
            elif request.remote_addr not in LOOPBACK:
                return err(M("e.local_only"), 403)
            return fn(*a, **kw)
        return wrapper

    @app.after_request
    def security_headers(resp):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["Vary"] = "Accept-Language"
        return resp

    # ------------------------------------------------------------ 页面
    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(WEB_DIR, name)

    # ------------------------------------------------------------ 只读 API
    @app.get("/api/health")
    def health():
        return jsonify(status="ok", version=__version__)

    @app.get("/api/config")
    def config():
        return out(
            version=__version__, schema_version=SCHEMA_VERSION, lang=lang(), langs=list(LANGS),
            thresholds=th.to_dict(), screener=sc.to_dict(), tiers=TIERS, tier_order=TIER_ORDER,
            auth_required=bool(settings.admin_token), local_only=not settings.admin_token,
            scheduler=scheduler.info(), ai=ai.cfg.public(),
        )

    @app.get("/api/scan/latest")
    def latest():
        return out(scan=store.latest())

    @app.get("/api/scan/status")
    def scan_status():
        return out(**scanner.status())

    @app.get("/api/history")
    def history():
        return out(items=store.list_history())

    @app.get("/api/history/<name>")
    def history_detail(name):
        scan = store.load_history(name)
        if scan is None:
            abort(404)
        return out(scan=scan)

    @app.get("/api/track")
    def track():
        return out(**engine.track_report())

    @app.get("/api/ai")
    def ai_all():
        """最新扫描中所有已有的 AI 研判（当前语言；附是否过期）。"""
        scan = store.latest()
        if not scan:
            return out(items={})
        by_t = {r["ticker"]: r for r in scan["results"]}
        items = {t: {**rec, "stale": ai.is_stale(rec, by_t[t])}
                 for t, rec in ai.cached_many(list(by_t), lang()).items()}
        return out(items=items)

    # ------------------------------------------------------------ 写 API
    @app.post("/api/ai/<ticker>")
    @admin_required
    def ai_generate(ticker):
        scan = store.latest()
        result = next((r for r in (scan or {}).get("results", []) if r["ticker"] == ticker.upper()), None)
        if result is None:
            return err(M("e.not_in_scan"), 404)
        try:
            rec = ai.generate(result, lang())
        except AIError as e:
            return err(msg_of(e), 502)
        return out(item={**rec, "stale": False})

    @app.post("/api/scan")
    @admin_required
    def start_scan():
        body = request.get_json(silent=True) or {}
        mode = body.get("mode", "live")
        if mode not in ("live", "curated", "custom"):
            return err(M("e.mode"), 400)
        tickers = universe.parse_tickers(body.get("tickers", [])) if mode == "custom" else []
        if mode == "custom" and not tickers:
            return err(M("e.no_custom"), 400)
        if not scanner.start(mode, tickers):
            return err(M("e.busy"), 409)
        return out(202, started=True)

    @app.post("/api/analyze/<ticker>")
    @admin_required
    def analyze_one(ticker):
        tickers = universe.parse_tickers([ticker])
        if not tickers:
            return err(M("e.bad_ticker"), 400)
        return out(result=engine.analyze(tickers[0]))

    # ------------------------------------------------------------ 导出
    def _latest_or_404():
        scan = store.latest()
        if not scan:
            abort(404)
        return scan

    @app.get("/api/export.csv")
    def export_csv():
        lg = lang()
        scan = render(_latest_or_404(), lg)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([tr(f"x.csv.{k}", lg) for k in
                    ("ticker", "name", "verdict", "price", "drop", "pb", "fpe", "fin", "mgmt", "unknown",
                     "timing", "industry", "time")])
        by = lambda r, k: next((c for c in r["checks"] if c["key"] == k), None)  # noqa: E731
        for r in scan["results"]:
            v = r["verdict"]
            pb, fpe = by(r, "pb"), by(r, "forward_pe")
            w.writerow([_safe_cell(x) for x in (
                r["ticker"], r["name"], v["label"], r["price"],
                None if r["drop_from_high_pct"] is None else round(r["drop_from_high_pct"], 1),
                pb["value"] if pb else None, fpe["value"] if fpe else None,
                f"{v['fin_pass']}/{v['fin_total']}", f"{v['mgmt_pass']}/3", v["unknown_count"],
                (r.get("timing") or {}).get("label", ""), r["industry"], scan["finished_at"])])
        return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="golden_pit_{scan["scan_id"]}.csv"'})

    @app.get("/api/export.md")
    def export_md():
        lg = lang()
        scan = render(_latest_or_404(), lg)
        tiers = render(TIERS, lg)
        lines = [f"# {tr('x.md.title', lg, time=scan['finished_at'])}", "",
                 tr("x.md.universe", lg, note=scan["universe"]["note"], n=len(scan["results"])), ""]
        for tier in TIER_ORDER:
            rows = [r for r in scan["results"] if r["verdict"]["tier"] == tier]
            if not rows or tier in ("error", "n_a"):
                continue
            lines += [f"## {tiers[tier]['label']} ({len(rows)})", "", f"> {tiers[tier]['desc']}", "",
                      tr("x.md.header", lg), "|---|---|---|---|---|---|---|---|"]
            for r in rows:
                v, d = r["verdict"], r["drop_from_high_pct"]
                lines.append(f"| {r['ticker']} | {_md(r['name'])} | {r['price']} | "
                             f"{'—' if d is None else f'{d:.1f}%'} | {v['fin_pass']}/{v['fin_total']} | "
                             f"{v['mgmt_pass']}/3 | {v['unknown_count']} | {(r.get('timing') or {}).get('label', '')} |")
            lines.append("")
        lines.append(tr("x.md.disclaimer", lg))
        return Response("\n".join(lines), mimetype="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="golden_pit_{scan["scan_id"]}.md"'})

    app.extensions["golden_pit"] = {"engine": engine, "scheduler": scheduler}
    return app


def _safe_cell(v):
    """防止 CSV 公式注入：以 = + - @ 开头的文本加前导单引号。"""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def _md(s: str) -> str:
    return str(s).replace("|", "\\|")


def create_app_from_env() -> Flask:
    """WSGI 入口：gunicorn "golden_pit.server:create_app_from_env()"。
    注意：扫描状态和调度器都在进程内，所以只能用 1 个 worker（多线程即可）。"""
    return create_app(Settings.from_env())
