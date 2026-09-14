import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dedup, models


def P(pid, text, ts, handle):
    return models.Post(id=pid, channel="twitter", author_handle=handle,
                       author_name=handle, text=text, ts=ts,
                       url="http://x", engagement={}, media=[], repost_of=None)


class TestDedup(unittest.TestCase):
    def test_keeps_earliest_of_duplicates(self):
        a = P("twitter:1", "同一条内容", "2026-09-12T10:00:00Z", "alice")
        b = P("twitter:2", "同一条内容", "2026-09-12T12:00:00Z", "bob")
        got = dedup.dedup([b, a])
        self.assertEqual([p.id for p in got], ["twitter:1"])

    def test_normalizes_whitespace_and_rt_prefix(self):
        a = P("twitter:1", "hello world", "2026-09-12T10:00:00Z", "alice")
        b = P("twitter:2", "RT @alice: hello   world", "2026-09-12T11:00:00Z", "bob")
        self.assertEqual(len(dedup.dedup([a, b])), 1)

    def test_different_text_kept(self):
        a = P("twitter:1", "aaa", "2026-09-12T10:00:00Z", "alice")
        b = P("twitter:2", "bbb", "2026-09-12T11:00:00Z", "bob")
        self.assertEqual(len(dedup.dedup([a, b])), 2)

    def test_empty_text_never_merged(self):
        a = P("xhs:1", "", "2026-09-12T10:00:00Z", "alice")
        b = P("xhs:2", "", "2026-09-12T11:00:00Z", "bob")
        self.assertEqual(len(dedup.dedup([a, b])), 2)


if __name__ == "__main__":
    unittest.main()
