#!/usr/bin/env python3
"""锐思投研 · 端到端验证

覆盖：所有只读接口的结构与关键数值、写入接口的回环、静态资源、
幂等性、以及「不编数据」的一致性检查（页面数值必须能从库里算出来）。
"""

import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8770"
DB = Path(__file__).resolve().parent / "data" / "market.db"

# 本机调试常常带着 HTTP_PROXY 环境变量，那会让 localhost 请求被代理转发而拿到 502。
# 这里显式绕过代理，直连本机。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASS, FAIL = [], []


def check(name, cond, got=None):
    (PASS if cond else FAIL).append(name)
    mark = "PASS " if cond else "FAIL "
    extra = "" if got is None else f"   → {got}"
    print(f"  {mark} {name}{extra}")


def get(path):
    with OPENER.open(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with OPENER.open(req, timeout=180) as r:
        return json.loads(r.read().decode())


# 这是一套每天都要跑的验证。期望值一律从库里推导，不硬编码某一天的数字 ——
# 硬编码快照值的测试第二天就会整片失败，那种「验证」等于没有。
_c = sqlite3.connect(DB)
_c.row_factory = sqlite3.Row
LATEST = _c.execute("SELECT MAX(trade_date) t FROM quote").fetchone()["t"]
VAL_LATEST = _c.execute("SELECT MAX(date) d FROM valuation").fetchone()["d"]


def dbval(sql, *args):
    r = _c.execute(sql, args).fetchone()
    return r[0] if r else None


print("=" * 78)
print("一、只读接口")
print("=" * 78)

meta = get("/api/meta")
check("meta 数据时点 = 库内最新交易日", meta.get("trade_date") == LATEST, f'{meta.get("trade_date")} / 库内 {LATEST}')
check("meta 数据时点格式合法", bool(__import__("re").match(r"^\d{4}-\d{2}-\d{2}$", meta.get("trade_date") or "")),
      meta.get("trade_date"))
check("meta 标注了数据源", "NeoData" in (meta.get("source") or ""), meta.get("source"))
check("meta 声明非实时推送", "不是逐笔实时" in (meta.get("note") or ""))
check("meta 有行数统计", (meta.get("row_counts") or {}).get("quote", 0) > 0)

idx = get("/api/indices")
items = idx["items"]
check("指数 8 个", len(items) == 8, len(items))
sh = [i for i in items if i["code"] == "000001.SH"]
sh_close = dbval("SELECT close FROM quote WHERE code='000001.SH' AND trade_date=?", LATEST)
check("上证收盘 = 库内值（接口不另造数）",
      sh and sh_close and abs(sh[0]["close"] - sh_close) < 0.01,
      f'{sh[0]["close"] if sh else None} / 库内 {sh_close}')
check("上证点位在合理区间", sh and 500 < sh[0]["close"] < 20000, sh[0]["close"] if sh else None)
check("上证涨跌幅在 ±11% 内（涨跌停约束）", sh and abs(sh[0]["chg_pct"]) <= 11, sh[0]["chg_pct"] if sh else None)
sh_amt = dbval("SELECT amount FROM quote WHERE code='000001.SH' AND trade_date=?", LATEST)
check("上证成交额 = 库内值", sh and sh_amt and abs(sh[0]["amount"] - sh_amt) < 1,
      f'{sh[0]["amount"] if sh else None} / 库内 {sh_amt}')
check("指数均带涨跌幅", all(i["chg_pct"] is not None for i in items))

sec = get("/api/sectors")
check("行业板块有数据", len(sec["items"]) >= 3, len(sec["items"]))
has_flow = [s for s in sec["items"] if s["main_net_in"] is not None]
check("部分板块带主力净流入", len(has_flow) >= 3, len(has_flow))
check("板块行均带名称与涨跌幅",
      all(s["sector_name"] and s["chg_pct"] is not None for s in sec["items"]))
# 板块只取当日榜单，但库应随每日同步累积历史 —— 这条守住「自动同步真的在写库」
_days = dbval("SELECT COUNT(DISTINCT trade_date) FROM sector_flow")
check("板块数据跨多交易日累积（自动同步生效）", _days and _days >= 2, f"{_days} 个交易日")

import datetime as _dt


def expected_dy(code):
    """按 sync.compute_dividend_yield 的口径，从库里独立复算一遍 TTM 股息率。"""
    q = _c.execute("SELECT close, trade_date FROM quote WHERE code=? AND kind='stock'"
                   " ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
    if not q or not q["close"]:
        return None
    cutoff = (_dt.datetime.strptime(q["trade_date"], "%Y-%m-%d")
              - _dt.timedelta(days=365)).strftime("%Y-%m-%d")
    rs = [r[0] for r in _c.execute(
        "SELECT per_10_shares FROM dividend WHERE code=? AND event_date>=?"
        " AND per_10_shares IS NOT NULL", (code, cutoff))]
    if not rs:
        return None
    ps = sum(rs) / 10.0
    return round(ps / q["close"] * 100.0, 3) if ps > 0 else None


stocks = get("/api/stocks")
sm = {s["code"]: s for s in stocks["items"]}
check("个股池非空", len(sm) >= 12, len(sm))
mt = sm.get("600519.SH")
check("贵州茅台在池内", mt is not None)
check("茅台 PE 为正且量级合理", mt and mt["pe_ttm"] and 3 < mt["pe_ttm"] < 120,
      mt["pe_ttm"] if mt else None)
check("茅台 PB 为正且量级合理", mt and mt["pb"] and 0.3 < mt["pb"] < 60,
      mt["pb"] if mt else None)
_ex = expected_dy("600519.SH")
check("茅台股息率 = 近12月已实施派息 ÷ 最新收盘价（独立复算一致）",
      mt and _ex is not None and mt["dividend_yield"] is not None
      and abs(mt["dividend_yield"] - _ex) < 0.002,
      f'{mt["dividend_yield"] if mt else None} / 复算 {_ex}')
cmb = sm.get("600036.SH")
check("招商银行 PB 为正且量级合理", cmb and cmb["pb"] and 0.1 < cmb["pb"] < 60,
      cmb["pb"] if cmb else None)
_exc = expected_dy("600036.SH")
check("招商银行股息率与独立复算一致",
      cmb and _exc is not None and cmb["dividend_yield"] is not None
      and abs(cmb["dividend_yield"] - _exc) < 0.002,
      f'{cmb["dividend_yield"] if cmb else None} / 复算 {_exc}')

det = get("/api/stock?code=600519.SH")
_db_mt_close = dbval("SELECT close FROM quote WHERE code='600519.SH'"
                     " AND kind='stock' ORDER BY trade_date DESC LIMIT 1")
check("个股详情收盘价 = 库内最新价", det["quote"]["close"] == _db_mt_close,
      f'{det["quote"]["close"]} / 库内 {_db_mt_close}')
check("个股详情与个股池口径一致", mt and det["quote"]["close"] == mt["close"])
check("个股详情有估值序列", len(det["valuation"]["history"]) >= 10, len(det["valuation"]["history"]))
check("估值分位已计算", det["valuation"]["pe_percentile"] is not None,
      det["valuation"]["pe_percentile"])
check("估值分位标注了样本窗口", "交易日" in (det["valuation"]["window"]["label"] or ""),
      det["valuation"]["window"]["label"])
check("个股详情有资金流", len(det["fund_flow"]["recent"]) >= 1, len(det["fund_flow"]["recent"]))
check("个股详情有财务", len(det["financial"]) >= 3, len(det["financial"]))
check("财务含报告期与归母净利润",
      det["financial"] and det["financial"][0]["report_period"] and det["financial"][0]["net_profit_attr"] is not None,
      (det["financial"][0]["report_period"], det["financial"][0]["net_profit_attr"]) if det["financial"] else None)

r404 = None
try:
    get("/api/stock?code=000000.XX")
except urllib.error.HTTPError as e:
    r404 = e.code
check("不存在的标的返回 404", r404 == 404, r404)

print()
print("=" * 78)
print("二、选股器（本地计算）")
print("=" * 78)

sc = get("/api/screener")
check("选股器返回全集", sc["universe"] == len(sm), f'{sc["universe"]} vs {len(sm)}')
check("无条件下全部命中", sc["selected"] == sc["universe"], sc["selected"])
cov = sc["coverage"]
check("标明估值覆盖范围", cov["valuation_fields"]["available"] < cov["valuation_fields"]["total"],
      f'{cov["valuation_fields"]["available"]}/{cov["valuation_fields"]["total"]}')
check("说明了覆盖差异原因", "单标的粒度" in cov["note"])

s2 = get("/api/screener?min_dy=3&require_valuation=1")
check("股息率≥3% 且需有估值 → 有结果", s2["selected"] >= 4, s2["selected"])
check("结果股息率均 ≥3%", all((x["dividend_yield"] or 0) >= 3 for x in s2["items"]),
      [round(x["dividend_yield"] or 0, 2) for x in s2["items"]])
check("结果均带 PE", all(x["pe_ttm"] is not None for x in s2["items"]))

s3 = get("/api/screener?max_pb=1&require_valuation=1")
check("PB≤1 筛选生效", all((x["pb"] or 9) <= 1 for x in s3["items"]), s3["selected"])
check("破净标的存在", s3["selected"] >= 2, s3["selected"])

s4 = get("/api/screener?min_mcap=10000")
check("市值≥1万亿 生效", all((x["mcap"] or 0) >= 10000 for x in s4["items"]), s4["selected"])

print()
print("=" * 78)
print("三、写入接口回环（自选股 / 持仓）")
print("=" * 78)

wl0 = get("/api/watchlist")["items"]
check("初始自选池状态已知", isinstance(wl0, list), len(wl0))

# 动态挑一个库里没有的标的，保证「按需补齐」这条路径每轮都被真正走一遍
CANDIDATES = ["600887.SH", "601398.SH", "600030.SH", "000858.SZ", "002415.SZ",
              "600276.SH", "601012.SH", "000333.SZ", "600585.SH", "601088.SH",
              "600028.SH", "601988.SH", "600050.SH", "000651.SZ", "002594.SZ",
              "600690.SH", "601668.SH", "600104.SH", "000002.SZ", "600009.SH",
              "601111.SH", "000725.SZ", "002304.SZ", "600438.SH", "601601.SH"]
TEST_CODE = None
for c in CANDIDATES:
    try:
        get(f"/api/stock?code={c}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            TEST_CODE = c
            break
check("找到一个库内尚无数据的标的用于测试", TEST_CODE is not None, TEST_CODE)

# 故意不传 name：服务端会向数据源确认权威名称。如果传个占位名，它会被写进行情/估值表，
# 之后整站都显示错名字 —— 这条路径必须被真正走一遍。
r = post("/api/watchlist", {"action": "add", "code": TEST_CODE})
check("加入自选成功", r.get("ok") is True, r.get("added"))
fetched = r.get("fetched") or []
check("新增触发了按需抓取", len(fetched) >= 3, [x.get("job") for x in fetched])
jobs = [x.get("job") or "" for x in fetched]
check("按需抓取覆盖行情/估值/财务/分红",
      all(any(j.startswith(p) for j in jobs) for p in
          ("quote_flow::", "valuation::", "financial::", "dividend::")), jobs)
check("按需抓取无失败项", all(not x.get("error") for x in fetched),
      [x for x in fetched if x.get("error")])

wl1 = get("/api/watchlist")["items"]
check("自选池已包含新标的", any(x["code"] == TEST_CODE for x in wl1), [x["code"] for x in wl1])
newrow = [x for x in wl1 if x["code"] == TEST_CODE]
check("新标的有真实收盘价", newrow and newrow[0]["close"] is not None,
      newrow[0]["close"] if newrow else None)
check("新标的名称来自数据源（不是占位符/代码）",
      newrow and newrow[0]["name"] and newrow[0]["name"] != TEST_CODE,
      newrow[0]["name"] if newrow else None)
check("新标的有估值", newrow and newrow[0]["pe_ttm"] is not None,
      newrow[0]["pe_ttm"] if newrow else None)

st2 = get(f"/api/stock?code={TEST_CODE}")
check("新标的可查详情", st2 and st2["name"], st2.get("name") if st2 else None)
check("新标的已标记在自选", st2.get("in_watchlist") is True)

bad = None
try:
    post("/api/watchlist", {"action": "add", "code": "12345"})
except urllib.error.HTTPError as e:
    bad = e.code
check("非法代码被拒绝", bad == 400, bad)

r = post("/api/watchlist", {"action": "remove", "code": TEST_CODE})
check("移除自选成功", r.get("ok") is True)
wl2 = [x["code"] for x in get("/api/watchlist")["items"]]
check("移除后不在自选池", TEST_CODE not in wl2)

# 持仓
p = post("/api/portfolio", {"action": "upsert", "code": "600519.SH", "name": "贵州茅台",
                            "shares": 100, "cost": 1200})
check("录入持仓成功", p.get("ok") is True)
# 不传 name 时也必须落到权威名称，而不是代码 —— 与自选股同一条规则
p2 = post("/api/portfolio", {"action": "upsert", "code": TEST_CODE, "shares": 10, "cost": 1})
pf2 = get("/api/portfolio")
nr = [x for x in pf2["positions"] if x["code"] == TEST_CODE]
check("持仓名称为数据源权威名称（非代码）",
      p2.get("ok") is True and nr and nr[0]["name"] and nr[0]["name"] != TEST_CODE,
      nr[0]["name"] if nr else None)
post("/api/portfolio", {"action": "remove", "code": TEST_CODE})
pf = get("/api/portfolio")
pos = [x for x in pf["positions"] if x["code"] == "600519.SH"]
check("持仓可读回", len(pos) == 1, len(pos))
if pos:
    q = pos[0]
    px = q["current_price"]
    check("持仓现价 = 库内最新收盘价",
          px and abs(px - dbval("SELECT close FROM quote WHERE code='600519.SH'"
                                " AND kind='stock' ORDER BY trade_date DESC LIMIT 1")) < 0.01, px)
    check("市值 = 现价 × 持股数", abs(q["market_value"] - px * 100) < 0.01, q["market_value"])
    check("盈亏 = 市值 − 成本额", abs(q["pnl"] - (px - 1200) * 100) < 0.01, q["pnl"])
    check("盈亏率计算正确", abs(q["pnl_pct"] - (px / 1200 - 1) * 100) < 0.01, q["pnl_pct"])
    check("权重计算正确", q["weight_pct"] is not None, q["weight_pct"])

badp = None
try:
    post("/api/portfolio", {"action": "upsert", "code": "600519.SH", "shares": 0})
except urllib.error.HTTPError as e:
    badp = e.code
check("持股数为 0 被拒绝", badp == 400, badp)

post("/api/portfolio", {"action": "remove", "code": "600519.SH"})
check("删除持仓成功", not [x for x in get("/api/portfolio")["positions"] if x["code"] == "600519.SH"])

print()
print("=" * 78)
print("四、数据血缘可追溯")
print("=" * 78)

raw = get("/api/raw_recall")
check("有原始返回留档", len(raw["items"]) >= 30, len(raw["items"]))
check("留档含接口类型", all(x["api_type"] for x in raw["items"][:5]))
check("留档含查询语句", all(x["query"] for x in raw["items"][:5]))
kinds = {x["api_type"] for x in raw["items"]}
check("覆盖多种接口类型", len(kinds) >= 6, sorted(kinds)[:8])

print()
print("=" * 78)
print("五、静态资源与安全性")
print("=" * 78)

with OPENER.open(BASE + "/", timeout=10) as r:
    html = r.read().decode()
    ctype = r.headers.get("Content-Type")
check("首页可访问且为 HTML", "text/html" in ctype and "锐思投研" in html)
check("首页含时效性说明", "不是逐笔实时推送" in html)
check("首页含免责声明", "不构成投资建议" in html)
with OPENER.open(BASE + "/app.js", timeout=10) as r:
    js = r.read().decode()
check("前端脚本可访问", "renderDash" in js)
check("前端不硬编码行情数字", "3911.87" not in js and "1257.12" not in js)
with OPENER.open(BASE + "/style.css", timeout=10) as r:
    css = r.read().decode()
check("红涨绿跌配色", "#d92b2b" in css and "#0f9960" in css)
check("无暗示实时的脉冲动画", "blink" not in css)

try:
    get("/../server.py")
    trav = None
except urllib.error.HTTPError as e:
    trav = e.code
check("目录穿越被拦截", trav == 404, trav)

print()
print("=" * 78)
print("六、库内一致性（页面数字必须能从库里算出来）")
print("=" * 78)

c = _c
row = c.execute("SELECT close FROM quote WHERE code='000001.SH' AND trade_date=?",
                (LATEST,)).fetchone()
check("库内上证收盘 = 页面值", row and sh and abs(row["close"] - sh[0]["close"]) < 0.01,
      row["close"] if row else None)
n = c.execute("SELECT COUNT(*) c FROM quote WHERE kind='index' AND trade_date=?",
              (LATEST,)).fetchone()["c"]
check("最新交易日的指数 8 行", n == 8, n)
n = c.execute("SELECT COUNT(DISTINCT trade_date) c FROM quote WHERE kind='index'").fetchone()["c"]
check("行情跨多交易日累积（自动同步生效）", n >= 2, f"{n} 个交易日")
n = c.execute("SELECT COUNT(DISTINCT code) c FROM valuation WHERE code LIKE '%.__'").fetchone()["c"]
check("有估值序列的指数/个股数 ≥ 9", n >= 9, n)
dy = c.execute("SELECT dividend_yield FROM valuation WHERE code='601318.SH'"
               " ORDER BY date DESC LIMIT 1").fetchone()
check("中国平安股息率已推算", dy and dy["dividend_yield"] and dy["dividend_yield"] > 2,
      dy["dividend_yield"] if dy else None)
# 股息率只应写在最新估值行上：它是用最新收盘价算的，写到历史行会让序列变成水平线
nul = c.execute("SELECT COUNT(*) c FROM valuation WHERE code='600519.SH'"
                " AND date=(SELECT MAX(date) FROM valuation WHERE code='600519.SH')"
                " AND pb IS NULL").fetchone()["c"]
check("茅台最新估值行有 PB", nul == 0, nul)
dyhist = c.execute("SELECT COUNT(*) c FROM valuation WHERE dividend_yield IS NOT NULL"
                   " AND date <> (SELECT MAX(date) FROM valuation v2"
                   " WHERE v2.code=valuation.code)").fetchone()["c"]
check("股息率未污染历史估值行（只写最新行）", dyhist == 0, dyhist)
n = c.execute("SELECT COUNT(*) c FROM breadth WHERE up_count IS NOT NULL").fetchone()["c"]
check("未写入不可信的涨跌家数", n == 0, n)

print()
print("=" * 78)
print(f"  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("    -", f)
print("=" * 78)
sys.exit(1 if FAIL else 0)
