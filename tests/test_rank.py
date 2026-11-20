"""黄金坑分档：只在黄金坑内部排先后，不改变判定；老战绩记录按当时的扫描补分档。"""
from golden_pit import analyze, rank
from golden_pit.config import Thresholds
from golden_pit.i18n import render
from golden_pit.track import Tracker

TH = Thresholds()


def verdict(fin=5, mgmt=3, anchors=2, unknown=0, tier="golden_pit"):
    return {"tier": tier, "fin_pass": fin, "fin_total": 5, "mgmt_pass": mgmt, "mgmt_total": 3,
            "anchors_pass": anchors, "unknown_count": unknown, "sort_key": [0, -(fin + mgmt), -40.0]}


def grade(state="basing", **kw):
    return rank.rank(verdict(**kw), {"state": state})["grade"]


def test_all_margins_met_is_a():
    rk = rank.rank(verdict(), {"state": "rebounding"})
    assert rk["grade"] == "a" and rk["gaps"] == 0 and all(c["ok"] for c in rk["conditions"])


def test_grade_thresholds_count_gaps():
    assert grade(fin=4) == "a"                                   # 差 1 项仍是 A
    assert grade(fin=4, anchors=1) == "b"                        # 差 2 项
    assert grade(fin=4, anchors=1, mgmt=2) == "c"                # 差 3 项
    assert grade(state="falling", fin=4, anchors=1, mgmt=2, unknown=1) == "c"


def test_falling_and_unknown_timing_are_gaps():
    assert rank.rank(verdict(), {"state": "falling"})["gaps"] == 1
    assert rank.rank(verdict(), None)["gaps"] == 1               # 没有走势数据不算"不在下跌"
    assert rank.rank(verdict(), {"state": "unclear"})["gaps"] == 0


def test_only_golden_pits_are_ranked():
    assert rank.rank(verdict(tier="quality_wait"), {"state": "basing"}) is None


def test_gap_labels_render_in_both_languages():
    rk = rank.rank(verdict(fin=4, anchors=1), {"state": "falling"})
    assert render(rk["gap_labels"], "zh") == ["财务 4/5", "估值只有 1 项合格", "仍在下跌"]
    assert render(rk["label"], "en") == "C · later"


def test_ranking_never_changes_the_verdict(raw, mutate):
    """分档只是排序：判定档位、通过数都不受影响。"""
    r = analyze.analyze_raw(raw, TH)
    assert r["verdict"]["tier"] == "golden_pit" and r["verdict"]["rank"]["grade"] in rank.GRADES
    worse = analyze.analyze_raw(mutate(lambda x: x.info.update({"priceToBook": 16.7})), TH)
    assert worse["verdict"]["tier"] == "golden_pit"
    assert worse["verdict"]["rank"]["gaps"] == r["verdict"]["rank"]["gaps"] + 1
    assert r["verdict"]["sort_key"] < worse["verdict"]["sort_key"]   # 差距少的排前面


def test_error_and_other_tiers_sort_keys_stay_comparable(raw):
    ok = analyze.analyze_raw(raw, TH)
    err = analyze.error_result("BAD", "x")
    assert len(ok["verdict"]["sort_key"]) == len(err["verdict"]["sort_key"]) and ok["verdict"]["sort_key"] < err["verdict"]["sort_key"]


# ---------------- 战绩里的分档
def _result(t, price, tier="golden_pit", **kw):
    v = verdict(tier=tier, **kw)
    return {"ticker": t, "name": t, "price": price, "verdict": v, "timing": {"state": "basing"}}


def test_new_track_entries_record_grade(tmp_path):
    tr = Tracker(tmp_path)
    r = rank.apply(_result("A", 10))
    tr.update({"scan_id": "1", "finished_at": "2026-01-01T00:00:00+00:00", "results": [r]}, lambda t: 100.0)
    assert tr.load()["entries"][0]["grade"] == "a"


def test_old_entries_backfilled_from_history(tmp_path):
    """分档上线前的记录：从当时的历史扫描补算；历史已清理的记为未分级。"""
    history = {"scan_1.json": {"results": [_result("OLD", 10, fin=4, anchors=1)]}}
    tr = Tracker(tmp_path, history=history.get)
    tr._save({"entries": [
        {"id": "OLD-1", "ticker": "OLD", "status": "open", "flagged_at": "2026-01-01T00:00:00+00:00",
         "flag_price": 10, "bench_flag": 100, "last_price": 11, "last_at": "2026-01-20T00:00:00+00:00", "bench_last": 100},
        {"id": "GONE-0", "ticker": "GONE", "status": "open", "flagged_at": "2026-01-01T00:00:00+00:00",
         "flag_price": 10, "bench_flag": 100, "last_price": 9, "last_at": "2026-01-20T00:00:00+00:00", "bench_last": 100},
    ]})
    data = tr.ensure_grades()
    assert {e["ticker"]: e["grade"] for e in data["entries"]} == {"OLD": "b", "GONE": None}
    by = Tracker.report(data)["summary"]["by_grade"]
    assert by["b"]["n"] == 1 and by["b"]["excess_mean"] == 10.0 and by["none"]["n"] == 1 and by["a"] == {"n": 0}


def test_report_summarises_exits_and_reentries(tmp_path):
    tr = Tracker(tmp_path)
    px = {"SPY": 100.0}
    sc = lambda rs, sid: {"scan_id": sid, "finished_at": f"2026-01-0{sid}T00:00:00+00:00", "results": rs}  # noqa: E731
    tr.update(sc([rank.apply(_result("A", 10))], "1"), px.get)
    tr.update(sc([_result("A", 12, tier="quality_wait")], "2"), px.get)
    tr.update(sc([rank.apply(_result("A", 11))], "3"), px.get)
    rep = Tracker.report(tr.load())
    assert sorted(e["nth"] for e in rep["entries"]) == [1, 2]
    assert rep["summary"]["exits"]["quality_wait"] == {"n": 1, "excess_during_mean": 20.0}


def test_scans_saved_before_grading_get_ranked_on_read(tmp_path):
    from golden_pit.store import Store
    st = Store(tmp_path)
    old = _result("OLD", 10, anchors=1)
    old["verdict"]["sort_key"] = [0, -8, -40.0]
    st.save_scan({"scan_id": "20260101_000000", "results": [old, {"ticker": "Q", "verdict": {"tier": "fail", "sort_key": [3, 0, 0]}}]})
    got = st.latest()["results"]
    assert got[0]["verdict"]["rank"]["grade"] == "a" and got[0]["verdict"]["sort_key"] == [0, 1, -8, -40.0]
    assert got[1]["verdict"]["rank"] is None
    assert st.load_history("scan_20260101_000000.json")["results"][0]["verdict"]["rank"]["grade"] == "a"
