"""TikHub 客户端。成本闸在这里收口 —— 调用计数超限立刻抛错,
不靠调用方自觉。"""
import json
import sys
import threading
import time
import urllib.parse
import urllib.request

BASE = "https://api.tikhub.io"
USER_AGENT = "daily-discover/1.0"


class TikHubError(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class TikHub:
    def __init__(self, api_key, max_calls=220, timeout=40, retries=3, opener=None):
        if not api_key:
            raise ValueError("TIKHUB_API_KEY 未设置")
        self.api_key = api_key
        self.max_calls = max_calls
        self.timeout = timeout
        self.retries = retries
        self.calls = 0
        self._opener = opener or urllib.request.urlopen
        self._lock = threading.Lock()

    def _spend(self):
        with self._lock:
            if self.calls >= self.max_calls:
                raise BudgetExceeded(f"已达单次运行调用上限 {self.max_calls}")
            self.calls += 1

    def _request(self, req):
        last = None
        for attempt in range(self.retries):
            try:
                with self._opener(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as exc:            # 网络层失败才重试
                last = exc
                if attempt == self.retries - 1:
                    raise TikHubError(f"请求失败: {exc}") from exc
                time.sleep(2 ** attempt)
        if payload.get("code") != 200:
            detail = payload.get("detail") or {}
            msg = detail.get("message") or payload.get("message") or payload
            raise TikHubError(f"TikHub code={payload.get('code')}: {str(msg)[:160]}")
        return payload

    def get(self, path, params):
        self._spend()
        url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.api_key}",
                                                     "User-Agent": USER_AGENT})
        return self._request(req)

    def post(self, path, body):
        self._spend()
        req = urllib.request.Request(
            f"{BASE}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "User-Agent": USER_AGENT},
            method="POST",
        )
        return self._request(req)

    def balance(self):
        """返回账户余额(美元)。用于低余额告警。"""
        payload = self.get("/api/v1/tikhub/user/get_user_info", {})
        for src in (payload.get("user_data"), payload.get("data"), payload):
            if isinstance(src, dict) and "balance" in src:
                return float(src["balance"])
        return None


import re
import models

RT_RE = re.compile(r"^RT @([A-Za-z0-9_]{1,15}):")


def warn(msg):
    """单条解析失败时留痕,不静默吞掉。"""
    print(f"WARN {msg}", file=sys.stderr)


def _extract_media(media):
    """media 字段有两种形态:空列表(无媒体),或 dict(按类型分组,
    键可能是 photo / video / animated_gif 等,不写死枚举)。
    每个分组下是一串媒体 dict,URL 在 media_url_https 里。"""
    if not isinstance(media, dict):
        return []
    out = []
    for items in media.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("media_url_https"):
                out.append(item["media_url_https"])
    return out


def _as_int(value):
    """engagement 数值统一转 int:likes/replies 本来就是 int,views 是字符串。
    缺失(None/空串)归 0,不能让一条推文的脏数据炸掉整批。"""
    if value is None or value == "":
        return 0
    return int(value)


def parse_twitter(payload, person):
    out = []
    for t in (payload.get("data") or {}).get("timeline") or []:
        tid = str(t.get("tweet_id"))
        try:
            text = t.get("text") or ""
            m = RT_RE.match(text)
            out.append(models.Post(
                id=f"twitter:{tid}",
                channel="twitter",
                author_handle=person["id"],
                author_name=person.get("name") or person["id"],
                text=text,
                ts=models.to_utc_iso(t.get("created_at")),
                url=f"https://x.com/{person['id']}/status/{tid}",
                engagement={"likes": _as_int(t.get("favorites")),
                            "views": _as_int(t.get("views")),
                            "replies": _as_int(t.get("replies"))},
                media=_extract_media(t.get("media")),
                repost_of=m.group(1) if m else None,
            ))
        except Exception as exc:
            warn(f"twitter {person['id']} 跳过一条(tweet_id={tid}): {exc}")
    return out


def fetch_twitter(client, person, pages=2):
    posts, cursor = [], None
    for _ in range(pages):
        params = {"screen_name": person["id"]}
        if cursor:
            params["cursor"] = cursor
        payload = client.get("/api/v1/twitter/web/fetch_user_post_tweet", params)
        posts.extend(parse_twitter(payload, person))
        cursor = (payload.get("data") or {}).get("next_cursor")
        if not cursor:
            break
    return posts


import urllib.parse as _up


def parse_xhs(payload, person):
    notes = ((payload.get("data") or {}).get("data") or {}).get("notes") or []
    out = []
    for idx, n in enumerate(notes):
        note_id = n.get("id") or f"index:{idx}"
        try:
            title = (n.get("display_title") or n.get("title") or "").strip()
            desc = (n.get("desc") or "").strip()
            if not desc:
                text = title
            elif desc.startswith(title):
                text = desc
            else:
                text = f"{title}\n{desc}"
            # note_id 拼不出可访问链接(缺 xsec_token),退而求其次跳站内搜索
            url = "https://www.xiaohongshu.com/search_result?keyword=" + _up.quote(title or person["id"])
            out.append(models.Post(
                id=f"xhs:{n.get('id')}",
                channel="xhs",
                author_handle=person["id"],
                author_name=(n.get("user") or {}).get("nickname") or person.get("name") or person["id"],
                text=text,
                ts=models.to_utc_iso(n.get("create_time")),
                url=url,
                engagement={"likes": n.get("likes") or 0,
                            "comments": n.get("comments_count") or 0,
                            "collected": n.get("collected_count") or 0,
                            "views": n.get("view_count") or 0},
                media=[i.get("url") for i in (n.get("images_list") or []) if isinstance(i, dict) and i.get("url")],
                repost_of=None,
            ))
        except Exception as exc:
            warn(f"xhs {person['id']} 跳过一条(id={note_id}): {exc}")
    return out


def fetch_xhs(client, person, pages=2):
    posts, cursor = [], None
    for _ in range(pages):
        params = {"user_id": person["id"]}
        if cursor:
            params["cursor"] = cursor
        payload = client.get("/api/v1/xiaohongshu/app_v2/get_user_posted_notes", params)
        batch = parse_xhs(payload, person)
        posts.extend(batch)
        notes = ((payload.get("data") or {}).get("data") or {}).get("notes") or []
        cursor = notes[-1].get("cursor") if notes else None
        if not cursor:
            break
    return posts


def parse_wechat(payload, person):
    out = []
    for art in (payload.get("data") or {}).get("articles") or []:
        app = art.get("appMsg") or {}
        base = app.get("baseInfo") or {}
        msg_id = (art.get("baseInfo") or {}).get("msgId") or base.get("appMsgId")
        created = base.get("createTime") or (art.get("baseInfo") or {}).get("dateTime")
        details = app.get("detailInfo") or []
        # 一次推送可含头条 + 次条,展平成多条
        for idx, d in enumerate(details):
            try:
                title = d.get("title") or ""
                digest = d.get("digest") or ""
                out.append(models.Post(
                    id=f"wechat:{msg_id}:{idx}",
                    channel="wechat",
                    author_handle=person["id"],
                    author_name=person.get("name") or person["id"],
                    text=f"{title}\n{digest}".strip() if digest else title,
                    ts=models.to_utc_iso(created),
                    url=d.get("contentUrl") or "",
                    engagement={},          # 互动数需另一个接口,本期不取
                    media=[d["coverImgUrl"]] if d.get("coverImgUrl") else [],
                    repost_of=None,
                ))
            except Exception as exc:
                warn(f"wechat {person['id']} 跳过一条(msgId={msg_id}, idx={idx}): {exc}")
    return out


def fetch_wechat(client, person):
    payload = client.post("/api/v1/wechat_mp/v2/fetch_account_articles",
                          {"username": person["id"]})
    return parse_wechat(payload, person)
