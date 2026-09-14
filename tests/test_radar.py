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


TRENDING_HTML = """
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a data-hydro-click="{&quot;x&quot;:1}" href="/owner/slow">owner/slow</a>
  </h2>
  <p class="col-9 color-fg-muted">一个 &amp; 符号</p>
  <a href="/owner/slow/stargazers" class="x"><svg>s</svg> 1,200</a>
  <span>50 stars today</span>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a data-hydro-click="{}" href="/owner/hot">owner/hot</a>
  </h2>
  <a href="/owner/hot/stargazers" class="x"><svg>s</svg> 900</a>
  <span>2,600 stars today</span>
</article>
"""

RELEASES_ATOM = """<feed><entry>
  <title>v1.2.3</title>
  <updated>2026-09-13T10:00:00Z</updated>
  <link rel="alternate" href="https://github.com/x/y/releases/tag/v1.2.3"/>
</entry><entry>
  <title>v1.2.2</title><updated>2026-09-01T10:00:00Z</updated>
</entry></feed>"""


class TestGithubTrending(unittest.TestCase):
    def test_href_is_not_the_first_attribute_on_the_anchor(self):
        """<a> 上 data-hydro-click 排在 href 前面,直接接 `<a href=` 匹配不到 ——
        线上就是这么解析出 0 行的。"""
        rows = radar.parse_trending(TRENDING_HTML)
        self.assertEqual({r["title"] for r in rows}, {"owner/slow", "owner/hot"})

    def test_sorted_by_stars_gained_today_not_total(self):
        rows = radar.fetch_github_trending(get=scripted({"github.com/trending": TRENDING_HTML}))
        self.assertEqual([r["title"] for r in rows], ["owner/hot", "owner/slow"])

    def test_star_count_survives_the_svg_between_link_and_number(self):
        rows = radar.parse_trending(TRENDING_HTML)
        slow = next(r for r in rows if r["title"] == "owner/slow")
        self.assertEqual(slow["score"], 1200)
        self.assertEqual(slow["today"], 50)

    def test_description_entities_are_unescaped(self):
        slow = next(r for r in radar.parse_trending(TRENDING_HTML) if r["title"] == "owner/slow")
        self.assertEqual(slow["note"], "一个 & 符号")

    def test_parsing_nothing_raises_instead_of_returning_empty(self):
        """GitHub 会改版这个页面。静默返回空会被读成「今天没有 trending」,
        一个坏掉的源就这么消失几个月。"""
        with self.assertRaises(ValueError):
            radar.parse_trending("<html>改版了</html>")

    def test_does_not_touch_api_github_com(self):
        """api.github.com 在云沙盒里被代理整个拦掉,带不带 token 都 403。"""
        get = scripted({"github.com/trending": TRENDING_HTML})
        radar.fetch_github_trending(get=get)
        self.assertTrue(get.seen)
        for url in get.seen:
            self.assertNotIn("api.github.com", url)


class TestGithubReleases(unittest.TestCase):
    def test_takes_only_the_newest_entry(self):
        rows = radar.parse_releases_atom(RELEASES_ATOM, "x/y")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "x/y v1.2.3")
        self.assertEqual(rows[0]["published"], "2026-09-13")
        self.assertEqual(rows[0]["url"], "https://github.com/x/y/releases/tag/v1.2.3")

    def test_empty_feed_is_not_an_error(self):
        self.assertEqual(radar.parse_releases_atom("<feed></feed>", "x/y"), [])

    def test_one_repo_failing_does_not_kill_the_other(self):
        get = scripted({"releases.atom": RELEASES_ATOM}, errors=("anthropics",))
        rows = radar.fetch_github_releases(
            get=get, repos=("anthropics/claude-code", "openai/openai-python"))
        self.assertEqual(len(rows), 1)

    def test_uses_the_atom_feed_not_the_api(self):
        get = scripted({"releases.atom": RELEASES_ATOM})
        radar.fetch_github_releases(get=get, repos=("x/y",))
        self.assertEqual(get.seen, ["https://github.com/x/y/releases.atom"])


PG_INDEX = '<html><a href="powerful.html">Making Startups Powerful</a></html>'


def ALL_SOURCES(url):
    """一个取数器喂四种载荷。"""
    if "hn.algolia" in url:
        return json.dumps(HN_PAYLOAD)
    if "github.com/trending" in url:
        return TRENDING_HTML
    if "releases.atom" in url:
        return RELEASES_ATOM
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
