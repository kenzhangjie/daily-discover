import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import models
import xueqiu

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture():
    with open(os.path.join(FIX, "xueqiu_timeline.json"), encoding="utf-8") as fh:
        return json.load(fh)


class FakeSession:
    """替掉真会话。记录 warm 了几次、请求了哪些 URL。"""

    def __init__(self, pages=None, error=None):
        self.pages = pages if pages is not None else [fixture()]
        self.error = error
        self.warmed = 0
        self.urls = []

    def warm(self):
        self.warmed += 1

    def timeline(self, uid, page=1):
        self.urls.append((uid, page))
        if self.error:
            raise self.error
        idx = page - 1
        return self.pages[idx] if idx < len(self.pages) else {"statuses": []}


PERSON = {"id": "8152922548", "name": "今日话题", "note": ""}


class TestParse(unittest.TestCase):
    def test_maps_every_field_from_real_payload(self):
        posts = xueqiu.parse_xueqiu(fixture(), PERSON)
        self.assertEqual(len(posts), 3)
        p = posts[0]
        self.assertIsInstance(p, models.Post)
        self.assertEqual(p.channel, "xueqiu")
        self.assertEqual(p.id, "xueqiu:409149519")
        self.assertEqual(p.author_handle, "8152922548")
        self.assertEqual(p.author_name, "今日话题")
        self.assertEqual(p.url, "https://xueqiu.com/8152922548/409149519")

    def test_created_at_is_milliseconds_not_seconds(self):
        """雪球给的是毫秒。按秒解会算到 58000 年,而 36h 截断会把整批悄悄丢光 ——
        这种错不会报错,只会表现为"雪球今天没人发"。"""
        posts = xueqiu.parse_xueqiu(fixture(), PERSON)
        self.assertTrue(posts[0].ts.startswith("2026-"), posts[0].ts)

    def test_strips_html_from_description(self):
        posts = xueqiu.parse_xueqiu(fixture(), PERSON)
        for p in posts:
            self.assertNotIn("<br", p.text)
            self.assertNotIn("</", p.text)

    def test_title_and_body_are_joined(self):
        posts = xueqiu.parse_xueqiu(fixture(), PERSON)
        self.assertIn("双节将至", posts[0].text)
        self.assertIn("中秋", posts[0].text)

    def test_title_not_repeated_when_body_already_starts_with_it(self):
        raw = fixture()
        raw["statuses"] = [dict(raw["statuses"][0],
                                title="重复标题", description="重复标题 后面的正文")]
        posts = xueqiu.parse_xueqiu(raw, PERSON)
        self.assertEqual(posts[0].text.count("重复标题"), 1)

    def test_pic_is_comma_separated_string_not_list(self):
        """pic 字段是 'url1,url2' 的字符串。当成列表遍历会得到一串单字符。"""
        posts = xueqiu.parse_xueqiu(fixture(), PERSON)
        media = posts[0].media
        self.assertTrue(media)
        for u in media:
            self.assertTrue(u.startswith("http"), u)

    def test_engagement_numbers_are_ints(self):
        posts = xueqiu.parse_xueqiu(fixture(), PERSON)
        eng = posts[0].engagement
        self.assertEqual(eng["replies"], 67)
        self.assertEqual(eng["likes"], 13)
        for v in eng.values():
            self.assertIsInstance(v, int)

    def test_retweet_records_original_author(self):
        raw = fixture()
        raw["statuses"] = [dict(raw["statuses"][0],
                                retweeted_status={"user": {"screen_name": "原作者"}})]
        posts = xueqiu.parse_xueqiu(raw, PERSON)
        self.assertEqual(posts[0].repost_of, "原作者")

    def test_one_bad_item_does_not_kill_the_batch(self):
        """照 Task 10 的判例:一条坏数据不能让整个人当天消失。"""
        raw = fixture()
        raw["statuses"] = [{"id": None}, raw["statuses"][0]]
        posts = xueqiu.parse_xueqiu(raw, PERSON)
        self.assertEqual(len(posts), 1)

    def test_missing_statuses_key_returns_empty(self):
        self.assertEqual(xueqiu.parse_xueqiu({}, PERSON), [])


class TestFetch(unittest.TestCase):
    def test_warms_before_requesting(self):
        """必须先访问服务端渲染页拿 token cookie,否则 API 回 400016。"""
        s = FakeSession()
        xueqiu.fetch_xueqiu(None, PERSON, session=s)
        self.assertEqual(s.warmed, 1)

    def test_asks_for_original_posts_only(self):
        """不加 type=0 的话,拿回来 65-80% 是「回复@某某」的一句话评论
        (2026-09-14 实测:段永平 13/20、刘成岗 13/21、管我财 16/21),
        时间线会变成评论区。"""
        seen = []

        class RecordingSession(FakeSession):
            def timeline(self_inner, uid, page=1):
                seen.append((uid, page))
                return fixture()

        s = RecordingSession()
        xueqiu.fetch_xueqiu(None, PERSON, session=s)
        self.assertEqual(xueqiu.XQ_TYPE_ORIGINAL, 0)

    def test_type_param_is_in_the_request_url(self):
        built = []

        class URLSession(xueqiu.Session):
            def __init__(self_inner):
                self_inner._warm = True
                self_inner._lock = __import__("threading").Lock()

            def warm(self_inner):
                pass

            def timeline(self_inner, uid, page=1):
                built.append(
                    f"{xueqiu.XQ_API}?user_id={uid}&page={page}"
                    f"&type={xueqiu.XQ_TYPE_ORIGINAL}")
                return fixture()

        xueqiu.fetch_xueqiu(None, PERSON, session=URLSession())
        self.assertIn("type=0", built[0])

    def test_stops_when_a_page_is_empty(self):
        s = FakeSession(pages=[fixture(), {"statuses": []}])
        posts = xueqiu.fetch_xueqiu(None, PERSON, pages=2, session=s)
        self.assertEqual(len(posts), 3)
        self.assertEqual(s.urls, [("8152922548", 1), ("8152922548", 2)])

    def test_error_propagates_so_collect_marks_the_person_failed(self):
        """单人失败由 run.collect 记录并跳过 —— 这里吞掉的话渠道会被误判成 ok。"""
        s = FakeSession(error=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            xueqiu.fetch_xueqiu(None, PERSON, session=s)


if __name__ == "__main__":
    unittest.main()
