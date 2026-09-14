import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import models


class TestToUtcIso(unittest.TestCase):
    def test_unix_int(self):
        # 小红书 create_time 是 unix 秒
        self.assertEqual(models.to_utc_iso(1756598549), "2025-08-31T00:02:29Z")

    def test_twitter_string(self):
        # Twitter created_at 形如 "Sat Sep 12 20:12:03 +0000 2026"
        self.assertEqual(
            models.to_utc_iso("Sat Sep 12 20:12:03 +0000 2026"),
            "2026-09-12T20:12:03Z",
        )

    def test_rejects_unknown(self):
        with self.assertRaises(ValueError):
            models.to_utc_iso("昨天")


class TestPost(unittest.TestCase):
    def test_to_dict_shape(self):
        p = models.Post(
            id="twitter:1", channel="twitter",
            author_handle="paulg", author_name="Paul Graham",
            text="hi", ts="2026-09-12T11:00:08Z",
            url="https://x.com/paulg/status/1",
            engagement={"likes": 3}, media=[], repost_of=None,
        )
        d = p.to_dict()
        self.assertEqual(d["author"], {"handle": "paulg", "name": "Paul Graham"})
        self.assertEqual(d["id"], "twitter:1")
        self.assertNotIn("author_handle", d)


class TestLoadSources(unittest.TestCase):
    def test_loads_and_validates(self):
        import json, tempfile
        data = {"twitter": [{"id": "paulg", "name": "Paul Graham", "note": "YC"}],
                "xhs": [{"id": "61b46d790000000010008153", "name": "林书豪"}],
                "wechat": [{"id": "gh_363b924965e9", "name": "人民日报"}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name
        got = models.load_sources(path)
        self.assertEqual(len(got["twitter"]), 1)
        self.assertEqual(got["twitter"][0]["id"], "paulg")

    def test_rejects_unknown_channel(self):
        import json, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"douyin": [{"id": "x", "name": "y"}]}, f)
            path = f.name
        with self.assertRaises(ValueError):
            models.load_sources(path)

    def test_rejects_entry_without_id(self):
        import json, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"twitter": [{"name": "no id"}]}, f)
            path = f.name
        with self.assertRaises(ValueError):
            models.load_sources(path)


if __name__ == "__main__":
    unittest.main()
