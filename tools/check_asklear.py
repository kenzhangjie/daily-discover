#!/usr/bin/env python3
"""Asklear 接线自检。**一个积分都不花**,专门用来在真正开跑之前验证:

  1. ASKLEAR_API_KEY 在不在、对不对
  2. api.asklear.cn 通不通(routine 沙盒默认 Trusted 网络会 403 host_not_allowed)
  3. 探出来的那三个 REST 路径对不对
  4. linkedin.py 的 Asklear 客户端本身能不能跑

为什么值得单独写一个:适配器上线首跑要花 84 积分/人,而失败原因(key 没配 /
域名没放行 / 路径错)都长得像同一个报错。这个脚本只打免费端点,把三件事分开。

本地跑(key 从 ~/.config/keys.env 读,**不要写在命令行上** —— Asklear
官方文档明写「不要把凭据写入命令参数、仓库、截图或共享日志」,命令行会
留在 shell history 和会话记录里):
    python3 tools/check_asklear.py
routine 里跑(验网络白名单,key 从环境变量来):
    python3 tools/check_asklear.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import linkedin  # noqa: E402

PROBE_URL = "https://www.linkedin.com/in/chamath"


KEYS_ENV = os.path.expanduser("~/.config/keys.env")


def _key_from_env_file(path=KEYS_ENV):
    """从 keys.env 读 ASKLEAR_API_KEY。值可能带引号,要剥掉 ——
    en-ken-solar 的 exa key 就踩过这个:sed 取出来的串比真值长两位。"""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                k, _, v = line.partition("=")
                if k.strip() == "ASKLEAR_API_KEY":
                    return v.strip().strip("\'\"")
    except OSError:
        pass
    return None


def main():
    key = os.environ.get("ASKLEAR_API_KEY") or _key_from_env_file()
    if not key:
        print(f"✗ 找不到 ASKLEAR_API_KEY:环境变量里没有,{KEYS_ENV} 里也没有",
              file=sys.stderr)
        print("  本地:echo 'ASKLEAR_API_KEY=\"...\"' >> ~/.config/keys.env",
              file=sys.stderr)
        print("  routine:在环境变量里加这一条", file=sys.stderr)
        return 2
    print(f"· ASKLEAR_API_KEY 已设(长度 {len(key)})")
    print(f"· BASE = {linkedin.BASE}")
    api = linkedin.Asklear(key)

    # ① 余额。get_usage 不计费,同时验掉「key 有效 + 域名可达 + Bearer 头格式对」
    try:
        usage = api._call("GET", "/v1/usage")
    except Exception as e:  # noqa: BLE001
        print(f"✗ GET /v1/usage 失败: {e}", file=sys.stderr)
        print("  403 host_not_allowed → 环境网络白名单没加 api.asklear.cn",
              file=sys.stderr)
        print("  401 → key 无效或用错了环境(CN 生产是 api.asklear.cn)",
              file=sys.stderr)
        return 1
    bal = usage.get("wallet_balance", usage.get("balance"))
    print(f"✓ /v1/usage 通,余额 {bal} 积分")

    # ② 估价。免费,但走的是真正要用的那条 collections 路径
    try:
        quote = api.estimate(linkedin.TASK_CODE, {"url": PROBE_URL})
    except Exception as e:  # noqa: BLE001
        print(f"✗ POST /v1/collections/estimate 失败: {e}", file=sys.stderr)
        return 1
    cost = quote.get("upper_bound_credits")
    print(f"✓ /v1/collections/estimate 通,{linkedin.TASK_CODE} 报价 {cost} 积分")
    if not quote.get("quote_token"):
        print("✗ 报价没返回 quote_token,start 会失败", file=sys.stderr)
        return 1
    print("✓ quote_token 拿到了")

    # ③ 算一下这份名单的日常开销
    try:
        import models
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        people = models.load_sources(os.path.join(here, "sources.yaml")).get("linkedin", [])
        slots = {}
        for p in people:
            slots.setdefault(p.get("slot"), []).append(p["id"])
        per_day = max((len(v) for v in slots.values()), default=0) * (cost or 0)
        print(f"· 名单 {len(people)} 人分 {len(slots)} 组,"
              f"单日最多 {per_day} 积分,余额够跑 {int((bal or 0) / per_day) if per_day else '∞'} 天")
    except Exception as e:  # noqa: BLE001
        print(f"· (名单估算跳过: {e})")

    print("\n全通。没有启动任何任务,没花积分。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
