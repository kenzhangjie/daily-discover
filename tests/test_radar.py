import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import radar

HN_PAYLOAD = {"hits": [
    {"objectID": "1", "title": "低分的", "url": "https://a/1", "points": 160,
     "num_comments": 10},
    {"objectID": "2", "title": "高分的", "url": "https://a/2", "points": 300,
     "num_comments": 90},
    {"objectID": "3", "title": "没有外链的", "points": 200, "num_comments": 5},
]}


def scripted(mapping, errors=()):
    """按 URL 子串返回预设**文本**(取数器统一返回文本,JSON 由调用方解);
    命中 errors 里的子串则抛错。"""
    seen = []

    def get(url):
        seen.append(url)
        for frag in errors:
            if frag in url:
                raise RuntimeError(f"boom {frag}")
        for frag, payload in mapping.items():
            if frag in url:
                return payload if isinstance(payload, str) else json.dumps(payload)
        raise AssertionError(f"没有为 {url} 准备返回")

    get.seen = seen
    return get


class TestHN(unittest.TestCase):
    def test_sorted_by_score_desc(self):
        rows = radar.fetch_hn(get=scripted({"hn.algolia.com": HN_PAYLOAD}))
        self.assertEqual([r["score"] for r in rows], [300, 200, 160])

    def test_story_without_url_falls_back_to_the_hn_item_page(self):
        rows = radar.fetch_hn(get=scripted({"hn.algolia.com": HN_PAYLOAD}))
        no_url = next(r for r in rows if r["score"] == 200)
        self.assertEqual(no_url["url"], "https://news.ycombinator.com/item?id=3")

    def test_always_carries_a_discussion_link(self):
        for r in radar.fetch_hn(get=scripted({"hn.algolia.com": HN_PAYLOAD})):
            self.assertIn("news.ycombinator.com/item", r["discuss"])

    def test_query_carries_the_threshold_and_time_window(self):
        get = scripted({"hn.algolia.com": HN_PAYLOAD})
        radar.fetch_hn(get=get, min_points=150, hours=24, now=1_000_000)
        url = get.seen[0]
        self.assertIn("points%3E150", url)
        self.assertIn(f"created_at_i%3E{1_000_000 - 86400}", url)

    def test_empty_hits_is_not_an_error(self):
        self.assertEqual(radar.fetch_hn(get=scripted({"hn.algolia": {"hits": []}})), [])


GH_RADAR = {
    "trending": [
        {"title": "owner/small", "url": "https://github.com/owner/small",
         "score": 900, "note": "小的"},
        {"title": "owner/big", "url": "https://github.com/owner/big",
         "score": 5000, "note": "大的"},
    ],
    "releases": [
        {"title": "x/y v1.2.3", "url": "https://github.com/x/y/releases/tag/v1.2.3",
         "published": "2026-09-13", "note": ""},
    ],
}


class TestGithubViaR2Bridge(unittest.TestCase):
    """沙盒里 github.com 和 api.github.com 全被代理拦掉(2026-09-14 两次实测 403),
    只放行 raw.githubusercontent.com。所以这两组由 GitHub Actions 预先算好摆到 R2,
    这里只读那个 JSON。"""

    def test_reads_the_bridge_json_not_github(self):
        get = scripted({"radar/github.json": GH_RADAR})
        radar.fetch_github_trending(get=get)
        radar.fetch_github_releases(get=get)
        self.assertTrue(get.seen)
        for url in get.seen:
            self.assertNotIn("github.com/", url.replace("raw.githubusercontent.com/", ""))
            self.assertIn("radar/github.json", url)

    def test_trending_sorted_by_stars(self):
        rows = radar.fetch_github_trending(get=scripted({"radar/github.json": GH_RADAR}))
        self.assertEqual([r["title"] for r in rows], ["owner/big", "owner/small"])

    def test_releases_passed_through(self):
        rows = radar.fetch_github_releases(get=scripted({"radar/github.json": GH_RADAR}))
        self.assertEqual(rows[0]["title"], "x/y v1.2.3")

    def test_bridge_missing_keys_is_not_an_error(self):
        """Actions 那边某一组取不到时会写成空数组,这里不能因此炸掉。"""
        get = scripted({"radar/github.json": {}})
        self.assertEqual(radar.fetch_github_trending(get=get), [])
        self.assertEqual(radar.fetch_github_releases(get=get), [])

    def test_bridge_unreachable_degrades_in_build_radar(self):
        def get(url):
            if "radar/github.json" in url:
                raise RuntimeError("R2 挂了")
            return ALL_SOURCES(url)

        r = radar.build_radar(get=get)
        self.assertEqual(r["github_trending"], [])
        self.assertEqual(r["github_releases"], [])
        self.assertTrue(r["hn"])


PG_INDEX = '<html><a href="powerful.html">Making Startups Powerful</a></html>'


def ALL_SOURCES(url):
    """一个取数器喂四种载荷。"""
    if "hn.algolia" in url:
        return json.dumps(HN_PAYLOAD)
    if "radar/github.json" in url:
        return json.dumps(GH_RADAR)
    if "articles.html" in url:
        return PG_INDEX
    return "September 2026"


class TestBuildRadar(unittest.TestCase):
    def test_merges_every_source(self):
        """一个注入的 get 服务全部四个源:HN 是 JSON、trending 是 HTML、
        releases 是 Atom、PG 是 HTML —— 取数器统一返回文本才做得到。"""
        r = radar.build_radar(get=ALL_SOURCES)
        self.assertEqual(sorted(r),
                         ["github_releases", "github_trending", "hn", "paulgraham"])
        self.assertTrue(all(r.values()))

    def test_one_source_down_leaves_the_others(self):
        def get(url):
            if "hn.algolia" in url:
                raise RuntimeError("down")
            return ALL_SOURCES(url)

        r = radar.build_radar(get=get)
        self.assertEqual(r["hn"], [])
        self.assertTrue(r["github_trending"])
        self.assertTrue(r["paulgraham"])

    def test_everything_down_returns_an_empty_skeleton_not_an_exception(self):
        """榜单是补充品,必须降级。这与 publish.upload_r2 必须抛错是刻意相反的:
        那是唯一交付出口,静默失败等于页面停更而没人知道。"""
        def boom(url):
            raise RuntimeError("all down")

        r = radar.build_radar(get=boom)
        self.assertEqual(r, {"hn": [], "github_trending": [], "github_releases": [], "paulgraham": []})

    def test_default_get_sends_nonempty_user_agent(self):
        captured = []

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return b"{}"

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            return _Resp()

        orig = radar.urllib.request.urlopen
        radar.urllib.request.urlopen = fake_urlopen
        try:
            radar._default_get("https://example.invalid/x")
        finally:
            radar.urllib.request.urlopen = orig
        self.assertTrue(captured[0].get_header("User-agent"))


if __name__ == "__main__":
    unittest.main()
