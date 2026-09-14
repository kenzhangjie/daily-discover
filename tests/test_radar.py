import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import radar

HN_PAYLOAD = {"hits": [
    {"objectID": "1", "title": "低分的", "url": "https://a/1", "points": 160,
     "num_comments": 10},
    {"objectID": "2", "title": "高分的", "url": "https://a/2", "points": 300,
     "num_comments": 90},
    {"objectID": "3", "title": "没有外链的", "points": 200, "num_comments": 5},
]}

GH_ITEMS = {"items": [
    {"full_name": "a/one", "html_url": "https://github.com/a/one",
     "stargazers_count": 500, "description": "一"},
    {"full_name": "b/two", "html_url": "https://github.com/b/two",
     "stargazers_count": 900, "description": "二"},
]}

RELEASE = {"tag_name": "v1.2.3", "html_url": "https://github.com/x/y/releases/tag/v1.2.3",
           "published_at": "2026-09-13T10:00:00Z", "name": "修了点东西"}


def scripted(mapping, errors=()):
    """按 URL 子串返回预设结果;命中 errors 里的子串则抛错。"""
    seen = []

    def get(url):
        seen.append(url)
        for frag in errors:
            if frag in url:
                raise RuntimeError(f"boom {frag}")
        for frag, payload in mapping.items():
            if frag in url:
                return payload
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


class TestGithub(unittest.TestCase):
    def test_queries_each_language_and_merges_sorted(self):
        get = scripted({"search/repositories": GH_ITEMS})
        rows = radar.fetch_github_trending(get=get, languages=("python", "typescript"),
                                           today=date(2026, 9, 14))
        self.assertEqual(len(get.seen), 2)
        self.assertEqual([r["score"] for r in rows], [900, 900, 500, 500])

    def test_one_language_failing_does_not_kill_the_other(self):
        """与 market.py 按天隔离、run.collect 按人隔离是同一个判例:
        容错粒度必须在循环体内。"""
        def get(url):
            if "language:python" in url or "language%3Apython" in url:
                raise RuntimeError("rate limited")
            return GH_ITEMS

        rows = radar.fetch_github_trending(get=get, languages=("python", "typescript"),
                                           today=date(2026, 9, 14))
        self.assertEqual(len(rows), 2)

    def test_release_row_shape(self):
        rows = radar.fetch_github_releases(get=scripted({"releases/latest": RELEASE}),
                                           repos=("x/y",))
        self.assertEqual(rows[0]["title"], "x/y v1.2.3")
        self.assertEqual(rows[0]["published"], "2026-09-13")

    def test_release_without_tag_is_skipped(self):
        rows = radar.fetch_github_releases(get=scripted({"releases/latest": {}}),
                                           repos=("x/y",))
        self.assertEqual(rows, [])

    def test_one_repo_failing_does_not_kill_the_other(self):
        get = scripted({"releases/latest": RELEASE}, errors=("x/y",))
        rows = radar.fetch_github_releases(get=get, repos=("x/y", "a/b"))
        self.assertEqual(len(rows), 1)


class TestBuildRadar(unittest.TestCase):
    def test_merges_every_source(self):
        get = scripted({"hn.algolia": HN_PAYLOAD, "search/repositories": GH_ITEMS,
                        "releases/latest": RELEASE})
        pg_index = '<html><a href="powerful.html">Making Startups Powerful</a></html>'
        r = radar.build_radar(get=get,
                              pg_get=lambda u: pg_index if "articles" in u else "September 2026")
        self.assertEqual(sorted(r),
                         ["github_releases", "github_trending", "hn", "paulgraham"])
        self.assertTrue(all(r.values()))

    def test_pg_uses_its_own_injection_point(self):
        """本模块的 get 返回已解析的 JSON,PG 那条要的是 HTML 文本 ——
        合成一个注入点会让其中一边永远解析失败。"""
        get = scripted({"hn.algolia": HN_PAYLOAD, "search/repositories": GH_ITEMS,
                        "releases/latest": RELEASE})
        r = radar.build_radar(get=get)          # 注入了 get 但没给 pg_get
        self.assertEqual(r["paulgraham"], [])   # 整条跳过,不落回默认取数器
        self.assertTrue(r["hn"])

    def test_one_source_down_leaves_the_others(self):
        get = scripted({"search/repositories": GH_ITEMS, "releases/latest": RELEASE},
                       errors=("hn.algolia",))
        r = radar.build_radar(get=get)
        self.assertEqual(r["hn"], [])
        self.assertTrue(r["github_trending"])

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
