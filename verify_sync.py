#!/usr/bin/env python3
"""锐思投研 · 同步层验证

只跑同步逻辑，不联网。重点守住三件在生产里出过真问题的事：

1. 空召回必须自动换问法重试（上游按自然语言检索，同一意图换个措辞命中率不同；
   实测 18 轮同步里有 3 轮出现「某任务空召回」，当天那块数据就缺了）
2. 所有任务都必须配好备用问法，否则第 1 条对它是失效的
3. 「召回到内容但写入 0 行」不得静默算成功 —— 那会让 ok_count 虚高，
   「31/31 成功」其实一个数都没写进去
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db          # noqa: E402
import neodata as nd   # noqa: E402
import sync        # noqa: E402

PASS, FAIL = [], []


def check(name, cond, got=None):
    (PASS if cond else FAIL).append(name)
    mark = "PASS " if cond else "FAIL "
    extra = "" if got is None else f"   → {got}"
    print(f"  {mark} {name}{extra}")


EMPTY = {"data": {"apiData": {"apiRecall": []}}}
HIT = {"data": {"apiData": {"apiRecall": [
    {"tag": "t", "type": "统一行情查询", "content": "| 代码 |\n|---|\n| TEST.SH |"}]}}}

tmp = Path(tempfile.mktemp(suffix=".db"))
conn = db.connect(str(tmp))
db.init_db(conn)
calls = []


def handler(ctx, block):
    return ("quote",
            [{"trade_date": ctx["trade_date"], "code": "TEST.SH", "name": "测试",
              "kind": "stock", "close": 1.0}],
            ["trade_date", "code"])


def use(fn):
    nd.query = fn
    sync.nd.query = fn


print("=" * 78)
print("一、空召回自动换问法")
print("=" * 78)

for j in sync.build_jobs() + sync.query_one_job("600519.SH", "贵州茅台"):
    if not j.get("alt"):
        check(f"任务 {j['id']} 配了备用问法", False, "缺 alt")
missing = [j["id"] for j in sync.build_jobs() if not j.get("alt")]
check("全部同步任务都配了备用问法", not missing, missing or f"{len(sync.build_jobs())} 个任务全覆盖")
missing2 = [j["id"] for j in sync.query_one_job("600519.SH", "贵州茅台") if not j.get("alt")]
check("按需补齐任务都配了备用问法", not missing2, missing2 or "4 个任务全覆盖")

calls.clear()
use(lambda q, **kw: (calls.append(q), EMPTY if q == "主问法" else HIT)[1])
n, err = sync.run_job(
    conn,
    {"id": "t1", "query": "主问法", "alt": ["备用问法1", "备用问法2"],
     "handlers": {"统一行情查询": handler}},
    {"fetched_at": db.now_iso()}, "2026-09-29")
check("主问法空召回时自动改用备用问法", err is None and n == 1, f"写入 {n} / err={err}")
check("问法按主 → 备用顺序尝试", calls == ["主问法", "备用问法1"], calls)
check("血缘记录的是真正命中的问法（可溯源）",
      [r[0] for r in conn.execute("SELECT DISTINCT query FROM raw_recall")] == ["备用问法1"])

calls.clear()
use(lambda q, **kw: (calls.append(q), EMPTY)[1])
n, err = sync.run_job(
    conn, {"id": "t2", "query": "主问法", "alt": ["备用问法1", "备用问法2"], "handlers": {}},
    {"fetched_at": db.now_iso()}, "2026-09-29")
check("全部问法都空召回时明确报错", n == 0 and err and "3 种问法" in err, err)
check("三种问法确实都被试过", len(calls) == 3, calls)

print()
print("=" * 78)
print("二、空写入不得算成功")
print("=" * 78)

use(lambda q, **kw: HIT)
n, err = sync.run_job(
    conn, {"id": "t3", "query": "主问法", "handlers": {"别的类型": handler}},
    {"fetched_at": db.now_iso()}, "2026-09-29")
check("召回成功但无 handler 命中 → 报错而非静默成功",
      n == 0 and err and "无有效数据行" in err, err)
check("报错里带上 api_type 便于定位", err and "统一行情查询" in err, err)

print()
print("=" * 78)
print("三、异常仍向上抛（保留连续失败熔断）")
print("=" * 78)


def boom(q, **kw):
    raise RuntimeError("模拟网络中断")


use(boom)
try:
    sync.run_job(conn, {"id": "t4", "query": "主问法", "alt": ["备用问法1"], "handlers": {}},
                 {"fetched_at": db.now_iso()}, "2026-09-29")
    check("网络异常向上抛出", False, "未抛出")
except RuntimeError:
    check("网络异常向上抛出（交给上层连续失败熔断）", True)

print()
print("=" * 78)
print("四、推算口径：股息率只写最新估值行")
print("=" * 78)

conn.execute("INSERT INTO quote(trade_date,code,name,kind,close,fetched_at)"
             " VALUES('2026-09-29','T.SH','测试','stock',10.0,?)", (db.now_iso(),))
conn.execute("INSERT INTO valuation(date,code,name,pe_ttm,fetched_at)"
             " VALUES('2026-09-01','T.SH','测试',10.0,?)", (db.now_iso(),))
conn.execute("INSERT INTO valuation(date,code,name,pe_ttm,fetched_at)"
             " VALUES('2026-09-28','T.SH','测试',11.0,?)", (db.now_iso(),))
conn.execute("INSERT INTO dividend(code,event_date,per_10_shares,fetched_at)"
             " VALUES('T.SH','2026-06-01',5.0,?)", (db.now_iso(),))
conn.commit()
dy = sync.compute_dividend_yield(conn, "T.SH")
rows = list(conn.execute("SELECT date,dividend_yield FROM valuation WHERE code='T.SH'"
                         " ORDER BY date"))
check("股息率已算出（近12月派息 ÷ 最新收盘价）", dy is not None and abs(dy - 5.0) < 0.01,
      f"{dy}（10派5 → 0.5/10.0 = 5%）")
check("只写入最新估值行", rows[-1][1] == dy, f"最新行 {tuple(rows[-1])}")
check("历史估值行保持空（不写成水平线）", rows[0][1] is None, f"历史行 {tuple(rows[0])}")

print()
print("=" * 78)
print(f"  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("    -", f)
print("=" * 78)

conn.close()
try:
    os.unlink(tmp)
except OSError:
    pass
sys.exit(1 if FAIL else 0)
