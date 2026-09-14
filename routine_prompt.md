# daily discover — 云 routine(每天 09:00 北京 / 01:00 UTC)

你在一个全新的 Anthropic 云沙盒里。你的任务只有四步:**下载脚本 → 跑 → 核对 → 报告**。
不做分析、不做摘要、不翻译 —— 这条流水线的产物是「全量信息流」,judgment 留给读者。
**绝不编造任何数据**:抓到多少就是多少,失败就如实报失败。

已设置的环境变量:`TIKHUB_API_KEY`、`R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID`、
`R2_SECRET_ACCESS_KEY`、`GH_TOKEN`。**绝不要打印它们的值**,只输出状态码/成败/字节数。

---

## Step 1 — 下载脚本与名单

沙盒的代理对 `api.github.com` 和 `git clone` 有按仓库门禁,但 `raw.githubusercontent.com`
可用(2026-07-08 实测)。所以逐个文件按 raw URL 下载,失败退 R2 公网副本。

```bash
set -uo pipefail
GH_RAW="https://raw.githubusercontent.com/kenzhangjie/daily-discover/main"
CLAUDE_RAW="https://raw.githubusercontent.com/kenzhangjie/claude/main"
R2="https://pub-42a2b5c1ec984024833f48ca358f4571.r2.dev"
mkdir -p /tmp/dd /tmp/ipo-earnings
fail=0
fetch() {   # fetch <raw_base> <repo_path> <dest>
  curl -sf --max-time 30 -H "Authorization: token ${GH_TOKEN:-}" "$1/$2" -o "$3" \
    && echo "OK github $2" \
    || { curl -sf --max-time 30 "$R2/code/$2" -o "$3" && echo "OK r2(fallback) $2"; } \
    || { echo "FAIL $2"; fail=1; }
}
for f in models.py tikhub.py xueqiu.py rss.py xiaoyuzhou.py blogs.py \
         dedup.py market.py radar.py publish.py run.py sources.yaml; do
  fetch "$GH_RAW" "$f" "/tmp/dd/$f"
done
# 跨仓库依赖:打新/财报日历。它的真源在 claude 仓库,「打新&财报」那条 routine 也在用,
# 不复制成两份。拿不到不影响帖子流 —— market 是补充品,run.py 里包了 try/except。
fetch "$CLAUDE_RAW" "ipo-earnings/fetch_market.py" "/tmp/ipo-earnings/fetch_market.py" || true
fetch "$CLAUDE_RAW" "ipo-earnings/watchlist.yaml"  "/tmp/ipo-earnings/watchlist.yaml"  || true
ls -la /tmp/dd/ /tmp/ipo-earnings/
[ "$fail" = 0 ] || { echo "核心文件缺失,停止"; exit 1; }
```

`/tmp/dd/` 下任一**核心**文件缺失或为 0 字节 → 报错并停止,不要试图自己写脚本的内容。
`ipo-earnings/` 两个文件拿不到 → 继续,在 Step 4 的报告里注明。

## Step 2 — 跑

```bash
cd /tmp/dd && timeout 1800 python3 run.py 2>/tmp/dd/warn.log
echo "exit=$?"
tail -40 /tmp/dd/warn.log
```

`run.py` 自己会:抓七个渠道 → 36h 截断 → 去重 → 拼市场块与榜单块 → 写 R2 的
`discover/<北京日期>.json` 和 `discover/index.json`。它也会自举 `pyyaml`。

**R2 上传失败必须让整个 routine 失败**(`run.py` 里 `upload_r2` 重试 3 次后抛错)——
页面静默停更比报错糟得多。市场数据和榜单相反:它们是补充品,失败只降级。

## Step 3 — 核对产出

```bash
TODAY=$(TZ='Asia/Shanghai' date +%F)
R2="https://pub-42a2b5c1ec984024833f48ca358f4571.r2.dev"
curl -s --max-time 30 "$R2/discover/index.json" -o /tmp/idx.json -w "index HTTP %{http_code} %{size_download}B\n"
curl -s --max-time 60 "$R2/discover/$TODAY.json" -o /tmp/shard.json -w "shard HTTP %{http_code} %{size_download}B\n"
python3 - <<'PY'
import json
idx=json.load(open('/tmp/idx.json')); sh=json.load(open('/tmp/shard.json'))
print("index 最新一天:", idx["days"][0] if idx.get("days") else "无")
print("分片 date:", sh.get("date"), "| posts:", len(sh.get("posts") or []))
print("stats:", json.dumps(sh.get("stats"), ensure_ascii=False))
print("market: polymarket", len((sh.get("market") or {}).get("polymarket") or []),
      "| ipo", len((sh.get("market") or {}).get("ipo") or []))
print("radar:", {k: len(v) for k, v in (sh.get("radar") or {}).items()})
PY
```

核对这几点,对不上就在报告里点名:
- 分片的 `date` 等于今天的北京日期
- `index.json` 的第一天就是今天,`total` 与分片里 `posts` 的条数一致
- `stats` 里每个渠道都要有 `ok` 字段。**`count:0, ok:true`(今天真没人发)和
  `ok:false`(接口挂了)是两回事**,后者要在报告里显眼列出。
  订阅/播客这类低频源大多数日子就是 `count:0, ok:true`,那是正常的,不要报成故障。

## Step 4 — 报告

用中文写一段简短报告,包含:

1. 每个渠道的条数与 ok/error(直接贴 `stats`)
2. 去重后总条数、TikHub 调用次数、余额告警(如果 `warn.log` 里有)
3. 市场块与榜单块各自拿到多少条;哪些源失败了(`warn.log` 里的 `WARN` 行)
4. Step 1 里有没有用到 R2 兜底,或 `ipo-earnings` 没拿到
5. 异常:任何 `ok:false` 的渠道、任何非零退出码、任何核对不上的数字

页面在 https://discover.ken.solar(密码门),数据是它直接读的,你不需要做任何部署动作。

**报告里不要贴任何环境变量的值。**
