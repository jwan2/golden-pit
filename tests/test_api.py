"""API / 扫描 / 存储 / 调度（全部离线，用假数据源）。"""
from datetime import datetime

import pytest

from golden_pit import universe
from golden_pit.config import Settings
from golden_pit.engine import Engine
from golden_pit.scheduler import ET, next_run
from golden_pit.server import create_app
from golden_pit.store import Store
from tests.conftest import healthy_raw


def make_settings(tmp_path, token=""):
    return Settings(data_dir=tmp_path, admin_token=token, host="127.0.0.1", port=0,
                    scheduler_enabled=False, workers=2, slots=("08:30", "16:15"))


def fake_fetch(ticker):
    if ticker == "BAD":
        from golden_pit.providers.yahoo import FetchError
        from golden_pit.i18n import M
        raise FetchError(M("err.no_quote", t="BAD"))
    r = healthy_raw(ticker)
    if ticker == "FLAT":
        r.info["fiftyTwoWeekHigh"] = 101.0
    return r


PRICES = {"SPY": 500.0}


def fake_price(ticker):
    return PRICES.get(ticker, 100.0)


def make_engine(tmp_path, token="", ai=None):
    s = make_settings(tmp_path, token)
    return s, Engine(s, ai=ai, fetch=fake_fetch, price=fake_price)


@pytest.fixture
def app(tmp_path):
    s, engine = make_engine(tmp_path)
    return create_app(s, engine=engine)


def run_scan(app, tickers=("AAA", "FLAT", "BAD")):
    return app.extensions["golden_pit"]["engine"].scanner.run_sync("custom", list(tickers), "test")


def test_empty_state(app):
    c = app.test_client()
    assert c.get("/api/scan/latest").get_json() == {"scan": None}
    assert c.get("/api/health").get_json()["status"] == "ok"
    cfg = c.get("/api/config").get_json()
    assert cfg["thresholds"]["drop_from_high_min_pct"] == 30.0 and "golden_pit" in cfg["tiers"]


def test_scan_end_to_end_and_exports(app):
    scan = run_scan(app)
    tiers = {r["ticker"]: r["verdict"]["tier"] for r in scan["results"]}
    assert tiers == {"AAA": "golden_pit", "FLAT": "quality_wait", "BAD": "error"}
    assert scan["summary"]["golden_pit"] == 1 and scan["summary"]["error"] == 1
    assert scan["results"][0]["ticker"] == "AAA"          # 黄金坑排最前，抓取失败排最后
    assert scan["results"][-1]["ticker"] == "BAD"

    c = app.test_client()
    assert c.get("/api/scan/latest").get_json()["scan"]["scan_id"] == scan["scan_id"]
    csv = c.get("/api/export.csv?lang=zh").data.decode("utf-8-sig")
    assert "AAA" in csv and "黄金坑" in csv
    assert "Golden pit" in c.get("/api/export.csv?lang=en").data.decode("utf-8-sig")
    assert "黄金坑" in c.get("/api/export.md?lang=zh").data.decode()
    assert "Golden pit" in c.get("/api/export.md?lang=en").data.decode()
    assert len(c.get("/api/history").get_json()["items"]) == 1


def test_second_scan_reports_changes(app):
    run_scan(app)
    scan2 = run_scan(app, ("AAA", "NEW"))
    assert scan2["changes"]["has_previous"] and scan2["changes"]["new_tickers"] == ["NEW"]
    assert scan2["changes"]["removed_tickers"] == ["BAD", "FLAT"]


def test_history_rejects_path_traversal(app):
    c = app.test_client()
    assert c.get("/api/history/..%2F..%2Fsettings.json").status_code == 404
    assert c.get("/api/history/scan_x.json").status_code == 404


def test_write_endpoints_local_only_without_token(app):
    c = app.test_client()
    remote = {"REMOTE_ADDR": "203.0.113.9"}
    assert c.post("/api/scan", json={"mode": "curated"}, environ_overrides=remote).status_code == 403
    assert c.post("/api/analyze/AAPL", environ_overrides=remote).status_code == 403


def test_write_endpoints_require_token_when_configured(tmp_path):
    s, engine = make_engine(tmp_path, token="s3cret")
    app = create_app(s, engine=engine)
    c = app.test_client()
    assert c.post("/api/analyze/AAA").status_code == 403
    assert c.post("/api/analyze/AAA", headers={"X-Admin-Token": "wrong"}).status_code == 403
    ok = c.post("/api/analyze/AAA", headers={"X-Admin-Token": "s3cret"})
    assert ok.status_code == 200 and ok.get_json()["result"]["verdict"]["tier"] == "golden_pit"
    # 令牌不得通过 URL 传递
    assert c.post("/api/analyze/AAA?token=s3cret").status_code == 403


def test_scan_validation(app):
    c = app.test_client()
    assert c.post("/api/scan", json={"mode": "nope"}).status_code == 400
    assert c.post("/api/scan", json={"mode": "custom", "tickers": ["<script>", "!!"]}).status_code == 400


def test_security_headers(app):
    r = app.test_client().get("/api/health")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["Cache-Control"] == "no-store"


def test_csv_formula_injection_guard():
    from golden_pit.server import _safe_cell
    assert _safe_cell("=1+1") == "'=1+1" and _safe_cell("Apple") == "Apple" and _safe_cell(5) == 5


def test_parse_tickers():
    assert universe.parse_tickers("aapl, MSFT;brk-b  <x> TOOLONGX aapl") == ["AAPL", "MSFT", "BRK-B"]


def test_atomic_save_keeps_old_file_on_failure(tmp_path):
    st = Store(tmp_path)
    st.save_scan({"scan_id": "20260101_000000", "results": [], "finished_at": "x"})
    with pytest.raises(ValueError):  # NaN 不允许写入
        st.save_scan({"scan_id": "20260102_000000", "results": [float("nan")], "finished_at": "y"})
    assert st.latest()["scan_id"] == "20260101_000000"
    assert not list(tmp_path.glob("*.tmp"))


def test_scheduler_skips_weekends():
    fri_evening = datetime(2026, 10, 9, 17, 0, tzinfo=ET)      # 周五 17:00 ET
    nxt = next_run(fri_evening, ("08:30", "16:15"))
    assert nxt.weekday() == 0 and (nxt.hour, nxt.minute) == (8, 30)
    mid = datetime(2026, 10, 12, 10, 0, tzinfo=ET)             # 周一 10:00 → 当天 16:15
    assert next_run(mid, ("08:30", "16:15")).hour == 16


# ---------------- AI 端点
def _ai_app(tmp_path, token=""):
    import json as _j
    from golden_pit.ai import AIConfig, AIService
    from tests.test_ai_plan import GOOD
    svc = AIService(AIConfig(provider="cli", model="m"), tmp_path, runner=lambda p, c: _j.dumps(GOOD))
    s, engine = make_engine(tmp_path, token, ai=svc)
    return create_app(s, engine=engine), engine.scanner


def test_ai_endpoints_flow(tmp_path):
    app, sc = _ai_app(tmp_path)
    c = app.test_client()
    assert c.post("/api/ai/AAA").status_code == 404                 # 还没扫描
    sc.run_sync("custom", ["AAA"], "t")
    rec = c.post("/api/ai/AAA").get_json()["item"]
    assert rec["analysis"]["thesis"] and rec["stale"] is False
    assert c.get("/api/ai").get_json()["items"]["AAA"]["stale"] is False
    assert c.get("/api/config").get_json()["ai"]["enabled"] is True


def test_ai_generate_requires_admin(tmp_path):
    app, sc = _ai_app(tmp_path, token="tok")
    sc.run_sync("custom", ["AAA"], "t")
    c = app.test_client()
    assert c.post("/api/ai/AAA").status_code == 403
    assert c.post("/api/ai/AAA", headers={"X-Admin-Token": "tok"}).status_code == 200


def test_ai_disabled_returns_clear_error(app):
    run_scan(app, ("AAA",))
    r = app.test_client().post("/api/ai/AAA?lang=zh")
    assert r.status_code == 502 and "未启用" in r.get_json()["error"]
    assert "not enabled" in app.test_client().post("/api/ai/AAA?lang=en").get_json()["error"]


def test_auto_ai_runs_after_scan(tmp_path):
    import json as _j
    from golden_pit.ai import AIConfig, AIService
    from tests.test_ai_plan import GOOD
    svc = AIService(AIConfig(provider="cli", model="m", auto_top=2), tmp_path, runner=lambda p, c: _j.dumps(GOOD))
    _, engine = make_engine(tmp_path, ai=svc)
    engine.scanner.run_sync("custom", ["AAA", "FLAT", "BAD"], "t")
    assert svc.cached("AAA") and svc.cached("FLAT") and svc.cached("BAD") is None   # 失败标的不研判


# ---------------- 战绩追踪（经由扫描）
def test_scan_updates_track_record(app):
    run_scan(app)                                        # AAA 入选黄金坑 @100，SPY @500
    eng = app.extensions["golden_pit"]["engine"]

    def fetch_up(t):
        r = fake_fetch(t)
        r.info.update(currentPrice=120.0, fiftyTwoWeekHigh=190.0)   # +20%，回撤仍 36.8% → 还在坑里
        return r
    eng.scanner.fetch = fetch_up
    PRICES["SPY"] = 550.0                                # SPY +10%
    try:
        run_scan(app, ("AAA",))
    finally:
        PRICES["SPY"] = 500.0
    rep = app.test_client().get("/api/track").get_json()
    e = rep["entries"][0]
    assert e["ticker"] == "AAA" and e["status"] == "open"
    assert e["return_since_pct"] == 20.0 and e["excess_since_pct"] == 10.0
    assert rep["summary"]["total"] == 1


def _patch_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("GOLDEN_PIT_DATA", str(tmp_path))
    monkeypatch.setattr("golden_pit.engine.CachedProvider.raw", lambda self, t, refresh=False: fake_fetch(t))
    monkeypatch.setattr("golden_pit.engine.CachedProvider.price", lambda self, t: fake_price(t))


def test_cli_analyze_json(tmp_path, monkeypatch, capsys):
    import json as _j
    from golden_pit import cli
    _patch_provider(monkeypatch, tmp_path)
    cli.main(["--lang", "en", "analyze", "aaa", "--json"])
    out = _j.loads(capsys.readouterr().out)
    assert out["ticker"] == "AAA" and out["tier"] == "golden_pit" and len(out["checks"]) == 11


def test_cli_scan_table(tmp_path, monkeypatch, capsys):
    from golden_pit import cli
    _patch_provider(monkeypatch, tmp_path)
    cli.main(["--lang", "zh", "scan", "AAA", "FLAT"])
    out = capsys.readouterr().out
    assert "AAA" in out and "黄金坑" in out and "优质 · 待价" in out
    cli.main(["--lang", "en", "scan", "AAA", "FLAT"])
    out = capsys.readouterr().out
    assert "Golden pit" in out and "Quality · wait" in out


def test_mcp_tools(tmp_path):
    from golden_pit.mcp_server import build_tools
    _, engine = make_engine(tmp_path)
    tools = build_tools(engine)
    assert set(tools) == {"scan_stocks", "analyze_stock", "latest_results", "track_record", "methodology"}
    assert "error" in tools["latest_results"]()
    res = tools["scan_stocks"](tickers=["aaa", "flat"])
    assert res["results"][0]["ticker"] == "AAA" and res["results"][0]["tier"] == "golden_pit"
    assert tools["latest_results"](tier="quality_wait")["results"][0]["ticker"] == "FLAT"
    a = tools["analyze_stock"]("AAA")
    assert a["verdict"] == "Golden pit" and a["checks"]
    assert tools["analyze_stock"]("<bad>")["error"]
    assert tools["track_record"]()["summary"]["total"] == 1
    assert tools["methodology"]()["thresholds"]["drop_from_high_min_pct"] == 30.0


def test_mcp_tools_register_with_sdk(tmp_path):
    """真实注册到 MCP SDK，确认工具签名能被 SDK 接受。"""
    pytest.importorskip("mcp")
    import asyncio
    from golden_pit.mcp_server import build_tools
    try:
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server
    _, engine = make_engine(tmp_path)
    server = Server(name="golden-pit")
    for fn in build_tools(engine).values():
        server.tool()(fn)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"scan_stocks", "analyze_stock"} <= names


def test_sp500_mode_end_to_end(tmp_path, monkeypatch):
    from golden_pit import universe
    monkeypatch.setattr(universe, "sp500", lambda data_dir=None: (["AAA", "FLAT"], {"$m": "u.sp500", "p": {"n": 2, "date": "x"}}))
    s, engine = make_engine(tmp_path)
    app = create_app(s, engine=engine)
    c = app.test_client()
    assert c.post("/api/scan", json={"mode": "sp500"}).status_code == 202
    for _ in range(200):
        if not engine.scanner.status()["running"]:
            break
        import time; time.sleep(0.02)
    scan = c.get("/api/scan/latest?lang=en").get_json()["scan"]
    assert scan["universe"]["mode"] == "sp500" and "S&P 500" in scan["universe"]["note"]
    assert c.get("/api/config").get_json()["scheduler"]["mode"] == "live"


def test_slim_list_and_lazy_detail(app):
    run_scan(app, ("AAA",))
    eng = app.extensions["golden_pit"]["engine"]
    scan = eng.store.latest()
    scan["results"][0]["chart"] = {"dates": ["2026-01-01"], "closes": [1.0]}
    eng.store.save_scan(scan)
    c = app.test_client()
    assert "chart" in c.get("/api/scan/latest").get_json()["scan"]["results"][0]
    slim = c.get("/api/scan/latest?slim=1").get_json()["scan"]["results"][0]
    assert "chart" not in slim and "reasons" not in slim["verdict"] and slim["slim"] is True
    assert {"key", "group", "status", "value", "display"} == set(slim["checks"][0])
    full = c.get("/api/scan/latest/aaa?lang=en").get_json()["result"]
    assert full["chart"]["closes"] == [1.0] and full["verdict"]["label"] == "Golden pit"
    assert c.get("/api/scan/latest/ZZZZ").status_code == 404
