import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import blogs

AN_NEWS = "https://www.anthropic.com/news"


def next_f(objs):
    """把一组 post 对象包成 Anthropic 那种 self.__next_f 流式载荷。
    字段顺序照真实结构:publishedOn → slug → subjects → summary → title。"""
    body = ",".join(
        '{"_type":"post","publishedOn":"%s","slug":{"_type":"slug","current":"%s"},'
        '"subjects":[],"summary":"%s","title":"%s"}' % (o["published"], o["slug"],
                                                        o.get("summary", ""), o["title"])
        for o in objs)
    payload = json.dumps('a:["x",[%s]]' % body)
    return f'<html><script>self.__next_f.push([1,{payload}])</script></html>'


def sitemap(pairs):
    urls = "".join(f"<url><loc>{u}</loc><lastmod>{m}</lastmod></url>" for u, m in pairs)
    return f"<urlset>{urls}</urlset>"


class TestAnthropic(unittest.TestCase):
    def test_parses_publishedon_slug_title_summary(self):
        page = next_f([{"published": "2026-09-01T23:31:00.000Z", "slug": "aaa",
                        "title": "标题 A", "summary": "摘要 A"}])
        rows = blogs.parse_anthropic(page, AN_NEWS)
        self.assertEqual(rows[0]["slug"], "aaa")
        self.assertEqual(rows[0]["ts"], "2026-09-01T23:31:00Z")
        self.assertEqual(rows[0]["title"], "标题 A")
        self.assertEqual(rows[0]["summary"], "摘要 A")
        self.assertEqual(rows[0]["url"], "https://www.anthropic.com/news/aaa")

    def test_missing_next_f_raises_rather_than_returning_empty(self):
        with self.assertRaises(ValueError):
            blogs.parse_anthropic("<html>改版了</html>", AN_NEWS)

    def test_publishedon_beats_sitemap_lastmod(self):
        """这是本模块存在的理由。实测 claude-corps 真实发布于 2026-06-11,
        sitemap 的 lastmod 却是 2026-09-11 —— lastmod 是「最后修改」。
        不校正的话,整站重建会把几百篇旧文按今天的日期灌进时间线。"""
        page = next_f([{"published": "2026-06-11T00:00:00.000Z", "slug": "old-post",
                        "title": "一篇老文"}])
        xml = sitemap([("https://www.anthropic.com/news/old-post",
                        "2026-09-11T02:45:01.000Z")])

        def get(url):
            return xml if "sitemap" in url else page

        rows = blogs.fetch_anthropic(get=get)
        self.assertEqual(rows[0]["ts"], "2026-06-11T00:00:00Z")
        self.assertEqual(rows[0]["title"], "一篇老文")

    def test_post_missing_from_the_index_falls_back_to_lastmod(self):
        """列表页会漏文章。只靠它就永远看不到那些,所以候选集来自 sitemap。"""
        xml = sitemap([("https://www.anthropic.com/news/not-in-index",
                        "2026-09-11T02:45:01.000Z")])

        def get(url):
            return xml if "sitemap" in url else next_f([])

        rows = blogs.fetch_anthropic(get=get)
        self.assertEqual(rows[0]["ts"], "2026-09-11T02:45:01Z")
        self.assertEqual(rows[0]["title"], "Not in index")

    def test_section_index_pages_are_not_posts(self):
        xml = sitemap([("https://www.anthropic.com/news", "2026-09-11T00:00:00.000Z"),
                       ("https://www.anthropic.com/news/", "2026-09-11T00:00:00.000Z"),
                       ("https://www.anthropic.com/pricing", "2026-09-11T00:00:00.000Z")])

        def get(url):
            return xml if "sitemap" in url else next_f([])

        self.assertEqual(blogs.fetch_anthropic(get=get), [])

    def test_capped_so_a_site_rebuild_cannot_flood_the_day(self):
        many = [(f"https://www.anthropic.com/news/p{i}", "2026-09-11T00:00:0%d.000Z" % (i % 10))
                for i in range(40)]

        def get(url):
            return sitemap(many) if "sitemap" in url else next_f([])

        self.assertEqual(len(blogs.fetch_anthropic(get=get)), blogs.SITE_CAP)

    def test_sitemap_down_falls_back_to_the_index(self):
        page = next_f([{"published": "2026-09-01T00:00:00.000Z", "slug": "aaa",
                        "title": "标题"}])

        def get(url):
            if "sitemap" in url:
                raise RuntimeError("sitemap 挂了")
            return page

        rows = blogs.fetch_anthropic(get=get)
        self.assertEqual(rows[0]["slug"], "aaa")


class TestOpenAI(unittest.TestCase):
    def test_only_index_slugs_count_as_posts(self):
        xml = sitemap([
            ("https://openai.com/index/real-post/", "2026-09-12T00:00:00.000Z"),
            ("https://openai.com/news/", "2026-09-11T00:00:00.000Z"),
            ("https://openai.com/news/engineering/", "2026-09-11T00:00:00.000Z"),
        ])
        rows = blogs.parse_openai_sitemap(xml)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://openai.com/index/real-post/")
        self.assertEqual(rows[0]["title"], "Real post")

    def test_merges_both_sitemaps_and_dedupes(self):
        xml = sitemap([("https://openai.com/index/same/", "2026-09-12T00:00:00.000Z")])
        rows = blogs.fetch_openai(get=lambda url: xml)
        self.assertEqual(len(rows), 1)

    def test_one_sitemap_failing_does_not_kill_the_other(self):
        xml = sitemap([("https://openai.com/index/ok/", "2026-09-12T00:00:00.000Z")])

        def get(url):
            if "company" in url:
                raise RuntimeError("403")
            return xml

        self.assertEqual(len(blogs.fetch_openai(get=get)), 1)


class TestFetchBlog(unittest.TestCase):
    def test_builds_posts_for_the_timeline(self):
        page = next_f([{"published": "2026-09-01T23:31:00.000Z", "slug": "aaa",
                        "title": "标题 A", "summary": "摘要 A"}])
        xml = sitemap([("https://www.anthropic.com/news/aaa", "2026-09-01T23:31:00.000Z")])

        def get(url):
            return xml if "sitemap" in url else page

        posts = blogs.fetch_blog(None, {"id": "anthropic", "name": "Anthropic"}, get=get)
        self.assertEqual(posts[0].channel, "blog")
        self.assertEqual(posts[0].id, "blog:anthropic:aaa")
        self.assertEqual(posts[0].author_name, "Anthropic")
        self.assertIn("摘要 A", posts[0].text)

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            blogs.fetch_blog(None, {"id": "someone-else"}, get=lambda u: "")


class TestPaulGraham(unittest.TestCase):
    INDEX = ('<html><a href="index.html">Home</a><a href="articles.html">Articles</a>'
             '<a href="powerful.html">Making Startups Powerful</a>'
             '<a href="prepare.html">How Universities Should Prepare Founders</a></html>')

    def test_skips_navigation_links(self):
        rows = blogs.fetch_pg_latest(
            get=lambda url: self.INDEX if "articles" in url else "<html>September 2026</html>",
            top_n=5)
        self.assertEqual([r["title"] for r in rows],
                         ["Making Startups Powerful", "How Universities Should Prepare Founders"])

    def test_month_comes_from_the_essay_body(self):
        rows = blogs.fetch_pg_latest(
            get=lambda url: self.INDEX if "articles" in url else "<p>September 2026</p> 正文",
            top_n=1)
        self.assertEqual(rows[0]["note"], "Sep 2026")

    def test_essay_without_a_month_still_shows_up(self):
        """月份只是补充信息。拿不到就空着,别把这一条整个丢掉。"""
        rows = blogs.fetch_pg_latest(
            get=lambda url: self.INDEX if "articles" in url else "<p>没有月份</p>", top_n=1)
        self.assertEqual(rows[0]["note"], "")
        self.assertTrue(rows[0]["url"].endswith("powerful.html"))


if __name__ == "__main__":
    unittest.main()
