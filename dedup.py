"""转发去重。同一条内容经多个关注对象转发时只留最早的一条。

空正文绝不合并 —— 小红书的纯图笔记正文可能为空,合并会把
不相干的内容当成重复。
"""
import hashlib
import re

RT_PREFIX = re.compile(r"^RT @[A-Za-z0-9_]{1,15}:\s*")
WS = re.compile(r"\s+")


def _key(text):
    t = RT_PREFIX.sub("", text or "")
    t = WS.sub(" ", t).strip()
    if not t:
        return None                      # 空正文不参与去重
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def dedup(posts):
    best = {}
    passthrough = []
    for p in posts:
        k = _key(p.text)
        if k is None:
            passthrough.append(p)
            continue
        cur = best.get(k)
        if cur is None or p.ts < cur.ts:
            best[k] = p
    kept = list(best.values()) + passthrough
    kept.sort(key=lambda p: p.ts, reverse=True)
    return kept
