"""访问统计：同一人一天只算一个访客，爬虫不算，换天清指纹，重启不丢数，不存 IP。"""
from golden_pit.visits import Visits

UA = "Mozilla/5.0 (iPhone) MicroMessenger/8.0"
DAY = 86400


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_dedupes_visitors_and_ignores_bots(tmp_path):
    v = Visits(tmp_path / "v.json", clock=Clock())
    for _ in range(3):
        v.hit("1.1.1.1", UA)
    v.hit("2.2.2.2", UA)
    assert not v.hit("3.3.3.3", "Googlebot/2.1") and not v.hit("4.4.4.4", "curl/8.0") and not v.hit("5.5.5.5", "")
    s = v.stats()
    assert s["today"]["pv"] == 4 and s["today"]["uv"] == 2


def test_new_day_resets_today_and_keeps_history(tmp_path):
    clock = Clock()
    v = Visits(tmp_path / "v.json", clock=clock)
    v.hit("1.1.1.1", UA)
    clock.t += DAY
    v.hit("1.1.1.1", UA)                      # 第二天同一个人：又算一个访客
    s = v.stats()
    assert s["today"]["uv"] == 1 and s["yesterday"]["uv"] == 1
    assert s["total"] == {"pv": 2, "uv": 2, "since": s["yesterday"]["date"]}
    assert len(v.data["today"]["seen"]) == 1  # 前一天的指纹已清掉
    assert len(s["series"]) == 30 and s["series"][-1]["date"] == s["today"]["date"]


def test_persists_across_restart_without_storing_ip(tmp_path):
    clock = Clock()
    Visits(tmp_path / "v.json", clock=clock).hit("9.8.7.6", UA)
    v2 = Visits(tmp_path / "v.json", clock=clock)
    v2.hit("9.8.7.6", UA)                     # 重启后同一天同一人：不重复算
    assert v2.stats()["today"] == {"date": v2.stats()["today"]["date"], "pv": 2, "uv": 1}
    assert "9.8.7.6" not in (tmp_path / "v.json").read_text()


def test_corrupt_file_starts_fresh(tmp_path):
    (tmp_path / "v.json").write_text("{oops")
    v = Visits(tmp_path / "v.json", clock=Clock())
    assert v.hit("1.1.1.1", UA) and v.stats()["today"]["pv"] == 1


def test_index_counts_and_stats_api(tmp_path):
    from golden_pit.server import create_app
    from tests.test_api import make_engine

    s, engine = make_engine(tmp_path)
    v = Visits(tmp_path / "v.json", clock=Clock())
    c = create_app(s, engine=engine, visits=v).test_client()
    c.get("/", headers={"User-Agent": UA, "CF-Connecting-IP": "1.2.3.4"})
    c.get("/", headers={"User-Agent": UA, "CF-Connecting-IP": "1.2.3.4"})
    c.head("/", headers={"User-Agent": UA})
    c.get("/", headers={"User-Agent": "Baiduspider"})
    body = c.get("/api/stats").get_json()
    assert body["today"]["pv"] == 2 and body["today"]["uv"] == 1 and body["tz"] == "UTC+8"
