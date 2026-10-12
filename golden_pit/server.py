"""Flask API + 静态前端。"""
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
from .scheduler import Scheduler
from .scoring import TIERS

WEB_DIR = Path(__file__).resolve().parent / "web"
LOOPBACK = {"127.0.0.1", "::1"}


def create_app(settings: Settings, engine: Engine | None = None, scheduler: Scheduler | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.json.sort_keys = False          # 保持档位等字典的定义顺序
    engine = engine or Engine(settings)
    store, scanner, ai, tracker = engine.store, engine.scanner, engine.ai, engine.tracker
    th, sc = engine.thresholds, engine.screener
    scheduler = scheduler or Scheduler(settings.slots, lambda: scanner.start("live", trigger="scheduled"))
    if settings.scheduler_enabled:
        scheduler.start()

    # ------------------------------------------------------------ 鉴权
    def admin_required(fn):
        """写操作（触发扫描/即时分析）需要管理员令牌。
        没配置令牌时只允许本机访问，绝不会"默认开放"到公网。"""
        @wraps(fn)
        def wrapper(*a, **kw):
            if settings.admin_token:
                given = request.headers.get("X-Admin-Token", "")
                if not hmac.compare_digest(given.encode(), settings.admin_token.encode()):
                    return jsonify(error="需要管理员令牌（X-Admin-Token）"), 403
            elif request.remote_addr not in LOOPBACK:
                return jsonify(error="服务器未配置 GOLDEN_PIT_ADMIN_TOKEN，仅允许本机操作"), 403
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
        return jsonify(
            version=__version__, schema_version=SCHEMA_VERSION,
            thresholds=th.to_dict(), screener=sc.to_dict(), tiers=TIERS,
            auth_required=bool(settings.admin_token),
            local_only=not settings.admin_token,
            scheduler=scheduler.info(),
            ai=ai.cfg.public(),
        )

    @app.get("/api/scan/latest")
    def latest():
        return jsonify(scan=store.latest())

    @app.get("/api/scan/status")
    def scan_status():
        return jsonify(scanner.status())

    @app.get("/api/history")
    def history():
        return jsonify(items=store.list_history())

    @app.get("/api/history/<name>")
    def history_detail(name):
        scan = store.load_history(name)
        if scan is None:
            abort(404)
        return jsonify(scan=scan)

    @app.get("/api/track")
    def track():
        return jsonify(engine.track_report())

    @app.get("/api/ai")
    def ai_all():
        """最新扫描中所有已有的 AI 研判（附是否过期）。"""
        scan = store.latest()
        if not scan:
            return jsonify(items={})
        by_t = {r["ticker"]: r for r in scan["results"]}
        out = {}
        for t, rec in ai.cached_many(list(by_t)).items():
            out[t] = {**rec, "stale": ai.is_stale(rec, by_t[t])}
        return jsonify(items=out)

    # ------------------------------------------------------------ 写 API
    @app.post("/api/ai/<ticker>")
    @admin_required
    def ai_generate(ticker):
        scan = store.latest()
        result = next((r for r in (scan or {}).get("results", []) if r["ticker"] == ticker.upper()), None)
        if result is None:
            return jsonify(error="最新扫描里没有这只股票，请先扫描"), 404
        try:
            rec = ai.generate(result)
        except AIError as e:
            return jsonify(error=str(e)), 502
        return jsonify(item={**rec, "stale": False})

    @app.post("/api/scan")
    @admin_required
    def start_scan():
        body = request.get_json(silent=True) or {}
        mode = body.get("mode", "live")
        if mode not in ("live", "curated", "custom"):
            return jsonify(error="mode 必须是 live / curated / custom"), 400
        tickers = universe.parse_tickers(body.get("tickers", [])) if mode == "custom" else []
        if mode == "custom" and not tickers:
            return jsonify(error="没有识别到有效的美股代码（如 AAPL, BRK-B）"), 400
        if not scanner.start(mode, tickers):
            return jsonify(error="已有扫描正在进行"), 409
        return jsonify(started=True), 202

    @app.post("/api/analyze/<ticker>")
    @admin_required
    def analyze_one(ticker):
        tickers = universe.parse_tickers([ticker])
        if not tickers:
            return jsonify(error="无效的股票代码"), 400
        return jsonify(result=engine.analyze(tickers[0]))

    # ------------------------------------------------------------ 导出
    def _latest_or_404():
        scan = store.latest()
        if not scan:
            abort(404)
        return scan

    @app.get("/api/export.csv")
    def export_csv():
        scan = _latest_or_404()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["代码", "公司", "结论", "现价", "回撤%", "P/B", "前瞻PE", "财务通过", "管理层通过", "未知项", "行业", "扫描时间"])
        by = lambda r, k: next((c for c in r["checks"] if c["key"] == k), None)  # noqa: E731
        for r in scan["results"]:
            v = r["verdict"]
            pb, fpe = by(r, "pb"), by(r, "forward_pe")
            w.writerow([_safe_cell(x) for x in (
                r["ticker"], r["name"], v["label"], r["price"],
                None if r["drop_from_high_pct"] is None else round(r["drop_from_high_pct"], 1),
                pb["value"] if pb else None, fpe["value"] if fpe else None,
                f"{v['fin_pass']}/{v['fin_total']}", f"{v['mgmt_pass']}/3", v["unknown_count"],
                r["industry"], scan["finished_at"])])
        return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="golden_pit_{scan["scan_id"]}.csv"'})

    @app.get("/api/export.md")
    def export_md():
        scan = _latest_or_404()
        lines = [f"# 黄金坑扫描 {scan['finished_at']}", "",
                 f"候选池：{scan['universe']['note']}　|　标的 {len(scan['results'])} 只", ""]
        for tier, meta in TIERS.items():
            rows = [r for r in scan["results"] if r["verdict"]["tier"] == tier]
            if not rows or tier in ("error", "n_a"):
                continue
            lines += [f"## {meta['label']}（{len(rows)}）", "", f"> {meta['desc']}", "",
                      "| 代码 | 公司 | 现价 | 回撤 | 财务 | 管理层 | 未知项 |", "|---|---|---|---|---|---|---|"]
            for r in rows:
                v, d = r["verdict"], r["drop_from_high_pct"]
                lines.append(f"| {r['ticker']} | {_md(r['name'])} | {r['price']} | "
                             f"{'—' if d is None else f'{d:.1f}%'} | {v['fin_pass']}/{v['fin_total']} | "
                             f"{v['mgmt_pass']}/3 | {v['unknown_count']} |")
            lines.append("")
        lines.append("> 本工具仅为筛选辅助，不构成投资建议。")
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
