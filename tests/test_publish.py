import sys, os, unittest
import urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import publish, models


def P(pid, ts, channel="twitter"):
    return models.Post(id=pid, channel=channel, author_handle="a", author_name="A",
                       text="t", ts=ts, url="u", engagement={}, media=[], repost_of=None)


class TestShard(unittest.TestCase):
    def test_shape(self):
        s = publish.build_shard(
            "2026-09-13",
            [P("twitter:1", "2026-09-12T10:00:00Z")],
            {"twitter": {"count": 1, "ok": True}},
            {"polymarket": [], "ipo": []},
        )
        self.assertEqual(s["date"], "2026-09-13")
        self.assertEqual(len(s["posts"]), 1)
        self.assertIn("generated_at", s)
        self.assertEqual(s["stats"]["twitter"]["ok"], True)

    def test_posts_sorted_desc(self):
        s = publish.build_shard("2026-09-13",
                                [P("a", "2026-09-12T08:00:00Z"), P("b", "2026-09-12T20:00:00Z")],
                                {}, {"polymarket": [], "ipo": []})
        self.assertEqual([p["id"] for p in s["posts"]], ["b", "a"])

    def test_failed_channel_marked_not_ok(self):
        s = publish.build_shard("2026-09-13", [], {"linkedin": {"count": 0, "ok": False, "error": "400"}},
                                {"polymarket": [], "ipo": []})
        self.assertFalse(s["stats"]["linkedin"]["ok"])


class TestIndex(unittest.TestCase):
    def test_merge_replaces_same_day(self):
        existing = {"days": [{"date": "2026-09-13", "total": 10, "channels": {}}], "authors": []}
        merged = publish.merge_index(existing, {"date": "2026-09-13", "total": 99, "channels": {}})
        self.assertEqual(len(merged["days"]), 1)
        self.assertEqual(merged["days"][0]["total"], 99)

    def test_merge_sorts_newest_first(self):
        existing = {"days": [{"date": "2026-09-11", "total": 1, "channels": {}}], "authors": []}
        merged = publish.merge_index(existing, {"date": "2026-09-13", "total": 2, "channels": {}})
        self.assertEqual([d["date"] for d in merged["days"]], ["2026-09-13", "2026-09-11"])


class TestUpload(unittest.TestCase):
    def test_retries_then_raises(self):
        calls = []
        def flaky(key, body):
            calls.append(key)
            raise OSError("boom")
        with self.assertRaises(publish.UploadFailed):
            publish.upload_r2("discover/x.json", {"a": 1}, put=flaky, sleep=lambda _: None)
        self.assertEqual(len(calls), 3)

    def test_succeeds_on_second_try(self):
        state = {"n": 0}
        def flaky(key, body):
            state["n"] += 1
            if state["n"] == 1:
                raise OSError("boom")
        publish.upload_r2("discover/x.json", {"a": 1}, put=flaky, sleep=lambda _: None)
        self.assertEqual(state["n"], 2)


class TestFetchIndex(unittest.TestCase):
    def test_returns_empty_skeleton_when_404(self):
        def missing(url):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        idx = publish.fetch_index(get=missing)
        self.assertEqual(idx["days"], [])
        self.assertEqual(idx["authors"], [])

    def test_returns_empty_skeleton_when_403(self):
        # R2 public 桶对不存在的 key 也可能回 403,当作"不存在"处理
        def forbidden(url):
            raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)
        idx = publish.fetch_index(get=forbidden)
        self.assertEqual(idx["days"], [])
        self.assertEqual(idx["authors"], [])

    def test_raises_on_http_500(self):
        def server_error(url):
            raise urllib.error.HTTPError(url, 500, "Internal Server Error", None, None)
        with self.assertRaises(publish.IndexFetchFailed):
            publish.fetch_index(get=server_error)

    def test_raises_on_url_error(self):
        def unreachable(url):
            raise urllib.error.URLError("network down")
        with self.assertRaises(publish.IndexFetchFailed):
            publish.fetch_index(get=unreachable)

    def test_raises_on_generic_exception(self):
        def boom(url):
            raise Exception("timeout")
        with self.assertRaises(publish.IndexFetchFailed):
            publish.fetch_index(get=boom)

    def test_returns_parsed_when_present(self):
        import json as _j
        def ok(url):
            return _j.dumps({"days": [{"date": "2026-09-12", "total": 5, "channels": {}}],
                             "authors": [{"channel": "twitter", "handle": "paulg", "name": "PG"}]}).encode()
        idx = publish.fetch_index(get=ok)
        self.assertEqual(len(idx["days"]), 1)
        self.assertEqual(idx["authors"][0]["handle"], "paulg")


if __name__ == "__main__":
    unittest.main()
