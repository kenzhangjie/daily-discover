import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import models
import rss
import xiaoyuzhou

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def rss_xml():
    with open(os.path.join(FIX, "rss_substack.xml"), encoding="utf-8") as fh:
        return fh.read()


def xyz_data():
    with open(os.path.join(FIX, "xiaoyuzhou_next_data.json"), encoding="utf-8") as fh:
        return json.load(fh)


def xyz_page():
    return ('<html><body><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(xyz_data(), ensure_ascii=False)
            + "</script></body></html>")


RSS_PERSON = {"id": "elenaverna", "name": "Elena Verna",
              "feed": "https://www.elenaverna.com/feed", "note": ""}
XYZ_PERSON = {"id": "626b46ea9cbbf0451cf5a962", "name": "张小珺", "note": ""}


class TestRSS(unittest.TestCase):
    def test_parses_real_substack_feed(self):
        posts = rss.parse_rss(rss_xml(), RSS_PERSON)
        self.assertEqual(len(posts), 3)
        p = posts[0]
        self.assertIsInstance(p, models.Post)
        self.assertEqual(p.channel, "rss")
        self.assertEqual(p.author_name, "Elena Verna")
        self.assertTrue(p.url.startswith("http"), p.url)
        self.assertTrue(p.ts.endswith("Z"), p.ts)

    def test_strips_html_from_summary(self):
        for p in rss.parse_rss(rss_xml(), RSS_PERSON):
            self.assertNotIn("<p", p.text)
            self.assertNotIn("</", p.text)
            self.assertNotIn("&amp;", p.text)

    def test_long_body_is_truncated(self):
        """有的 feed 把全文塞进 description,一条能顶满整屏。"""
        for p in rss.parse_rss(rss_xml(), RSS_PERSON):
            self.assertLess(len(p.text), 1200, p.text[:80])

    def test_item_without_date_is_dropped_not_stamped_with_now(self):
        """PG 的社区镜像就是这样:条目没有日期。用 now() 顶替会让 2023 年的
        旧文每天都显示成刚发布。"""
        xml = "<rss><channel><item><title>无日期</title>" \
              "<link>https://x/1</link></item></channel></rss>"
        self.assertEqual(rss.parse_rss(xml, RSS_PERSON), [])

    def test_one_bad_item_does_not_kill_the_feed(self):
        xml = ("<rss><channel>"
               "<item><title>坏的</title><link>https://x/1</link></item>"
               "<item><title>好的</title><link>https://x/2</link>"
               "<pubDate>Fri, 12 Sep 2026 10:00:00 +0000</pubDate></item>"
               "</channel></rss>")
        posts = rss.parse_rss(xml, RSS_PERSON)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].url, "https://x/2")

    def test_atom_link_lives_in_an_attribute(self):
        xml = ('<feed><entry><title>Atom 条目</title>'
               '<link rel="alternate" href="https://a/1"/>'
               "<updated>2026-09-12T10:00:00Z</updated></entry></feed>")
        posts = rss.parse_rss(xml, RSS_PERSON)
        self.assertEqual(posts[0].url, "https://a/1")

    def test_fetch_uses_the_feed_field(self):
        seen = []

        def fake_get(url):
            seen.append(url)
            return rss_xml()

        rss.fetch_rss(None, RSS_PERSON, get=fake_get)
        self.assertEqual(seen, ["https://www.elenaverna.com/feed"])

    def test_default_get_sends_nonempty_user_agent(self):
        """注入 get 的单测碰不到默认取数器那一层,而不少 feed 主机对
        Python-urllib 直接 403 —— 线上只会表现为这个源天天是空的。"""
        captured = []

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return b"<rss></rss>"

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            return _Resp()

        orig = rss.urllib.request.urlopen
        rss.urllib.request.urlopen = fake_urlopen
        try:
            rss._default_get("https://example.invalid/feed")
        finally:
            rss.urllib.request.urlopen = orig
        self.assertTrue(captured[0].get_header("User-agent"))

    def test_error_propagates_so_collect_marks_it_failed(self):
        def boom(url):
            raise RuntimeError("down")

        with self.assertRaises(RuntimeError):
            rss.fetch_rss(None, RSS_PERSON, get=boom)


class TestXiaoyuzhou(unittest.TestCase):
    def test_parses_real_embedded_data(self):
        posts = xiaoyuzhou.parse_xiaoyuzhou(xyz_data(), XYZ_PERSON)
        self.assertEqual(len(posts), 3)
        p = posts[0]
        self.assertEqual(p.channel, "podcast")
        self.assertTrue(p.id.startswith("podcast:"))
        self.assertTrue(p.url.startswith("https://www.xiaoyuzhoufm.com/episode/"))
        self.assertTrue(p.ts.startswith("2026-"), p.ts)

    def test_engagement_from_clap_and_comment(self):
        p = xiaoyuzhou.parse_xiaoyuzhou(xyz_data(), XYZ_PERSON)[0]
        self.assertEqual(p.engagement["likes"], 542)
        self.assertEqual(p.engagement["comments"], 187)

    def test_missing_next_data_raises_rather_than_returning_empty(self):
        """页面改版要当场知道。静默返回空会被读成「这档播客今天没更新」。"""
        with self.assertRaises(ValueError):
            xiaoyuzhou.extract_next_data("<html><body>改版了</body></html>")

    def test_one_bad_episode_does_not_kill_the_podcast(self):
        data = xyz_data()
        eps = data["props"]["pageProps"]["podcast"]["episodes"]
        data["props"]["pageProps"]["podcast"]["episodes"] = [{"eid": None}] + eps
        self.assertEqual(len(xiaoyuzhou.parse_xiaoyuzhou(data, XYZ_PERSON)), 3)

    def test_fetch_requests_the_podcast_page(self):
        seen = []

        def fake_get(url):
            seen.append(url)
            return xyz_page()

        posts = xiaoyuzhou.fetch_xiaoyuzhou(None, XYZ_PERSON, get=fake_get)
        self.assertEqual(
            seen, ["https://www.xiaoyuzhoufm.com/podcast/626b46ea9cbbf0451cf5a962"])
        self.assertEqual(len(posts), 3)


if __name__ == "__main__":
    unittest.main()
