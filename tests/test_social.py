"""Twitter / 小红书 / 公众号的 Asklear 适配器测试。fixture 是 2026-09-26
各真实调用一次的完整返回,不是手写的。"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import run  # noqa: E402
import social  # noqa: E402


def _fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as fh:
        return json.load(fh)


class FakeAsklear:
    """按顺序吐出预设的 job 返回,并记下每次调用的参数。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def collect(self, task_code, payload, key, max_credits=None, sleep=None):
        self.calls.append({"task_code": task_code, "payload": payload,
                           "key": key, "max_credits": max_credits})
        return self.responses.pop(0)


TW = _fixture("asklear_twitter_user_posts.json")
XHS = _fixture("asklear_xhs_user_notes.json")
WX = _fixture("asklear_wechat_account_articles.json")
PAULG = {"id": "paulg", "name": "Paul Graham"}


def test_twitter_parses_real_sample():
    posts = social.parse_twitter(TW["result"]["data"], PAULG)
    assert len(posts) == len(TW["result"]["data"]["items"])
    p = posts[0]
    assert p.id.startswith("twitter:") and p.channel == "twitter"
    assert p.url.startswith("https://x.com/paulg/status/")
    assert p.author_name == "Paul Graham"          # 上游 author_name 全是 null
    assert p.ts.startswith("2026-")                # unix 秒,不是毫秒
    assert p.engagement["likes"] > 0


def test_twitter_marks_retweets():
    posts = social.parse_twitter(TW["result"]["data"], PAULG)
    rts = [p for p in posts if p.text.startswith("RT @")]
    assert rts and all(p.repost_of for p in rts)


def test_twitter_second_page_only_when_first_page_still_in_window():
    data = TW["result"]["data"]
    newest = max(i["published_at"] for i in data["items"])
    oldest = min(i["published_at"] for i in data["items"])
    page2 = {"result": {"data": {"items": [], "has_more": False}}}

    # 第一页最旧一条仍在窗口内 → 翻第二页
    api = FakeAsklear(TW, page2)
    social.fetch_twitter(None, PAULG, asklear=api, now=oldest + 3600)
    assert len(api.calls) == 2
    assert api.calls[1]["payload"]["cursor"] == data["next_cursor"]

    # 第一页已经翻出窗口 → 不翻
    api = FakeAsklear(TW)
    social.fetch_twitter(None, PAULG, asklear=api, now=newest + 10 * 86400)
    assert len(api.calls) == 1
    assert api.calls[0]["max_credits"] == 2


def test_xhs_parses_real_sample():
    person = {"id": "6065978600000000010076c6", "name": "大卫翁"}
    posts = social.parse_xhs(XHS["result"]["data"], person)
    assert len(posts) == len(XHS["result"]["data"]["items"])
    p = posts[0]
    assert p.id == "xhs:" + XHS["result"]["data"]["items"][0]["note_id"]
    assert p.author_name == "大卫翁"
    assert p.url.startswith("https://www.xiaohongshu.com/search_result?keyword=")
    assert p.media and p.media[0].startswith("https://")
    assert p.ts.startswith("20")


def test_wechat_parses_real_sample():
    person = {"id": "gh_3e5d3b151ac4", "name": "42章经"}
    items = WX["result"]["data"]["items"]
    posts = social.parse_wechat(WX["result"]["data"], person)
    assert len(posts) == len(items)
    assert len({p.id for p in posts}) == len(posts)   # 头条/次条靠 idx 区分,不能撞 id
    assert posts[0].url.startswith("http://mp.weixin.qq.com/s?")
    assert posts[0].text.startswith(items[0]["title"])


def test_one_bad_item_does_not_drop_the_rest():
    data = {"items": [{"tweet_id": "1"}] + TW["result"]["data"]["items"][:2]}
    posts = social.parse_twitter(data, PAULG)
    assert len(posts) == 2                            # 缺 published_at 的那条被跳过


def test_run_wires_three_channels_to_social():
    assert run.DEFAULT_FETCHERS["twitter"] is social.fetch_twitter
    assert run.DEFAULT_FETCHERS["xhs"] is social.fetch_xhs
    assert run.DEFAULT_FETCHERS["wechat"] is social.fetch_wechat
