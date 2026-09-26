import sys, os, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import run, models


def P(pid, ts, channel="twitter"):
    return models.Post(id=pid, channel=channel, author_handle="a", author_name="A",
                       text=pid, ts=ts, url="u", engagement={}, media=[], repost_of=None)



EMPTY_RADAR = {"hn": [], "github_trending": [], "github_releases": [], "paulgraham": []}

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


class TestSlowChannels(unittest.TestCase):
    """播客/订阅/博客是周更级的。36 小时窗口会让它们一周里有六天整个是空的 ——
    2026-09-15 实测两个小宇宙播客最新一集是 12 天和 17 天前。"""

    def test_slow_channel_keeps_a_week_while_twitter_keeps_36h(self):
        fetchers = {
            "podcast": lambda c, p: [P("ep", "2026-09-10T00:00:00Z", "podcast")],
            "twitter": lambda c, p: [P("tw", "2026-09-10T00:00:00Z", "twitter")],
        }
        sources = {"podcast": [{"id": "x"}], "twitter": [{"id": "y"}]}
        posts, stats = run.collect(None, sources, "2026-09-14T00:00:00Z",
                                   fetchers=fetchers,
                                   slow_cutoff_iso="2026-09-08T00:00:00Z")
        self.assertEqual([p.id for p in posts], ["ep"])
        self.assertEqual(stats["podcast"]["count"], 1)
        self.assertEqual(stats["twitter"]["count"], 0)

    def test_every_slow_channel_is_a_real_channel(self):
        """打错一个名字不会报错,只会让那个渠道悄悄退回 36 小时。"""
        for ch in run.SLOW_CHANNELS:
            self.assertIn(ch, run.DEFAULT_FETCHERS)

    def test_omitting_slow_cutoff_falls_back_to_the_normal_one(self):
        fetchers = {"podcast": lambda c, p: [P("ep", "2026-09-10T00:00:00Z", "podcast")]}
        posts, _ = run.collect(None, {"podcast": [{"id": "x"}]},
                               "2026-09-14T00:00:00Z", fetchers=fetchers)
        self.assertEqual(posts, [])


class TestFetchPodcastRouting(unittest.TestCase):
    """中英文播客在名单上是同一个渠道,靠有没有 feed 分流。"""

    def test_feed_goes_to_rss_but_keeps_the_podcast_channel(self):
        """走 rss.py 但 channel 必须是 podcast —— 否则英文播客会在页面上
        显示成「订阅」,和小宇宙那几个分到两个渠道去。"""
        seen = {}

        def fake_rss(client, person, get=None, channel="rss"):
            seen["channel"] = channel
            return [P("e", "2026-09-10T00:00:00Z", channel)]

        with mock.patch.object(run.rss, "fetch_rss", fake_rss):
            out = run.fetch_podcast(None, {"id": "20vc", "feed": "https://f/x.xml"})
        self.assertEqual(seen["channel"], "podcast")
        self.assertEqual(out[0].channel, "podcast")

    def test_no_feed_goes_to_xiaoyuzhou(self):
        calls = []
        with mock.patch.object(run.xiaoyuzhou, "fetch_xiaoyuzhou",
                               lambda c, p, get=None: calls.append(p) or []):
            run.fetch_podcast(None, {"id": "626b46ea9cbbf0451cf5a962"})
        self.assertEqual(calls[0]["id"], "626b46ea9cbbf0451cf5a962")

    def test_rss_channel_defaults_to_rss_for_the_plain_rss_list(self):
        """同一个 fetch_rss 服务两个渠道,默认值不能被上面那条改掉。"""
        import rss as rss_mod
        rows = rss_mod.parse_rss(
            "<item><title>T</title><link>https://a/1</link>"
            "<pubDate>Mon, 08 Sep 2026 00:00:00 GMT</pubDate></item>",
            {"id": "s", "name": "S"})
        self.assertEqual(rows[0].channel, "rss")


class TestMainCompletes(unittest.TestCase):
    def test_main_fetches_and_uploads_without_tikhub(self):
        # 2026-09-26 起没有渠道再走 TikHub:main 不该再构造它的客户端、
        # 也不该再依赖 TIKHUB_API_KEY —— 不打任何 TikHub 的补丁也要跑完。
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

        with mock.patch.object(run, "DEFAULT_FETCHERS", fetchers), \
             mock.patch.object(run, "load_market", return_value={"polymarket": [], "ipo": []}), \
             mock.patch.object(run.radar_mod, "build_radar", return_value=EMPTY_RADAR), \
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

        uploaded = {}

        def fake_upload(key, payload, **kw):
            uploaded[key] = payload

        with mock.patch.object(run, "DEFAULT_FETCHERS", fetchers), \
             mock.patch.object(run, "load_market", return_value={"polymarket": [], "ipo": []}), \
             mock.patch.object(run.radar_mod, "build_radar", return_value=EMPTY_RADAR), \
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
        self.assertEqual(market, {"polymarket": [], "ipo": [], "earnings": []})


class TestNoRealNetwork(unittest.TestCase):
    def test_main_never_reaches_the_network_for_radar(self):
        """run.main() 会调 radar_mod.build_radar(),它内部逐源容错所以不会报错,
        只会静默打三次真实 HTTP —— 测试跑 8 秒就是这么来的。任何新接的
        补充品数据源都要在这里补一条 mock。"""
        import radar
        with mock.patch.object(radar.urllib.request, "urlopen",
                               side_effect=AssertionError("测试里不许打真网络")):
            self.assertEqual(radar.build_radar(),
                             {"hn": [], "github_trending": [], "github_releases": [], "paulgraham": []})


if __name__ == "__main__":
    unittest.main()
