"""小宇宙播客适配器。

小宇宙**没有公开 RSS**(/podcast/<id>/feed 一律 404),但播客页是 Next.js
服务端渲染的,单集列表就在 `__NEXT_DATA__` 里:

    props.pageProps.podcast.episodes[] → eid / title / description / pubDate
                                          / clapCount / commentCount / duration

所以不需要 RSSHub。(Ken 那台 Railway 上的 RSSHub 从 2026-07-21 起就没部署成功过,
sources.yaml 里那两个 /xiaoyuzhou/ 路由一直是死的 —— 这个适配器把它替掉。)
"""
import html
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone

import models

# 打的是普通网页,UA 要像浏览器;用 daily-discover/1.0 会拿到精简页
XYZ_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
XYZ_PODCAST = "https://www.xiaoyuzhoufm.com/podcast/{}"
XYZ_EPISODE = "https://www.xiaoyuzhoufm.com/episode/{}"
TIMEOUT = 25

_NEXT_RE = re.compile(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _warn(msg):
    print(f"WARN {msg}", file=sys.stderr)


def _default_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": XYZ_UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def extract_next_data(page):
    """把 __NEXT_DATA__ 从页面里抠出来。抠不到就抛错 —— 页面改版了要当场知道,
    静默返回空会被读成「这个播客今天没更新」。"""
    m = _NEXT_RE.search(page or "")
    if not m:
        raise ValueError("页面里没有 __NEXT_DATA__,小宇宙可能改版了")
    return json.loads(m.group(1))


def _plain(raw, limit=600):
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>|</p>", "\n", raw, flags=re.I)
    text = html.unescape(_TAG_RE.sub("", text))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit].rstrip() + "…" if len(text) > limit else text


def _ts(raw):
    # pubDate 形如 2026-09-03T00:00:00.000Z
    s = (raw or "").strip().replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt).astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    raise ValueError(f"看不懂的 pubDate: {raw!r}")


def parse_xiaoyuzhou(data, person):
    """逐条容错:一集解析失败不能让整档播客当天消失。"""
    pod = ((data or {}).get("props") or {}).get("pageProps", {}).get("podcast") or {}
    name = person.get("name") or pod.get("title") or str(person["id"])
    out = []
    for ep in pod.get("episodes") or []:
        try:
            eid = ep.get("eid")
            if not eid:
                raise ValueError("缺 eid")
            title = (ep.get("title") or "").strip()
            body = _plain(ep.get("description"))
            text = f"{title}\n\n{body}" if body and not body.startswith(title) else (title or body)
            eng = {}
            for key, field in (("likes", "clapCount"), ("comments", "commentCount")):
                try:
                    n = int(ep.get(field) or 0)
                except (TypeError, ValueError):
                    n = 0
                if n:
                    eng[key] = n
            out.append(models.Post(
                id=f"podcast:{eid}",
                channel="podcast",
                author_handle=str(person["id"]),
                author_name=name,
                text=text,
                ts=_ts(ep.get("pubDate")),
                url=XYZ_EPISODE.format(eid),
                engagement=eng,
            ))
        except Exception as exc:  # noqa: BLE001
            _warn(f"小宇宙 {person.get('id')} 有一集解析失败,跳过: {exc}")
    return out


def fetch_xiaoyuzhou(client, person, get=None):
    """client 是 TikHub 客户端,这里用不到,保留只为与其他适配器同签名。
    整档失败往上抛给 run.collect 记 stats。"""
    doer = get or _default_get
    page = doer(XYZ_PODCAST.format(person["id"]))
    return parse_xiaoyuzhou(extract_next_data(page), person)
