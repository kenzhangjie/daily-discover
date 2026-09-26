"""Twitter / 小红书 / 公众号适配器,走 Asklear 异步采集。

2026-09-26 从 TikHub 切过来:TikHub 余额耗尽,三个渠道全线
`HTTP 402 Payment Required`。Asklear 的客户端(估价 → 启动 → 轮询)
复用 linkedin.py 里那一个,这里只管三个渠道各自的输入和解析。

## 单价(固定积分/次)

    twitter_user_posts_v1          2
    xhs_user_notes_v1              8
    wechat_mp_account_articles_v1  3

按 2026-09 的名单(30 / 5 / 11 人)约 133 积分/天,不含 LinkedIn。

## 字段是照实跑的样本写的,不是照文档

样本在 tests/fixtures/asklear_*.json(2026-09-26 各拉一次)。跟 TikHub 比:

- 时间戳三个渠道都是 unix **秒**。
- Twitter **没有媒体字段**,配图这一项从 TikHub 切过来就丢了;author_name /
  author_handle 全是 null,显示名只能取 sources.yaml。转发仍以 `RT @x:` 开头。
- 小红书同样没有 xsec_token,note_id 拼不出能打开的链接,继续跳站内搜索
  (TikHub 时期就是这么处理的,见 tikhub.parse_xhs)。
- 公众号 cover 在样本里全是 null;一次推送的头条 / 次条已经展平成多条,
  用 idx 区分。

## 翻页

小红书、公众号一次一页(约 20 / 15 条),对 36 小时窗口绰绰有余。
Twitter 一页 20 条,高频账号(elonmusk)可能不够:只在第一页最旧一条仍落在
窗口内时才翻第二页,多花 2 积分。
"""
import html
import re
import sys
import time
import urllib.parse

import linkedin
import models

CUTOFF_HOURS = 36       # 跟 run.CUTOFF_HOURS 一致;翻页判断用,不做截断
TWITTER_MAX_PAGES = 2
RT_RE = re.compile(r"^RT @([A-Za-z0-9_]{1,15}):")

CREDITS = {
    "twitter_user_posts_v1": 2,
    "xhs_user_notes_v1": 8,
    "wechat_mp_account_articles_v1": 3,
}


def _warn(msg):
    print(f"WARN {msg}", file=sys.stderr)


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _collect(api, task_code, payload, key, sleep):
    got = api.collect(task_code, payload, key, max_credits=CREDITS[task_code], sleep=sleep)
    return (got.get("result") or {}).get("data") or {}


def _key(channel, person, page=1):
    # 幂等键带日期:同一天重跑复用同一个 job,不重复扣费
    day = time.strftime("%Y%m%d", time.gmtime())
    return f"dd-{channel}-{person['id']}-{day}-p{page}"


# ── Twitter ────────────────────────────────────────────────────────────

def parse_twitter(data, person):
    out = []
    for t in data.get("items") or []:
        tid = str(t.get("tweet_id") or "")
        try:
            if not tid:
                raise ValueError("缺 tweet_id")
            text = html.unescape(t.get("text") or "")
            m = RT_RE.match(text)
            out.append(models.Post(
                id=f"twitter:{tid}",
                channel="twitter",
                author_handle=person["id"],
                author_name=person.get("name") or person["id"],
                text=text,
                ts=models.to_utc_iso(int(t["published_at"])),
                url=f"https://x.com/{person['id']}/status/{tid}",
                engagement={"likes": _int(t.get("like_count")),
                            "views": _int(t.get("view_count")),
                            "replies": _int(t.get("reply_count"))},
                media=[],
                repost_of=m.group(1) if m else None,
            ))
        except Exception as exc:  # noqa: BLE001
            _warn(f"twitter {person['id']} 跳过一条(tweet_id={tid}): {exc}")
    return out


def fetch_twitter(client, person, asklear=None, sleep=time.sleep, now=None):
    """client 是给 TikHub 时代留的统一签名,这里用不到。"""
    api = asklear or linkedin._default_client()
    cutoff = (now or time.time()) - CUTOFF_HOURS * 3600
    posts, cursor = [], ""
    for page in range(1, TWITTER_MAX_PAGES + 1):
        payload = {"screen_name": person["id"]}
        if cursor:
            payload["cursor"] = cursor
        data = _collect(api, "twitter_user_posts_v1", payload, _key("twitter", person, page), sleep)
        posts.extend(parse_twitter(data, person))
        items = data.get("items") or []
        oldest = min((_int(i.get("published_at")) for i in items), default=0)
        cursor = data.get("next_cursor") or ""
        if not (data.get("has_more") and cursor and oldest >= cutoff):
            break
    return posts


# ── 小红书 ─────────────────────────────────────────────────────────────

def parse_xhs(data, person):
    out = []
    for idx, n in enumerate(data.get("items") or []):
        note_id = n.get("note_id") or f"index:{idx}"
        try:
            title = html.unescape(n.get("title") or "").strip()
            desc = html.unescape(n.get("description") or "").strip()
            if not desc:
                text = title
            elif desc.startswith(title):
                text = desc
            else:
                text = f"{title}\n{desc}"
            url = "https://www.xiaohongshu.com/search_result?keyword=" + urllib.parse.quote(
                title or person["id"])
            out.append(models.Post(
                id=f"xhs:{n.get('note_id')}",
                channel="xhs",
                author_handle=person["id"],
                author_name=(n.get("author") or {}).get("nickname") or person.get("name") or person["id"],
                text=text,
                ts=models.to_utc_iso(int(n["publish_time"])),
                url=url,
                engagement={"likes": _int(n.get("liked_count")),
                            "comments": _int(n.get("comments_count")),
                            "collected": _int(n.get("collected_count")),
                            "views": _int(n.get("view_count"))},
                media=[i["url"] for i in (n.get("images") or []) if isinstance(i, dict) and i.get("url")],
                repost_of=None,
            ))
        except Exception as exc:  # noqa: BLE001
            _warn(f"xhs {person['id']} 跳过一条(id={note_id}): {exc}")
    return out


def fetch_xhs(client, person, asklear=None, sleep=time.sleep):
    api = asklear or linkedin._default_client()
    data = _collect(api, "xhs_user_notes_v1", {"user_id": person["id"]}, _key("xhs", person), sleep)
    return parse_xhs(data, person)


# ── 公众号 ─────────────────────────────────────────────────────────────

def parse_wechat(data, person):
    out = []
    for a in data.get("items") or []:
        mid = a.get("app_msg_id")
        idx = a.get("idx")
        try:
            if mid in (None, ""):
                raise ValueError("缺 app_msg_id")
            title = html.unescape(a.get("title") or "")
            digest = html.unescape(a.get("digest") or "")
            out.append(models.Post(
                id=f"wechat:{mid}:{idx}",
                channel="wechat",
                author_handle=person["id"],
                author_name=person.get("name") or person["id"],
                text=f"{title}\n{digest}".strip() if digest else title,
                ts=models.to_utc_iso(int(a["create_time"])),
                url=a.get("url") or "",
                engagement={},
                media=[a["cover"]] if a.get("cover") else [],
                repost_of=None,
            ))
        except Exception as exc:  # noqa: BLE001
            _warn(f"wechat {person['id']} 跳过一条(app_msg_id={mid}, idx={idx}): {exc}")
    return out


def fetch_wechat(client, person, asklear=None, sleep=time.sleep):
    api = asklear or linkedin._default_client()
    data = _collect(api, "wechat_mp_account_articles_v1", {"username": person["id"]},
                    _key("wechat", person), sleep)
    return parse_wechat(data, person)
