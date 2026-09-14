"""統一的 Post 模型。三個渠道的原始返回差異極大,歸一化在這裡收口。"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
import subprocess
import sys

TWITTER_FMT = "%a %b %d %H:%M:%S %z %Y"

CHANNELS = ("twitter", "xueqiu", "xhs", "wechat", "rss", "podcast", "blog")

# sources.yaml 里不是「关注的人」的段落。放在同一个文件里是因为它们同样由 Ken
# 手改、同样靠 push 生效;但它们不是渠道,load_sources 要跳过而不是报「未知渠道」。
SETTINGS_KEYS = ("polymarket",)


def _yaml():
    """名单用 YAML 而非 JSON —— 这个文件是 Ken 手改的,注释和不带引号的中文
    值是刚需。pyyaml 不在沙盒基础镜像里,照 daily-insight 的做法自举装一次
    (同一个沙盒里它这么跑了几个月)。装不上就抛错:名单读不到没有降级可言。"""
    try:
        import yaml
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pyyaml"],
                       check=True)
        import yaml
    return yaml


def load_sources(path: str) -> dict:
    """读关注名单(YAML)。未知渠道、缺 id 一律抛错 —— 名单写错了要当场炸,
    静默少抓一个渠道会被误读成"今天这个渠道没人发"。"""
    with open(path, encoding="utf-8") as fh:
        raw = _yaml().safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError("sources 顶层必须是对象")
    out = {}
    for channel, people in raw.items():
        if channel in SETTINGS_KEYS:
            continue
        if channel not in CHANNELS:
            raise ValueError(f"未知渠道 {channel!r},只支持 {CHANNELS}")
        if not isinstance(people, list):
            raise ValueError(f"{channel} 必须是列表")
        for entry in people:
            if not isinstance(entry, dict) or not entry.get("id"):
                raise ValueError(f"{channel} 下有条目缺 id: {entry!r}")
        out[channel] = people
    return out


def load_settings(path: str) -> dict:
    """读 sources.yaml 里的非渠道配置段(见 SETTINGS_KEYS)。
    缺段或读不到都返回空 dict —— 调用方各自有默认值,不该因为配置缺失而崩。"""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = _yaml().safe_load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in SETTINGS_KEYS and isinstance(v, dict)}


def to_utc_iso(value: Any) -> str:
    """把各渠道的时间表示统一成 UTC ISO8601。

    小红书给 unix 秒(int),Twitter 给 "Sat Sep 12 20:12:03 +0000 2026",
    公众号给 unix 秒。拿不准的一律抛错 —— 静默产生错误时间比崩溃更糟。
    """
    if isinstance(value, bool):
        raise ValueError(f"unsupported timestamp: {value!r}")
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str):
        try:
            dt = datetime.strptime(value, TWITTER_FMT)
        except ValueError as exc:
            raise ValueError(f"unsupported timestamp: {value!r}") from exc
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raise ValueError(f"unsupported timestamp: {value!r}")


@dataclass
class Post:
    id: str
    channel: str
    author_handle: str
    author_name: str
    text: str
    ts: str
    url: str
    engagement: dict = field(default_factory=dict)
    media: list = field(default_factory=list)
    repost_of: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "channel": self.channel,
            "author": {"handle": self.author_handle, "name": self.author_name},
            "text": self.text,
            "ts": self.ts,
            "url": self.url,
            "engagement": self.engagement,
            "media": self.media,
            "repost_of": self.repost_of,
        }
