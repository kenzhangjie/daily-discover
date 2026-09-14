import sys, os, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import run, models


def P(pid, ts, channel="twitter"):
    return models.Post(id=pid, channel=channel, author_handle="a", author_name="A",
                       text=pid, ts=ts, url="u", engagement={}, media=[], repost_of=None)


class TestCollect(unittest.TestCase):
    def test_drops_older_than_cutoff(self):
        fetchers = {"twitter": lambda c, p: [P("new", "2026-09-13T00:00:00Z"),
                                             P("old", "2026-09-01T00:00:00Z")]}
        posts, stats = run.collect(None, {"twitter": [{"id": "a", "name": "A"}]},
                                   "2026-09-12T00:00:00Z", fetchers=fetchers)
        self.assertEqual([p.id for p in posts], ["new"])
        self.assertEqual(stats["twitter"]["count"], 1)
        self.assertTrue(stats["twitter"]["ok"])

    def test_failure_marks_channel_not_ok_and_continues(self):
        def boom(c, p):
            raise RuntimeError("401")
        fetchers = {"twitter": boom,
                    "xhs": lambda c, p: [P("x", "2026-09-13T00:00:00Z", "xhs")]}
        sources = {"twitter": [{"id": "a", "name": "A"}], "xhs": [{"id": "b", "name": "B"}]}
        posts, stats = run.collect(None, sources, "2026-09-12T00:00:00Z", fetchers=fetchers)
        self.assertFalse(stats["twitter"]["ok"])
        self.assertIn("401", stats["twitter"]["error"])
        self.assertEqual(stats["xhs"]["count"], 1)
        self.assertEqual(len(posts), 1)

    def test_zero_posts_but_ok_is_distinguishable(self):
        fetchers = {"wechat": lambda c, p: []}
        posts, stats = run.collect(None, {"wechat": [{"id": "g", "name": "G"}]},
                                   "2026-09-12T00:00:00Z", fetchers=fetchers)
        self.assertEqual(stats["wechat"]["count"], 0)
        self.assertTrue(stats["wechat"]["ok"])


class TestBalanceFailureDegrades(unittest.TestCase):
    def test_balance_failure_still_completes_fetch_and_upload(self):
        # 余额查询是纯告警用途,不该有一票否决权 —— 它挂了,抓取和上传照常跑完。
        class FakeClient:
            calls = 3

            def balance(self):
                raise RuntimeError("balance endpoint 500")

        from datetime import datetime, timezone
        fresh_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fetchers = {
            "twitter": lambda c, p: [P("t1", fresh_ts, "twitter")],
            "xhs": lambda c, p: [],
            "wechat": lambda c, p: [],
        }
        uploaded = {}

        def fake_upload(key, payload, **kw):
            uploaded[key] = payload

        with mock.patch("run.tikhub.TikHub", return_value=FakeClient()), \
             mock.patch.object(run, "DEFAULT_FETCHERS", fetchers), \
             mock.patch.object(run, "load_market", return_value={"polymarket": [], "ipo": []}), \
             mock.patch("publish.fetch_index", return_value={"days": [], "authors": []}), \
             mock.patch("publish.upload_r2", side_effect=fake_upload):
            rc = run.main([])

        self.assertEqual(rc, 0)
        self.assertIn("discover/index.json", uploaded)
        shard_keys = [k for k in uploaded if k != "discover/index.json"]
        self.assertEqual(len(shard_keys), 1)
        self.assertEqual(len(uploaded[shard_keys[0]]["posts"]), 1)


class TestStatsCountMatchesIndexTotal(unittest.TestCase):
    def test_cross_channel_duplicate_keeps_count_and_total_consistent(self):
        # collect() 在 dedup 之前统计 count,而 index.total 是 dedup 之后的
        # len(posts) —— 真实产物里出现过 total=118 但各渠道 count 合计 122。
        # 构造一对跨渠道重复(同文本,不同渠道),验证去重后重新统计。
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)

        def iso(dt):
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        t_early = iso(now - timedelta(hours=2))
        t_mid = iso(now - timedelta(hours=1))
        t_late = iso(now - timedelta(minutes=10))

        def post(pid, channel, text, ts):
            return models.Post(id=pid, channel=channel, author_handle="a", author_name="A",
                               text=text, ts=ts, url=pid, engagement={}, media=[], repost_of=None)

        fetchers = {
            "twitter": lambda c, p: [
                post("dup-tw", "twitter", "same breaking news", t_early),
                post("solo-tw", "twitter", "unrelated tweet", t_mid),
            ],
            "xhs": lambda c, p: [post("dup-xhs", "xhs", "same breaking news", t_late)],
            "wechat": lambda c, p: [],
        }

        class FakeClient:
            calls = 3

            def balance(self):
                return 5.0

        uploaded = {}

        def fake_upload(key, payload, **kw):
            uploaded[key] = payload

        with mock.patch("run.tikhub.TikHub", return_value=FakeClient()), \
             mock.patch.object(run, "DEFAULT_FETCHERS", fetchers), \
             mock.patch.object(run, "load_market", return_value={"polymarket": [], "ipo": []}), \
             mock.patch("publish.fetch_index", return_value={"days": [], "authors": []}), \
             mock.patch("publish.upload_r2", side_effect=fake_upload):
            rc = run.main([])

        self.assertEqual(rc, 0)
        shard_key = [k for k in uploaded if k != "discover/index.json"][0]
        shard = uploaded[shard_key]
        day_entry = uploaded["discover/index.json"]["days"][0]

        self.assertEqual(sum(st["count"] for st in shard["stats"].values()), day_entry["total"])
        # xhs 那条被去重(同文重复、ts 更晚)清空,但渠道仍要留在 stats 里,ok 不受影响
        self.assertIn("xhs", shard["stats"])
        self.assertEqual(shard["stats"]["xhs"]["count"], 0)
        self.assertTrue(shard["stats"]["xhs"]["ok"])


class TestLoadMarket(unittest.TestCase):
    def test_import_failure_degrades_instead_of_raising(self):
        # here 指向一个不存在 ipo-earnings/fetch_market.py 的目录,
        # 触发真实 ImportError,而不是靠 mock 假装失败
        missing_here = "/tmp/does-not-exist-daily-discover-test"
        market = run.load_market(missing_here)
        self.assertEqual(market, {"polymarket": [], "ipo": []})


if __name__ == "__main__":
    unittest.main()
