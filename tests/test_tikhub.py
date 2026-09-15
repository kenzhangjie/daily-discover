import concurrent.futures
import sys, os, threading, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tikhub


class FakeOpener:
    """替掉 urlopen,记录调用并按脚本返回。"""
    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def __call__(self, req, timeout=None):
        self.seen.append(req.full_url)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResp(item)


class _FakeResp:
    def __init__(self, payload):
        import json
        self._b = json.dumps(payload).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


class TestBudget(unittest.TestCase):
    def test_counts_calls(self):
        c = tikhub.TikHub("k", opener=FakeOpener([{"code": 200, "data": {}}]))
        c.get("/x", {})
        self.assertEqual(c.calls, 1)

    def test_raises_when_over_budget(self):
        opener = FakeOpener([{"code": 200, "data": {}}, {"code": 200, "data": {}}])
        c = tikhub.TikHub("k", max_calls=1, opener=opener)
        c.get("/x", {})
        with self.assertRaises(tikhub.BudgetExceeded):
            c.get("/x", {})
        # 超限必须在真正发请求之前拦下——不能白烧一次调用
        self.assertEqual(len(opener.seen), 1)

    def test_non_200_code_raises(self):
        c = tikhub.TikHub("k", opener=FakeOpener([{"code": 400, "detail": {"message": "bad"}}]))
        with self.assertRaises(tikhub.TikHubError):
            c.get("/x", {})


class ConcurrentFakeOpener:
    """给并发测试用的 opener:list.pop(0) 在 CPython 里本身已是原子的,
    但按要求显式加锁,避免"假 opener 不是线程安全的"这件事伪造出跟
    _spend() 无关的测试失败。"""
    def __init__(self, script):
        self.script = list(script)
        self._lock = threading.Lock()

    def __call__(self, req, timeout=None):
        with self._lock:
            item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResp(item)


class TestSpendConcurrency(unittest.TestCase):
    def test_spend_is_atomic_under_concurrent_calls(self):
        opener = ConcurrentFakeOpener([{"code": 200, "data": {}}] * 8)
        c = tikhub.TikHub("k", max_calls=10, opener=opener)

        def worker(_):
            try:
                c.get("/x", {})
                return "ok"
            except tikhub.BudgetExceeded:
                return "budget"

        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(worker, range(8)))

        self.assertEqual(len(results), 8)
        self.assertEqual(results.count("ok") + results.count("budget"), 8)
        self.assertLessEqual(c.calls, 10)


class RequestCapturingOpener:
    """记录真实发出的 Request 对象(而非仅 URL),用来断言 header。"""
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResp(item)


class TestUserAgent(unittest.TestCase):
    def test_get_sends_nonempty_user_agent(self):
        opener = RequestCapturingOpener([{"code": 200, "data": {}}])
        c = tikhub.TikHub("k", opener=opener)
        c.get("/x", {})
        ua = opener.requests[0].get_header("User-agent")
        self.assertTrue(ua)

    def test_post_sends_nonempty_user_agent(self):
        opener = RequestCapturingOpener([{"code": 200, "data": {}}])
        c = tikhub.TikHub("k", opener=opener)
        c.post("/x", {})
        ua = opener.requests[0].get_header("User-agent")
        self.assertTrue(ua)


import copy
import json
FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class TestParseTwitter(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "twitter_user_posts.json"), encoding="utf-8") as fh:
            self.payload = json.load(fh)

    def test_extracts_posts(self):
        posts = tikhub.parse_twitter(self.payload, {"id": "paulg", "name": "Paul Graham"})
        self.assertEqual(len(posts), 3)
        p = posts[0]
        self.assertEqual(p.channel, "twitter")
        self.assertEqual(p.author_handle, "paulg")
        self.assertTrue(p.id.startswith("twitter:"))
        self.assertTrue(p.ts.endswith("Z"))
        self.assertTrue(p.url.startswith("https://x.com/paulg/status/"))

    def test_html_entities_are_decoded(self):
        """TikHub 的正文是 HTML 转义过的。页面用 textContent 渲染,不还原的话
        读者看到的就是字面的 `-&gt;` —— 2026-09-15 线上 124 条里 8 条这样。"""
        payload = copy.deepcopy(self.payload)
        payload["data"]["timeline"][0]["text"] = "Parking ticket -&gt; @bot &amp; done"
        posts = tikhub.parse_twitter(payload, {"id": "paulg", "name": "Paul Graham"})
        self.assertEqual(posts[0].text, "Parking ticket -> @bot & done")

    def test_marks_retweet(self):
        posts = tikhub.parse_twitter(self.payload, {"id": "paulg", "name": "Paul Graham"})
        rts = [p for p in posts if p.repost_of]
        self.assertTrue(rts, "fixture 里第一条是 RT,应被标记")

    def test_extracts_media_url(self):
        posts = tikhub.parse_twitter(self.payload, {"id": "paulg", "name": "Paul Graham"})
        p = posts[1]
        self.assertEqual(len(p.media), 1)
        self.assertTrue(p.media[0].startswith("https://pbs.twimg.com/"))

    def test_engagement_views_is_int(self):
        posts = tikhub.parse_twitter(self.payload, {"id": "paulg", "name": "Paul Graham"})
        views = posts[1].engagement["views"]
        self.assertIsInstance(views, int)
        self.assertEqual(views, 143114)


class TestParseXhs(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "xhs_user_notes.json"), encoding="utf-8") as fh:
            self.payload = json.load(fh)

    def test_extracts_from_double_nesting(self):
        posts = tikhub.parse_xhs(self.payload, {"id": "61b46d790000000010008153", "name": "林书豪"})
        self.assertEqual(len(posts), 3)
        p = posts[0]
        self.assertEqual(p.channel, "xhs")
        self.assertTrue(p.text, "标题或正文不该为空")
        self.assertTrue(p.ts.endswith("Z"))

    def test_url_is_search_not_fake_note_link(self):
        posts = tikhub.parse_xhs(self.payload, {"id": "61b46d790000000010008153", "name": "林书豪"})
        self.assertIn("search_result", posts[0].url)
        self.assertNotIn("/explore/", posts[0].url)

    def test_engagement_carries_xhs_specific_counts(self):
        posts = tikhub.parse_xhs(self.payload, {"id": "61b46d790000000010008153", "name": "林书豪"})
        self.assertIn("collected", posts[0].engagement)

    def test_desc_starting_with_title_is_not_duplicated(self):
        # 笔记 2("开学了！你们是开心还是舍不得呢？")的 desc 本身就以标题开头，
        # 不能再拼一遍标题——那样正文没丢，但标题会显示两遍。
        posts = tikhub.parse_xhs(self.payload, {"id": "61b46d790000000010008153", "name": "林书豪"})
        p = posts[2]
        title = "开学了！你们是开心还是舍不得呢？"
        self.assertEqual(p.text.count(title), 1, "desc 已含标题时不该再拼一遍")

    def test_desc_not_starting_with_title_keeps_both(self):
        # 笔记 0("是时候说再见了❤️")的 desc 不以标题开头，标题和正文都要保留。
        posts = tikhub.parse_xhs(self.payload, {"id": "61b46d790000000010008153", "name": "林书豪"})
        p = posts[0]
        title = "是时候说再见了❤️"
        self.assertTrue(p.text.startswith(title + "\n"))
        self.assertIn("职业运动员", p.text)


class TestParseWechat(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "wechat_account_articles.json"), encoding="utf-8") as fh:
            self.payload = json.load(fh)

    def test_flattens_detail_info(self):
        posts = tikhub.parse_wechat(self.payload, {"id": "gh_363b924965e9", "name": "人民日报"})
        # fixture 里 3 次推送,每次 detailInfo 至少 1 篇
        self.assertGreaterEqual(len(posts), 3)
        self.assertEqual(posts[0].channel, "wechat")

    def test_url_and_title_present(self):
        posts = tikhub.parse_wechat(self.payload, {"id": "gh_363b924965e9", "name": "人民日报"})
        self.assertTrue(posts[0].text)
        self.assertTrue(posts[0].url.startswith("http"))

    def test_html_entities_are_decoded(self):
        posts = tikhub.parse_wechat(self.payload, {"id": "gh_x", "name": "某号"})
        payload = copy.deepcopy(self.payload)
        payload["data"]["articles"][0]["appMsg"]["detailInfo"][0]["title"] = "A &amp; B"
        posts = tikhub.parse_wechat(payload, {"id": "gh_x", "name": "某号"})
        self.assertTrue(posts[0].text.startswith("A & B"))

    def test_ids_unique_within_one_push(self):
        posts = tikhub.parse_wechat(self.payload, {"id": "gh_363b924965e9", "name": "人民日报"})
        self.assertEqual(len(posts), len({p.id for p in posts}))

    def test_flattens_multiple_detail_info_in_one_push(self):
        # fixture 里 3 次推送恰好各只有 1 篇,测不出"一次推送多篇"的展平行为。
        # 基于真实 fixture 构造:取第一次推送,把它的 detailInfo 复制成两项,
        # 只有"多重性"是构造的,其余字段(msgId/createTime/coverImgUrl 等)保持真实形态。
        import copy
        payload = copy.deepcopy(self.payload)
        art = payload["data"]["articles"][0]
        head = art["appMsg"]["detailInfo"][0]
        second = copy.deepcopy(head)
        second["title"] = head["title"] + "-次条"
        second["contentUrl"] = head["contentUrl"] + "&idx=2"
        art["appMsg"]["detailInfo"] = [head, second]
        payload["data"]["articles"] = [art]

        posts = tikhub.parse_wechat(payload, {"id": "gh_363b924965e9", "name": "人民日报"})

        self.assertEqual(len(posts), 2, "一次推送含两篇 detailInfo,必须展平成 2 条 Post")
        msg_id = art["baseInfo"]["msgId"]
        self.assertEqual(posts[0].id, f"wechat:{msg_id}:0")
        self.assertEqual(posts[1].id, f"wechat:{msg_id}:1")
        self.assertNotEqual(posts[0].id, posts[1].id)
        self.assertNotIn("-次条", posts[0].text)
        self.assertIn("-次条", posts[1].text)


class TestParseTwitterBadTimestamp(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "twitter_user_posts.json"), encoding="utf-8") as fh:
            self.payload = json.load(fh)

    def test_null_created_at_is_skipped_not_fatal(self):
        # 真实故障:29 个 handle 里有 1 个 20 条推文里有 1 条 created_at 是 None,
        # 之前会让整批(20 条)因这一条抛错而全部丢失。
        payload = copy.deepcopy(self.payload)
        timeline = payload["data"]["timeline"]
        bad_id = timeline[0]["tweet_id"]
        good_ids = {f"twitter:{t['tweet_id']}" for t in timeline[1:]}
        timeline[0]["created_at"] = None

        posts = tikhub.parse_twitter(payload, {"id": "paulg", "name": "Paul Graham"})

        self.assertEqual(len(posts), len(timeline) - 1)
        got_ids = {p.id for p in posts}
        self.assertNotIn(f"twitter:{bad_id}", got_ids)
        self.assertEqual(got_ids, good_ids)


class TestParseXhsBadTimestamp(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "xhs_user_notes.json"), encoding="utf-8") as fh:
            self.payload = json.load(fh)

    def test_null_create_time_is_skipped_not_fatal(self):
        payload = copy.deepcopy(self.payload)
        notes = payload["data"]["data"]["notes"]
        bad_id = notes[1]["id"]
        good_ids = {f"xhs:{n['id']}" for i, n in enumerate(notes) if i != 1}
        notes[1]["create_time"] = None

        posts = tikhub.parse_xhs(payload, {"id": "61b46d790000000010008153", "name": "林书豪"})

        self.assertEqual(len(posts), len(notes) - 1)
        got_ids = {p.id for p in posts}
        self.assertNotIn(f"xhs:{bad_id}", got_ids)
        self.assertEqual(got_ids, good_ids)


class TestParseWechatBadTimestamp(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "wechat_account_articles.json"), encoding="utf-8") as fh:
            self.payload = json.load(fh)

    def test_null_create_time_is_skipped_not_fatal(self):
        payload = copy.deepcopy(self.payload)
        arts = payload["data"]["articles"]
        bad = arts[0]
        bad["appMsg"]["baseInfo"]["createTime"] = None
        bad["baseInfo"]["dateTime"] = None
        # fixture 里每次推送恰好 1 篇 detailInfo,坏的那次推送整体消失
        good_msg_ids = {a["baseInfo"]["msgId"] for a in arts[1:]}

        posts = tikhub.parse_wechat(payload, {"id": "gh_363b924965e9", "name": "人民日报"})

        self.assertEqual(len(posts), len(arts) - 1)
        got_msg_ids = {p.id.split(":")[1] for p in posts}
        self.assertEqual(got_msg_ids, {str(m) for m in good_msg_ids})


if __name__ == "__main__":
    unittest.main()
