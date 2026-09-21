"""LinkedIn 适配器测试。fixture 是 2026-09-21 真实调用 chamath 的返回抽样,
不是手写的 —— 手写 fixture 只能验证我对字段的假设,验不出假设本身错没错。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import linkedin  # noqa: E402
import run  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PERSON = {"id": "chamath", "name": "Chamath Palihapitiya", "slot": 0}


@pytest.fixture
def payload():
    with open(os.path.join(HERE, "fixtures/linkedin_chamath_posts.json"),
              encoding="utf-8") as fh:
        return json.load(fh)["data"]


def test_只留本人发的和本人转发的(payload):
    posts = linkedin.parse_linkedin(payload, PERSON)
    assert len(posts) == len(payload["items"])   # 样本里没有污染条目
    assert all(p.channel == "linkedin" for p in posts)
    assert all(p.author_handle == "chamath" for p in posts)


def test_转发带上_repost_of_原创不带(payload):
    posts = linkedin.parse_linkedin(payload, PERSON)
    by_id = {p.id: p for p in posts}
    for item in payload["items"]:
        p = by_id[f"linkedin:{item['post_id']}"]
        if item["is_reshared"]:
            assert p.repost_of, f"{item['post_id']} 是转发但没标 repost_of"
            assert p.repost_of != "chamath"
        else:
            assert p.repost_of is None


def test_keep_reshares_关掉就只剩原创(payload):
    posts = linkedin.parse_linkedin(payload, PERSON, keep_reshares=False)
    assert posts and all(p.repost_of is None for p in posts)
    assert len(posts) < len(payload["items"])


def test_污染条目被丢掉(payload):
    """既不是本人发的、也不是本人转发的 —— 文档说这类会混进来,丢掉。"""
    dirty = dict(payload)
    dirty["items"] = payload["items"] + [{
        "post_id": "999", "text": "别人的帖,他只是评论过",
        "url": "https://www.linkedin.com/feed/update/urn:li:activity:999/",
        "published_at": 1789742981, "like_count": 1,
        "author_url": "https://www.linkedin.com/in/someone-else",
        "is_reshared": False,
    }]
    posts = linkedin.parse_linkedin(dirty, PERSON)
    assert "linkedin:999" not in {p.id for p in posts}


def test_时间戳按秒解不是毫秒(payload):
    """published_at 是 unix 秒。当毫秒解会算到公元五万年,36h 截断把整批
    悄悄丢光 —— 不报错,只表现为「LinkedIn 今天没人发」。"""
    posts = linkedin.parse_linkedin(payload, PERSON)
    assert all(p.ts.startswith("202") for p in posts), [p.ts for p in posts]


def test_author_url_归一化域名和大小写():
    """实测 profile 接口返回过 https://ua.linkedin.com/in/Deykhan ——
    国家前缀域名 + 大小写都跟名单里的 id 不一样。"""
    assert linkedin._slug("https://ua.linkedin.com/in/Deykhan") == "deykhan"
    assert linkedin._slug("https://www.linkedin.com/in/chamath/") == "chamath"
    assert linkedin._slug("https://www.linkedin.com/company/8090solutions") == "8090solutions"
    assert linkedin._slug(None) == ""


def test_显示名取名单不取接口(payload):
    """author_name 实测 50/50 全是 null,只能用 sources.yaml 里的 name。"""
    assert all(i["author_name"] is None for i in payload["items"])
    posts = linkedin.parse_linkedin(payload, PERSON)
    assert all(p.author_name == "Chamath Palihapitiya" for p in posts)


def test_坏条目跳过不拖垮整个人(payload):
    dirty = dict(payload)
    dirty["items"] = [{"post_id": "bad", "author_url":
                       "https://www.linkedin.com/in/chamath"}] + payload["items"]
    posts = linkedin.parse_linkedin(dirty, PERSON)
    assert len(posts) == len(payload["items"])


# ── 轮询 ────────────────────────────────────────────────────────────

SOURCES = {
    "linkedin": [{"id": f"p{i}", "slot": i % 4} for i in range(12)],
    "twitter": [{"id": "paulg"}, {"id": "karpathy"}],
}


def test_轮询每天只出一组():
    for day in range(4):
        got = run.rotate_slots(SOURCES, day_index=day)
        assert len(got["linkedin"]) == 3, f"day {day}"
        assert {p["slot"] for p in got["linkedin"]} == {day}


def test_轮询四天覆盖全员():
    seen = set()
    for day in range(4):
        seen |= {p["id"] for p in run.rotate_slots(SOURCES, day_index=day)["linkedin"]}
    assert seen == {p["id"] for p in SOURCES["linkedin"]}


def test_没有_slot_的渠道不受影响():
    for day in range(4):
        assert run.rotate_slots(SOURCES, day_index=day)["twitter"] == SOURCES["twitter"]


def test_轮询不改原名单():
    """authors(侧栏名单)按全量 sources 建,轮询只能返回副本。"""
    before = json.dumps(SOURCES, sort_keys=True)
    run.rotate_slots(SOURCES, day_index=2)
    assert json.dumps(SOURCES, sort_keys=True) == before
