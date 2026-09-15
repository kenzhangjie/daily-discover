# daily discover — 云 routine(每天 09:00 北京 / 01:00 UTC)

你在一个全新的 Anthropic 云沙盒里。你的任务只有四步:**下载脚本 → 跑 → 核对 → 报告**。
不做分析、不做摘要、不翻译 —— 这条流水线的产物是「全量信息流」,judgment 留给读者。
**绝不编造任何数据**:抓到多少就是多少,失败就如实报失败。

已设置的环境变量:`TIKHUB_API_KEY`、`R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID`、
`R2_SECRET_ACCESS_KEY`、`GH_TOKEN`。**绝不要打印它们的值**,只输出状态码/成败/字节数。
(`GH_TOKEN` 现在只用于私有的 `kenzhangjie/claude`;本仓库已公开,不带 token。)

⚠️ 这条不只是「别 echo」:**调试时不要用 `curl -v` / `-sv`**。`-v` 会把整个请求头
打进日志,`Authorization: token ...` 就跟着进去了(2026-09-14 实测被这样泄露过一次)。
要看请求为什么失败,用 `-w '%{http_code}'` 看状态码、`-D -` 只看**响应**头。

---

## Step 1 — 下载脚本与名单

`kenzhangjie/daily-discover` 现在是**公开仓库**,`raw.githubusercontent.com`
不带任何 token 就能读(2026-09-15 沙盒实测:公开仓库 raw 200;而 github.com 网页、
codeload、api.github.com 一律 403 —— 被拦的是普通 HTTP GET,不是 raw)。
所以脚本直接从 GitHub 拉,**没有 R2 兜底**:你在 GitHub 上改完 sources.yaml,
下一次运行立刻读到,不用等任何同步。

兜底是故意去掉的。一份可能过期的副本,会在主路径坏掉时安静地拿旧代码把今天跑完,
而报告照样全绿 —— 这正是 2026-09-15 那个「索引每天被清空」两天没人发现的机制。
raw 拉不到就**当场停**,今天不出数据,比出一份说不清是哪版代码产的数据强。

```bash
set -uo pipefail
GH_RAW="https://raw.githubusercontent.com/kenzhangjie/daily-discover/main"
CLAUDE_RAW="https://raw.githubusercontent.com/kenzhangjie/claude/main"
mkdir -p /tmp/dd /tmp/ipo-earnings
fail=0
for f in models.py tikhub.py xueqiu.py rss.py xiaoyuzhou.py blogs.py \
         dedup.py market.py radar.py publish.py run.py sources.yaml; do
  curl -sf --max-time 30 "$GH_RAW/$f" -o "/tmp/dd/$f" \
    && echo "OK $f" || { echo "FAIL $f"; fail=1; }
done
# 跨仓库依赖:打新/财报日历,真源在**私有**仓库 kenzhangjie/claude,所以这两个
# 仍要带 GH_TOKEN。拿不到不影响帖子流 —— market 是补充品,run.py 里包了 try/except。
for f in ipo-earnings/fetch_market.py ipo-earnings/watchlist.yaml; do
  curl -sf --max-time 30 -H "Authorization: token ${GH_TOKEN:-}" "$CLAUDE_RAW/$f" \
    -o "/tmp/$f" && echo "OK $f" || echo "SKIP $f(打新/财报这一块今天会空)"
done
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
print("index 天数:", len(idx.get("days") or []), [d["date"] for d in idx.get("days") or []])
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
- **`index 天数` 不能比昨天少。**只看「最新一天对不对」是看不出历史在丢的 ——
  2026-09-15 那次就是:两次运行都报「全绿」,而 `discover/2026-09-14.json`
  已经好端端躺在桶里却不在索引里了,页面上「加载更早一天」永远不出现。
  天数掉了就在报告里显眼写出来,不要因为当天数据对得上就判全绿。
- `stats` 里每个渠道都要有 `ok` 字段。**`count:0, ok:true`(今天真没人发)和
  `ok:false`(接口挂了)是两回事**,后者要在报告里显眼列出。
  订阅/播客这类低频源大多数日子就是 `count:0, ok:true`,那是正常的,不要报成故障。

## Step 4 — 报告

用中文写一段简短报告,包含:

1. 每个渠道的条数与 ok/error(直接贴 `stats`)
2. 去重后总条数、TikHub 调用次数、余额告警(如果 `warn.log` 里有)
3. 市场块与榜单块各自拿到多少条;哪些源失败了(`warn.log` 里的 `WARN` 行)
4. Step 1 有没有哪个文件 FAIL,或 `ipo-earnings` 两个文件没拿到
5. 异常:任何 `ok:false` 的渠道、任何非零退出码、任何核对不上的数字

页面在 https://discover.ken.solar(密码门),数据是它直接读的,你不需要做任何部署动作。

**报告里不要贴任何环境变量的值。**
