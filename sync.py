#!/usr/bin/env python3
"""锐思投研 · 数据同步层

从 NeoData 拉取真实数据并落库。幂等：同一交易日重复执行只覆盖不重复。

用法：
    python3 sync.py                  # 同步当日（自动跳过 7 天内已同步过的财务）
    python3 sync.py --date 2026-09-18
    python3 sync.py --only indices,sectors
    python3 sync.py --with-financial   # 强制重取财务
    python3 sync.py --list             # 列出所有任务

数据来源全部为 NeoData（腾讯）。每个 API 返回块都会原样存档到 raw_recall，
保证任何一个数字都能回溯到它的原始返回。
"""

import argparse
import sys
import time
import traceback
from datetime import datetime, timedelta

import db
import neodata as nd

# ----------------------------------------------------------------------------
# 重点研究池：覆盖不同风格，保证平台有可比较的样本
# ----------------------------------------------------------------------------
FOCUS_POOL = [
    ("600519.SH", "贵州茅台"),   # 价值 / 高股息
    ("300750.SZ", "宁德时代"),   # 成长 / 估值低位
    ("300308.SZ", "中际旭创"),   # AI 算力 / 高估值
    ("600036.SH", "招商银行"),   # 金融 / 红利
    ("601318.SH", "中国平安"),   # 保险 / 大盘蓝筹
    ("300502.SZ", "新易盛"),     # AI 算力 / 高弹性
    ("600900.SH", "长江电力"),   # 公用事业 / 类债
    ("601899.SH", "紫金矿业"),   # 资源 / 周期
]

INDEX_CODES = [
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"),
    ("899050.BJ", "北证50"),
]

FINANCIAL_FRESH_DAYS = 7


# ----------------------------------------------------------------------------
# 各 API 块的解析 handler
# ----------------------------------------------------------------------------

def _entity_tables(content):
    """把 content 拆成 [(code, name, table), ...]

    多标的的行情查询返回形如：
        ### 000001.SH 上证指数
        市场: ... | 币种: CNY
        | 日期 | 开盘 | ...
    标题顺序与表格顺序一一对应。
    """
    import re

    lines = content.splitlines()
    pairs = []
    for line in lines:
        s = line.strip()
        if s.startswith("#"):
            t = s.strip("# ").strip()
            m = re.match(r"^([0-9A-Za-z]+(?:[._][A-Za-z]+)?)\s+(.+)$", t)
            if m:
                pairs.append((m.group(1), m.group(2).strip()))
    tables = nd.parse_tables(content)
    out = []
    for i, (code, name) in enumerate(pairs):
        if i < len(tables):
            out.append((code, name, tables[i]))
    if not out and tables and len(pairs) == 0:
        out.append((None, None, tables[0]))
    return out


def h_index_quote(ctx, block):
    """统一行情查询 → quote (kind=index)"""
    rows = []
    for code, name, t in _entity_tables(block.content):
        for r in t["rows"]:
            close = nd.num_cell(r, t["header"], "收盘/最新", "收盘")
            if close is None:
                # 休市日「未开盘」占位行：日期非交易日、各价量全为 --，
                # 落库只会得到全 NULL 垃圾行（2026-09-25 曾写入 16 行）。直接丢弃。
                continue
            rows.append(
                {
                    "trade_date": nd.norm_date(nd.cell(r, t["header"], "日期")),
                    "code": code,
                    "name": name,
                    "kind": "index",
                    "open": nd.num_cell(r, t["header"], "开盘"),
                    "high": nd.num_cell(r, t["header"], "最高"),
                    "low": nd.num_cell(r, t["header"], "最低"),
                    "close": nd.num_cell(r, t["header"], "收盘/最新", "收盘"),
                    "prev_close": nd.num_cell(r, t["header"], "前收"),
                    "chg": nd.num_cell(r, t["header"], "涨跌额"),
                    "chg_pct": nd.num_cell(r, t["header"], "涨跌幅"),
                    "amplitude": nd.num_cell(r, t["header"], "振幅"),
                    "volume": nd.num_cell(r, t["header"], "成交量"),
                    "amount": nd.num_cell(r, t["header"], "成交额"),
                    "turnover": nd.num_cell(r, t["header"], "换手率"),
                    "vol_ratio": nd.num_cell(r, t["header"], "量比"),
                    "fetched_at": ctx["fetched_at"],
                }
            )
    return "quote", rows, ["trade_date", "code"]


def h_stock_quote(ctx, block):
    """统一行情查询 → quote (kind=stock)，代码来自任务上下文"""
    rows = []
    tables = nd.parse_tables(block.content) or [{"header": [], "rows": []}]
    t = tables[0]
    for r in t.get("rows", []):
        close = nd.num_cell(r, t["header"], "收盘/最新", "收盘")
        if close is None:
            # 同 h_index_quote：丢掉休市日「未开盘」占位行，避免全 NULL 垃圾行落库。
            continue
        rows.append(
            {
                "trade_date": nd.norm_date(nd.cell(r, t["header"], "日期")) or ctx["trade_date"],
                "code": ctx["code"],
                "name": ctx["name"],
                "kind": "stock",
                "open": nd.num_cell(r, t["header"], "开盘"),
                "high": nd.num_cell(r, t["header"], "最高"),
                "low": nd.num_cell(r, t["header"], "最低"),
                "close": close,
                "prev_close": nd.num_cell(r, t["header"], "前收"),
                "chg": nd.num_cell(r, t["header"], "涨跌额"),
                "chg_pct": nd.num_cell(r, t["header"], "涨跌幅"),
                "amplitude": nd.num_cell(r, t["header"], "振幅"),
                "volume": nd.num_cell(r, t["header"], "成交量"),
                "amount": nd.num_cell(r, t["header"], "成交额"),
                "turnover": nd.num_cell(r, t["header"], "换手率"),
                "vol_ratio": nd.num_cell(r, t["header"], "量比"),
                "fetched_at": ctx["fetched_at"],
            }
        )
    return "quote", rows, ["trade_date", "code"]


def h_valuation(ctx, block):
    """统一估值查询 → valuation（按日期时序落库，用于分位计算）"""
    import re

    rows = []
    for t in nd.parse_tables(block.content):
        for r in t["rows"]:
            d = nd.norm_date(nd.cell(r, t["header"], "估值日期"))
            if not d:
                continue
            rows.append(
                {
                    "date": d,
                    "code": ctx["code"],
                    "name": ctx.get("name"),
                    "pe_static": nd.num_cell(r, t["header"], "静态市盈率"),
                    "pe_dynamic": nd.num_cell(r, t["header"], "动态市盈率"),
                    "pe_ttm": nd.num_cell(r, t["header"], "扣非后滚动市盈率"),
                    "pb": None,
                    "dividend_yield": nd.num_cell(r, t["header"], "滚动股息率", "静态股息率"),
                    "raw": db.json_dumps(dict(zip(t["header"], r))),
                    "fetched_at": ctx["fetched_at"],
                }
            )
    # 时序表里没有 PB 列，但正文汇总行直供「市净率（倍）」（如 **市净率（倍）**: 6.15）。
    # 该值是当前值，只落到最新估值日那一行，不摊给历史行。
    m = re.search(r"市净率（倍）\**\s*[:：]\s*([\d.]+)", block.content)
    if m and rows:
        pb = nd.num(m.group(1))
        if pb is not None:
            latest = max(r["date"] for r in rows)
            for r in rows:
                if r["date"] == latest:
                    r["pb"] = pb
    return "valuation", rows, ["date", "code"]


def h_fund_flow(ctx, block):
    """统一资金流向 → fund_flow

    名称以正文的「**标的名称**: X」为准，ctx["name"] 仅兜底。
    原因：按需补齐（/api/fetch，用户新增自选股）会把用户填写的名称透传下来，
    而 h_stock_quote 是从正文取名、本函数原先直接取 ctx["name"] ——
    两处口径不一致，导致用户填占位名（如「测试标的」）时 fund_flow.name 被污染。
    """
    import re

    m = re.search(r"标的名称\*{0,2}\s*[:：]\s*([^\s*|]+)", block.content or "")
    name = (m.group(1).strip() if m else "") or ctx.get("name")
    rows = []
    for t in nd.parse_tables(block.content):
        for r in t["rows"]:
            d = nd.norm_date(nd.cell(r, t["header"], "交易日期", "日期")) or ctx["trade_date"]
            if not d:
                continue
            rows.append(
                {
                    "trade_date": d,
                    "code": ctx["code"],
                    "name": name,
                    "main_net_in": nd.num_cell(r, t["header"], "主力净流入"),
                    "main_in": nd.num_cell(r, t["header"], "主力流入"),
                    "main_out": nd.num_cell(r, t["header"], "主力流出"),
                    "retail_net_in": nd.num_cell(r, t["header"], "散户净流入"),
                    "super_net_in": nd.num_cell(r, t["header"], "超大单净流入"),
                    "big_net_in": nd.num_cell(r, t["header"], "大单净流入"),
                    "raw": db.json_dumps(dict(zip(t["header"], r))),
                    "fetched_at": ctx["fetched_at"],
                }
            )
    return "fund_flow", rows, ["trade_date", "code"]


def h_breadth(ctx, block):
    """大盘市场宽度统计 → breadth

    该接口正文提供两类数据：
      1) 「成交统计」表：成交量 / 成交额 / 较昨日成交额变化量（按市场分行）；
      2) 「涨跌家数」段落：沪深两市口径的 上涨/下跌/平盘 家数，括号内附 涨停/跌停 家数。

    修正记录（2026-09-30）：原 docstring 写「该接口只提供成交量/成交额，不提供涨跌家数」，
    与上游实际返回不符 —— 上游**直供**全市场涨跌家数。因这条错误假设，涨跌家数此前只能靠
    申万行业「涨股比」求和推导，而推导有 MIN_SECTORS_FOR_BREADTH=25 的覆盖率门槛、
    常年达不到，导致 up_count/down_count/flat_count 恒为 NULL（挂了一个多月的 issue）。
    现改为直接解析上游原文：口径就是全市场，无需覆盖率门槛，也比用少数行业推导更准。
    """
    import re

    content = block.content or ""
    rows = []
    for t in nd.parse_tables(content):
        for r in t["rows"]:
            mkt = nd.cell(r, t["header"], "市场")
            if not mkt:
                continue
            rows.append(
                {
                    "trade_date": ctx["trade_date"],
                    "market": mkt,
                    "volume": nd.num_cell(r, t["header"], "成交量"),
                    "amount": nd.num_cell(r, t["header"], "成交额"),
                    "amount_chg": nd.num_cell(r, t["header"], "较昨日成交额变化量"),
                    "up_count": None,
                    "down_count": None,
                    "flat_count": None,
                    "limit_up_count": None,
                    "limit_down_count": None,
                    "raw": db.json_dumps(dict(zip(t["header"], r))),
                    "fetched_at": ctx["fetched_at"],
                }
            )

    # 只在「涨跌家数」这一小节里取数，避免误命中「涨跌幅区间统计」或「昨日复盘」里的同词。
    # 注意：小节标题形如 `**涨跌家数**`，闭合的 `**` 紧跟在标题文字之后，
    # 所以必须跳过它再找下一个 `**`，否则会切出一个空片段。
    i = content.find("涨跌家数")
    seg = ""
    if i >= 0:
        start = i + len("涨跌家数")
        j = content.find("**", start)
        if j == start:  # 紧跟的是标题自身的闭合标记
            j = content.find("**", j + 2)
        seg = content[i : j if j > start else i + 400]

    def pick(pat):
        m = re.search(pat, seg)
        return nd.num(m.group(1)) if m else None

    counts = {
        "up_count": pick(r"上涨\s*([\d,]+)\s*家"),
        "down_count": pick(r"下跌\s*([\d,]+)\s*家"),
        "flat_count": pick(r"平盘\s*([\d,]+)\s*家"),
        "limit_up_count": pick(r"涨停\s*([\d,]+)\s*家"),
        "limit_down_count": pick(r"跌停\s*([\d,]+)\s*家"),
    }
    # 涨跌家数是「沪深两市」口径 → 落到「两市汇总」那一行
    if counts["up_count"] is not None:
        for r in rows:
            if r["market"] == "两市汇总":
                r.update(counts)
                break
    return "breadth", rows, ["trade_date", "market"]


def h_sector_flow(ctx, block):
    """板块资金排行 → sector_flow"""
    import re

    rows = []
    for t in nd.parse_tables(block.content):
        for r in t["rows"]:
            scode = nd.cell(r, t["header"], "板块代码")
            sname = nd.cell(r, t["header"], "板块名称")
            if not sname:
                continue
            ratio = nd.cell(r, t["header"], "涨股比") or ""
            m = re.match(r"\s*(\d+)\s*/\s*(\d+)", str(ratio))
            up_cnt = int(m.group(1)) if m else None
            tot_cnt = int(m.group(2)) if m else None
            rows.append(
                {
                    "trade_date": ctx["trade_date"],
                    "sector_code": scode or sname,
                    "sector_name": sname,
                    # 「板块资金排行」表头是「当日涨跌幅（%）」，「板块涨跌排行」是「涨跌幅（%）」，
                    # 两个接口共用本解析器，必须都认，否则涨跌排行会整行解析成 NULL。
                    "chg_pct": nd.num_cell(r, t["header"], "当日涨跌幅", "涨跌幅"),
                    "amount": nd.num_cell(r, t["header"], "成交额"),
                    "turnover": nd.num_cell(r, t["header"], "换手率"),
                    "up_ratio": up_cnt,
                    "main_net_in": nd.num_cell(r, t["header"], "主力净流入"),
                    "main_net_in_5d": nd.num_cell(r, t["header"], "5日主力净流入"),
                    "main_net_in_20d": nd.num_cell(r, t["header"], "20日主力净流入"),
                    "lead_name": nd.cell(r, t["header"], "领涨股名称"),
                    "lead_code": nd.cell(r, t["header"], "领涨股代码"),
                    "lead_chg_pct": nd.num_cell(r, t["header"], "领涨股涨跌幅"),
                    "fetched_at": ctx["fetched_at"],
                }
            )
            ctx.setdefault("breadth_acc", []).append((up_cnt, tot_cnt))
    return "sector_flow", rows, ["trade_date", "sector_code"]


def h_leaderboard(ctx, block):
    """A股个股交易排行榜 → quote(kind=stock) + 补充市值

    这个接口是少有的「一次拿多只股票、且含市值」的入口，
    选股器的全市场维度就靠它。
    """
    rows = []
    extra = []
    for t in nd.parse_tables(block.content):
        for r in t["rows"]:
            code = nd.cell(r, t["header"], "股票代码")
            name = nd.cell(r, t["header"], "股票名称")
            if not code:
                continue
            close = nd.num_cell(r, t["header"], "最新价")
            rows.append(
                {
                    "trade_date": ctx["trade_date"],
                    "code": code,
                    "name": name,
                    "kind": "stock",
                    "close": close,
                    "chg_pct": nd.num_cell(r, t["header"], "当日涨跌幅", "涨跌幅"),
                    "turnover": nd.num_cell(r, t["header"], "换手率"),
                    "vol_ratio": nd.num_cell(r, t["header"], "量比"),
                    "amplitude": nd.num_cell(r, t["header"], "振幅"),
                    "fetched_at": ctx["fetched_at"],
                }
            )
            extra.append(
                {
                    "trade_date": ctx["trade_date"],
                    "code": code,
                    "name": name,
                    "mcap": nd.num_cell(r, t["header"], "总市值"),
                    "float_mcap": nd.num_cell(r, t["header"], "流通市值"),
                    "fetched_at": ctx["fetched_at"],
                }
            )
    ctx.setdefault("pending_extra", []).extend(extra)
    return "quote", rows, ["trade_date", "code"]


def h_market_cap(ctx, block):
    """市值与股本 → 补 mcap / float_mcap"""
    rows = []
    for t in nd.parse_tables(block.content):
        for r in t["rows"]:
            rows.append(
                {
                    "trade_date": ctx["trade_date"],
                    "code": ctx["code"],
                    "name": ctx.get("name"),
                    "mcap": nd.num_cell(r, t["header"], "总市值"),
                    "float_mcap": nd.num_cell(r, t["header"], "流通市值"),
                    "fetched_at": ctx["fetched_at"],
                }
            )
    return "market_cap", rows, ["trade_date", "code"]


def h_financial(ctx, block):
    """利润表 / 综合财务指标 → financial"""
    rows = []
    for t in nd.parse_tables(block.content):
        for r in t["rows"]:
            period = nd.cell(r, t["header"], "报告期")
            if not period:
                continue
            rows.append(
                {
                    "code": ctx["code"],
                    "report_period": period,
                    "publish_date": nd.norm_date(nd.cell(r, t["header"], "发布日期")),
                    "revenue": nd.num_cell(r, t["header"], "营业总收入", "营业收入"),
                    "net_profit": nd.num_cell(r, t["header"], "净利润"),
                    "net_profit_attr": nd.num_cell(r, t["header"], "归母净利润"),
                    "eps": nd.num_cell(r, t["header"], "基本每股收益", "每股收益(eps)"),
                    "raw": db.json_dumps(dict(zip(t["header"], r))),
                    "fetched_at": ctx["fetched_at"],
                }
            )
    return "financial", rows, ["code", "report_period"]


def h_financial_metrics(ctx, block):
    """综合财务指标 → 抽 每股净资产 / ROE / 毛利率 存入 financial.raw 并回填 valuation.pb 所需字段"""
    import re

    rows = []
    for t in nd.parse_tables(block.content):
        for r in t["rows"]:
            period = nd.cell(r, t["header"], "报告期")
            if not period:
                continue
            bps = nd.num_cell(r, t["header"], "每股净资产")
            rows.append(
                {
                    "code": ctx["code"],
                    "report_period": period,
                    "publish_date": nd.norm_date(nd.cell(r, t["header"], "发布日期")),
                    "revenue": nd.num_cell(r, t["header"], "营业总收入"),
                    "net_profit": nd.num_cell(r, t["header"], "净利润"),
                    "net_profit_attr": nd.num_cell(r, t["header"], "归母净利润"),
                    "eps": nd.num_cell(r, t["header"], "每股收益(eps)", "基本每股收益"),
                    "raw": db.json_dumps(dict(zip(t["header"], r))),
                    "fetched_at": ctx["fetched_at"],
                }
            )
            if bps:
                ctx.setdefault("bps", {})[period] = bps
    return "financial", rows, ["code", "report_period"]


def h_dividend(ctx, block):
    """分红派息详细 → dividend

    估值接口的股息率字段恒为空，所以 TTM 股息率由近 12 个月已实施派息推算，
    见 compute_dividend_yield。口径会在前端明确标注。
    """
    rows = []
    for t in nd.parse_tables(block.content):
        for r in t["rows"]:
            ed = nd.norm_date(nd.cell(r, t["header"], "事件日期"))
            if not ed:
                continue
            per10 = nd.num_cell(r, t["header"], "每10股派息")
            rows.append(
                {
                    "code": ctx["code"],
                    "event_date": ed,
                    "reg_date": nd.norm_date(nd.cell(r, t["header"], "股权登记日")),
                    "ex_date": nd.norm_date(nd.cell(r, t["header"], "除权日")),
                    "currency": nd.cell(r, t["header"], "分红币种"),
                    "per_10_shares": per10,
                    "plan": nd.cell(r, t["header"], "分红方案描述"),
                    "fetched_at": ctx["fetched_at"],
                }
            )
    return "dividend", rows, ["code", "event_date"]


def h_index_valuation(ctx, block):
    """指数估值 → valuation（code 为指数代码），用于指数层面的估值时序"""
    rows = []
    for t in nd.parse_tables(block.content):
        for r in t["rows"]:
            d = nd.norm_date(nd.cell(r, t["header"], "交易日"))
            code = nd.cell(r, t["header"], "指数代码")
            if not d or not code:
                continue
            rows.append(
                {
                    "date": d,
                    "code": code,
                    "name": nd.cell(r, t["header"], "指数简称"),
                    "pe_ttm": nd.num_cell(r, t["header"], "滚动市盈率PE(TTM)"),
                    "pe_static": nd.num_cell(r, t["header"], "静态市盈率PE(LYR)"),
                    "pb": nd.num_cell(r, t["header"], "市净率PB(LF最新)", "市净率PB(MRQ最近季)"),
                    "dividend_yield": None,
                    "raw": db.json_dumps(dict(zip(t["header"], r))),
                    "fetched_at": ctx["fetched_at"],
                }
            )
    return "valuation", rows, ["date", "code"]


def compute_dividend_yield(conn, code):
    """近 12 个月已实施派息合计 ÷ 最新收盘价 → TTM 股息率。

    只在确实有派息记录时写入；拿不到就保持 NULL，不猜。
    """
    q = conn.execute(
        "SELECT close, trade_date FROM quote WHERE code=? AND kind='stock'"
        " ORDER BY trade_date DESC LIMIT 1",
        (code,),
    ).fetchone()
    if not q or not q["close"]:
        return None
    cutoff = (datetime.strptime(q["trade_date"], "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d")
    rs = conn.execute(
        "SELECT per_10_shares FROM dividend WHERE code=? AND event_date>=? AND per_10_shares IS NOT NULL",
        (code, cutoff),
    ).fetchall()
    if not rs:
        return None
    per_share = sum(r["per_10_shares"] for r in rs) / 10.0
    if per_share <= 0:
        return None
    dy = round(per_share / q["close"] * 100.0, 3)
    # 只把「最新估值日」那一行更新为按最新收盘价算出的 TTM 股息率。
    #
    # 这里**不能**再像以前那样先 `UPDATE valuation SET dividend_yield=NULL WHERE code=?`
    # 清空该标的全部历史行 —— 那个写法每天同步都会毁掉历史数据（与 valuation.pb 的
    # 老 bug 完全同类，2026-09-30 修）：
    #   1) h_valuation 会从估值窗口时序表里解析**按日**的「滚动股息率/静态股息率」，
    #      窗口覆盖到的历史日期因此带上了真实的历史股息率；清空会把它们一并抹掉。
    #   2) 本函数写入的值是用「当次同步时的最新收盘价」算的，落到当次的最新估值日上，
    #      本身就是该日的正确口径；历史行保留自己那天的值时点序列才是对的，
    #      并不存在「变成一条水平线」的问题（旧写法把日期戳连续的同值写进历史行才会）。
    # 若估值序列新增了更晚的日期，新日期的行由 h_valuation 逐日解析或由本次 UPDATE 填充，
    # 旧日期保留旧值 —— 不会有「最新一行显示 —」的情况。
    conn.execute(
        "UPDATE valuation SET dividend_yield=?"
        " WHERE code=? AND date=(SELECT MAX(date) FROM valuation WHERE code=?)",
        (dy, code, code),
    )
    return dy


HANDLERS = {
    "统一行情查询": h_stock_quote,      # 由任务决定用 index 还是 stock 版本
    "统一估值查询": h_valuation,
    "统一资金流向": h_fund_flow,
    "综合财务指标": h_financial_metrics,
    "利润表": h_financial,
    "大盘市场宽度统计": h_breadth,
    "板块资金排行": h_sector_flow,
    "板块涨跌排行": h_sector_flow,
    "A 股个股交易排行榜": h_leaderboard,
    "股本结构": h_market_cap,
    "分红派息详细": h_dividend,
    "指数估值": h_index_valuation,
}

# 涨跌家数只有当覆盖足够多行业时才允许推导，否则宁可不给也不误导
MIN_SECTORS_FOR_BREADTH = 25

# 这些表的一行会被多个接口先后写入，新接口不提供的列不得把旧值擦成 NULL。
# sector_flow：sectors（申万行业主力资金）/ sectors_gain（板块涨幅）/ sectors_lose（净流出）
# 会命中同一 (trade_date, sector_code)，而「板块涨跌排行」本就不含主力净流入/成交额。
#
# valuation：估值时序接口每次返回一个**滚动窗口**（含近几个交易日），h_valuation 对窗口内
# 每一行都写 pb=None（时序表里没有 PB 列），PB 仅由正文「市净率（倍）」或每股净资产回填、
# 且只落到最新估值日。于是 upsert 会把窗口内**历史日期**上早已回填好的 pb 擦成 NULL ——
# 每天同步都会毁掉前一交易日的 PB。keep_non_null 让 NULL 不再覆盖旧值，即可保住历史 PB。
#
# quote：统一行情接口的「量比」只在交易日当天返回，隔日再查同一交易日会给 --。
# 于是**非交易日/次日复跑同一 trade_date 时，upsert 会把已落库的 vol_ratio 擦成 NULL**
# （2026-10-01 国庆休市复跑，18 行 09-30 的 vol_ratio 被擦）。同 PK 为 (trade_date, code)，
# keep_non_null 只在「同日重复写入」时生效，不会把旧交易日的值带到新交易日，安全。
KEEP_NON_NULL_TABLES = {"sector_flow", "valuation", "quote"}


# ----------------------------------------------------------------------------
# 任务定义
# ----------------------------------------------------------------------------

def build_jobs():
    jobs = []

    # --- 市场层 ---
    idx_names = "、".join(n for _, n in INDEX_CODES)
    jobs.append(
        {
            "id": "indices",
            "desc": "八大指数收盘行情",
            "query": f"{idx_names} 今日收盘点位、涨跌幅、成交额、成交量、换手率",
            "alt": [
                f"{idx_names} 最新指数行情",
                "今日A股主要指数收盘表现",
            ],
            "handlers": {"统一行情查询": h_index_quote},
            "critical": True,
        }
    )
    jobs.append(
        {
            "id": "breadth",
            "desc": "两市成交统计（市场宽度）",
            "query": "今日A股两市成交量、成交额和较昨日成交额变化",
            "alt": [
                "今日沪深两市总成交额和成交量",
                "今日A股市场成交统计与涨跌分布",
            ],
            "handlers": {},
            "critical": True,
        }
    )
    jobs.append(
        {
            "id": "sectors",
            "desc": "申万行业板块主力资金排行（接口返回前3）",
            "query": "今日申万一级行业板块主力资金净流入排行",
            "alt": [
                "今日申万一级行业主力资金净流入排名",
                "今日行业板块资金流向排行",
            ],
            "handlers": {},
            "critical": True,
        }
    )
    jobs.append(
        {
            "id": "sectors_gain",
            "desc": "板块涨幅排行（接口返回前6）",
            "query": "今日A股板块涨幅排行榜",
            "alt": [
                "今日行业板块涨幅排名",
                "今日申万一级行业涨跌幅排行",
            ],
            "handlers": {},
            "critical": False,
        }
    )
    jobs.append(
        {
            "id": "sectors_lose",
            "desc": "板块主力净流出排行（接口返回前3）",
            "query": "今日A股板块主力资金净流出排行",
            "alt": [
                "今日行业板块主力资金净流出排名",
                "今日板块资金流出排行",
            ],
            "handlers": {},
            "critical": False,
        }
    )
    jobs.append(
        {
            "id": "leaderboard",
            "desc": "A股个股交易排行榜（全市场维度）",
            "query": "今日A股个股交易排行榜前30名",
            "alt": [
                "今日A股成交额排行前30",
                "今日A股个股交易排名",
            ],
            "handlers": {},
            "critical": False,
        }
    )
    jobs.append(
        {
            "id": "index_valuation",
            "desc": "核心指数估值时序",
            "query": "沪深300指数最近的滚动市盈率和市净率估值",
            "alt": [
                "沪深300指数估值水平和市盈率",
                "沪深300 指数PE PB 估值",
            ],
            "handlers": {},
            "code": "000300.SH",
            "name": "沪深300",
            "critical": False,
        }
    )

    # --- 个股层 ---
    for code, name in FOCUS_POOL:
        A = stock_alts(code, name)
        jobs.append(
            {
                "id": f"quote_flow::{code}",
                "desc": f"{name} 行情与资金流",
                "query": f"{name}{code} 今日最新价、涨跌幅、成交额、换手率、主力资金净流入",
                "alt": A["quote_flow"],
                "handlers": {"统一行情查询": h_stock_quote},
                "code": code,
                "name": name,
                "critical": False,
            }
        )
        jobs.append(
            {
                "id": f"valuation::{code}",
                "desc": f"{name} 估值时序",
                "query": f"{name}{code} 最近的市盈率估值历史",
                "alt": A["valuation"],
                "handlers": {},
                "code": code,
                "name": name,
                "critical": False,
            }
        )
        jobs.append(
            {
                "id": f"financial::{code}",
                "desc": f"{name} 财务（营收/利润/每股净资产）",
                "query": f"{name}{code} 最近三期营业总收入、归母净利润、每股净资产、净资产收益率",
                "alt": A["financial"],
                "handlers": {},
                "code": code,
                "name": name,
                "critical": False,
                "weekly": True,
            }
        )
        jobs.append(
            {
                "id": f"dividend::{code}",
                "desc": f"{name} 分红派息（用于推算股息率）",
                "query": f"{name}{code} 最近的分红派息方案和每10股派息金额",
                "alt": A["dividend"],
                "handlers": {},
                "code": code,
                "name": name,
                "critical": False,
                "post": "dividend_yield",
            }
        )
    return jobs


def query_one_job(code, name):
    """单个标的的按需补齐任务（行情+资金流 / 估值 / 财务）。

    供服务层在新增自选股时调用 —— 用户加一只，就实时把这只补齐。
    """
    A = stock_alts(code, name)
    return [
        {
            "id": f"quote_flow::{code}",
            "desc": f"{name} 行情与资金流",
            "query": f"{name}{code} 今日最新价、涨跌幅、成交额、换手率、主力资金净流入",
            "alt": A["quote_flow"],
            "handlers": {"统一行情查询": h_stock_quote},
            "code": code,
            "name": name,
        },
        {
            "id": f"valuation::{code}",
            "desc": f"{name} 估值时序",
            "query": f"{name}{code} 最近的市盈率估值历史",
            "alt": A["valuation"],
            "handlers": {},
            "code": code,
            "name": name,
        },
        {
            "id": f"financial::{code}",
            "desc": f"{name} 财务",
            "query": f"{name}{code} 最近三期营业总收入、归母净利润、每股净资产、净资产收益率",
            "alt": A["financial"],
            "handlers": {},
            "code": code,
            "name": name,
        },
        {
            "id": f"dividend::{code}",
            "desc": f"{name} 分红派息",
            "query": f"{name}{code} 最近的分红派息方案和每10股派息金额",
            "alt": A["dividend"],
            "handlers": {},
            "code": code,
            "name": name,
            "post": "dividend_yield",
        },
    ]


def stock_alts(code, name):
    """个股类任务的备用问法。

    上游是按自然语言检索的，同一意图换个措辞命中率不同。实测 18 轮同步里有
    3 轮出现「某任务空召回」，当天那块数据就缺了 —— 这里给每类查询配好退路。
    """
    return {
        "quote_flow": [
            f"{name}（{code}）今日收盘行情和主力资金流向",
            f"{code} 最新价 涨跌幅 成交额 换手率",
        ],
        "valuation": [
            f"{name}{code} 市盈率历史估值区间",
            f"{name} 历史市盈率PE数据",
        ],
        "financial": [
            f"{name}{code} 最新财报主要财务指标",
            f"{name} 营业总收入 净利润 净资产收益率",
        ],
        "dividend": [
            f"{name}{code} 历史分红派息记录",
            f"{name} 历次分红方案每10股派息",
        ],
    }


# ----------------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------------

def run_job(conn, job, ctx_base, job_date):
    ctx = dict(ctx_base)
    ctx["trade_date"] = job_date
    ctx["code"] = job.get("code")
    ctx["name"] = job.get("name")

    # 空召回自动换问法重试。
    # 上游是按自然语言检索的，同一意图换个措辞命中率不同；实测 18 轮里有 3 轮
    # 出现「某任务空召回」导致当天该块数据缺失。这里把备用问法依次试一遍。
    queries = [job["query"]] + list(job.get("alt") or [])
    blocks, used, last_err = [], job["query"], None
    for q in queries:
        try:
            resp = nd.query(q)
        except nd.TokenError:
            raise
        except Exception as e:      # 网络类异常：记下，继续试下一种问法
            last_err = e
            continue
        got = nd.recall_blocks(resp, q)
        if got:
            blocks, used = got, q
            break
    if not blocks:
        if last_err is not None:
            raise last_err
        return 0, f"空召回（{len(queries)} 种问法均未命中接口）"
    if used != job["query"]:
        # 用实际命中的问法记录血缘，否则溯源会指到一个查不到数据的 query
        print(f"      ↳ 备用问法命中：{used}")
        job = dict(job, query=used)

    written = 0
    handlers = job.get("handlers") or {}
    for b in blocks:
        # 原始留档
        conn.execute(
            "INSERT INTO raw_recall(trade_date, query, api_tag, api_type, content, fetched_at)"
            " VALUES(?,?,?,?,?,?)",
            (job_date, job["query"], b.tag, b.type, b.content, ctx["fetched_at"]),
        )
        conn.execute(
            "INSERT INTO data_source(table_name, trade_date, code, query, api_tag, api_type, fetched_at)"
            " VALUES(?,?,?,?,?,?,?)",
            ("*", job_date, ctx.get("code"), job["query"], b.tag, b.type, ctx["fetched_at"]),
        )
        fn = (job.get("handlers") or {}).get(b.type) or handlers.get(b.type) or HANDLERS.get(b.type)
        if not fn:
            continue
        try:
            table, rows, keys = fn(ctx, b)
        except Exception as e:
            conn.execute(
                "INSERT INTO data_source(table_name, trade_date, code, query, api_tag, api_type, fetched_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (f"!parse_fail:{e.__class__.__name__}", job_date, ctx.get("code"), job["query"], b.tag, b.type, ctx["fetched_at"]),
            )
            continue
        # 只保留主键完整的行
        rows = [r for r in rows if all(r.get(k) for k in keys)]
        if rows:
            written += db.upsert(
                conn, table, rows, keys, keep_non_null=(table in KEEP_NON_NULL_TABLES)
            )

    # 排行榜携带的市值 → market_cap
    for ex in ctx.get("pending_extra", []):
        db.upsert(conn, "market_cap", [ex], ["trade_date", "code"])

    # 板块「涨股比」汇总 → 涨跌家数（**兜底路径**）
    # 上游「大盘市场宽度统计」正文现已直供全市场涨跌家数（见 h_breadth），那是首选口径。
    # 这里仅在直供缺失时兜底，且必须满足覆盖率门槛：排行接口每次只返回前几条、覆盖不到全市场，
    # 覆盖率不足时宁可不给，也不要用几个行业去冒充全市场家数 —— 那比缺数据更糟。
    acc = ctx.get("breadth_acc") or []
    ups = [u for u, _ in acc if u is not None]
    tots = [t for _, t in acc if t is not None]
    direct = conn.execute(
        "SELECT COUNT(*) AS c FROM breadth WHERE trade_date=? AND up_count IS NOT NULL",
        (job_date,),
    ).fetchone()
    has_direct = bool(direct and direct["c"])
    if not has_direct and len(acc) >= MIN_SECTORS_FOR_BREADTH and ups and tots and len(ups) == len(tots):
        total = sum(tots)
        up = sum(ups)
        conn.execute(
            "INSERT INTO breadth(trade_date, market, up_count, down_count, flat_count,"
            " fetched_at, raw) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(trade_date, market) DO UPDATE SET"
            " up_count=excluded.up_count, down_count=excluded.down_count,"
            " flat_count=excluded.flat_count, raw=excluded.raw",
            (
                job_date,
                "两市汇总（涨跌家数）",
                up,
                max(total - up, 0),
                0,
                ctx["fetched_at"],
                db.json_dumps({"derived": "申万行业涨股比求和", "sectors": len(acc), "total": total, "up": up}),
            ),
        )
        written += 1

    # 每股净资产 → 推算 PB 写回估值
    bps = ctx.get("bps") or {}
    if bps:
        latest = sorted(bps.keys())[-1]
        q = conn.execute(
            "SELECT close FROM quote WHERE code=? AND kind='stock' ORDER BY trade_date DESC LIMIT 1",
            (ctx.get("code"),),
        ).fetchone()
        if q and q["close"] and bps[latest]:
            pb = round(q["close"] / bps[latest], 4)
            # 只回填最新估值日：每股净资产是「最新报告期」的口径，
            # 若不加日期限制，会拿最新收盘价乘出来的 PB 覆盖全部历史行 —— 历史估值就被写坏了。
            conn.execute(
                "UPDATE valuation SET pb=? WHERE code=? AND pb IS NULL"
                " AND date=(SELECT MAX(date) FROM valuation WHERE code=?)",
                (pb, ctx.get("code"), ctx.get("code")),
            )

    # 后置计算：TTM 股息率。
    # 估值序列一旦更新，股息率就必须跟着重算 —— 否则单独跑 `--only valuation`
    # 会往估值表末尾插入更晚的日期，股息率却留在旧行上，最新一行反而显示「—」。
    if ctx.get("code") and (job.get("post") == "dividend_yield"
                            or (job.get("id") or "").startswith("valuation::")):
        dy = compute_dividend_yield(conn, ctx["code"])
        if dy is not None:
            written += 1

    if written == 0:
        # 召回拿到了内容，但没有任何 handler 命中 / 没有主键完整的行。
        # 静默算成功会让 ok_count 虚高 ——「31/31 成功」其实一个数都没写进去。
        tags = "、".join(sorted({b.type for b in blocks if getattr(b, "type", None)}))
        return 0, f"召回成功但无有效数据行（api_type: {tags or '未知'}）"

    return written, None


def resolve_latest_trade_date():
    """问上游要「最新交易日」，用作不加 --date 时的默认值。

    为什么不信库内最新日：库里已有旧数据时，默认值会永远回落到那个旧日，
    于是每日自动同步只会一遍遍重写旧交易日（并把旧日历史行覆盖成当天数值）。
    为什么不信本地今天：休市日（周末/节假日）上游仍返回上一交易日的数据，
    用今天当 trade_date 会把上一交易日的行情标成今天 —— 同样是写坏数据。

    上证指数的统一行情查询返回里带「日期」和「交易状态」，
    取到的日期天然就是最新交易日，两个坑一起绕开。取不到时返回 None，由调用方兜底。
    """
    q = "上证指数最新点位"
    try:
        resp = nd.query(q)
        for b in nd.recall_blocks(resp, q):
            for _code, _name, t in _entity_tables(b.content):
                for r in t["rows"]:
                    d = nd.norm_date(nd.cell(r, t["header"], "日期"))
                    if not d:
                        continue
                    # 休市日（周末/法定节假日）上游仍会补齐一行「未开盘」占位行：
                    # 日期是当天、但各价量全为 --。它不是交易日，若直接采信，
                    # trade_date 会落到休市日，上一交易日的数值就会被标成当天。
                    # 判据：没有收盘/最新价的行一律不算交易日。
                    if nd.num_cell(r, t["header"], "收盘/最新", "收盘", "最新") is None:
                        continue
                    return d
    except Exception:
        return None
    return None


def financial_is_fresh(conn, code):
    r = conn.execute(
        "SELECT fetched_at FROM financial WHERE code=? ORDER BY fetched_at DESC LIMIT 1", (code,)
    ).fetchone()
    if not r or not r["fetched_at"]:
        return False
    try:
        t = datetime.strptime(r["fetched_at"][:10], "%Y-%m-%d")
    except ValueError:
        return False
    return (datetime.now() - t) < timedelta(days=FINANCIAL_FRESH_DAYS)


def main():
    ap = argparse.ArgumentParser(description="锐思投研数据同步")
    ap.add_argument("--date", default=None, help="交易日 YYYY-MM-DD，默认取库内最新或今天")
    ap.add_argument("--only", default=None, help="只跑指定任务 id（逗号分隔，支持前缀）")
    ap.add_argument("--with-financial", action="store_true", help="强制重取财务数据")
    ap.add_argument("--list", action="store_true", help="列出全部任务")
    ap.add_argument("--stop-after-fail", type=int, default=8, help="连续失败多少次后中止")
    args = ap.parse_args()

    jobs = build_jobs()
    if args.list:
        for j in jobs:
            print(f"  {j['id']:<28} {j['desc']}")
        print(f"\n共 {len(jobs)} 个任务")
        return 0

    if args.only:
        pats = [p.strip() for p in args.only.split(",") if p.strip()]
        jobs = [j for j in jobs if any(j["id"].startswith(p) for p in pats)]
        if not jobs:
            print("没有匹配的任务")
            return 1

    with db.db() as conn:
        # 默认取上游最新交易日；上游不可用时才退回库内最新日/今天
        trade_date = args.date or resolve_latest_trade_date() or db.latest_trade_date(conn) or nd.today()
        started = db.now_iso()
        cur = conn.execute(
            "INSERT INTO sync_run(trade_date, started_at, status) VALUES(?,?,'running')",
            (trade_date, started),
        )
        run_id = cur.lastrowid
        conn.commit()

        print(f"=== 同步交易日 {trade_date} · {len(jobs)} 个任务 ===")
        ctx_base = {"fetched_at": db.now_iso(), "trade_date": trade_date}
        ok = fail = skip = 0
        consec = 0
        notes = []
        t0 = time.time()
        for i, job in enumerate(jobs, 1):
            if job.get("weekly") and not args.with_financial:
                code = job.get("code")
                if code and financial_is_fresh(conn, code):
                    skip += 1
                    print(f"[{i:>2}/{len(jobs)}] {job['id']:<28} 跳过（财务 7 天内已同步）")
                    continue
            try:
                n, err = run_job(conn, job, ctx_base, trade_date)
                conn.commit()
                if err:
                    fail += 1
                    consec += 1
                    notes.append(f"{job['id']}: {err}")
                    print(f"[{i:>2}/{len(jobs)}] {job['id']:<28} 空召回")
                else:
                    ok += 1
                    consec = 0
                    print(f"[{i:>2}/{len(jobs)}] {job['id']:<28} 写入 {n} 行")
            except nd.TokenError as e:
                db.upsert(
                    conn,
                    "sync_run",
                    [{"id": run_id, "status": "failed", "note": f"凭证失效：{e}"}],
                    ["id"],
                )
                conn.commit()
                print(f"\n!! 凭证失效，需重新获取后再同步：{e}")
                return 2
            except Exception as e:
                fail += 1
                consec += 1
                notes.append(f"{job['id']}: {e.__class__.__name__}: {e}")
                print(f"[{i:>2}/{len(jobs)}] {job['id']:<28} 失败 {e.__class__.__name__}: {str(e)[:90]}")
                if consec >= args.stop_after_fail:
                    print(f"\n!! 连续失败 {consec} 次，中止同步（疑似网络或服务问题）")
                    break

        status = "ok" if fail == 0 else ("partial" if ok else "failed")
        # --only 属于局部补跑，ok_count 只代表被选中的那几个任务，不代表整轮同步。
        # 用 scope= 前缀标记，供 server.build_meta 区分「完整同步」与「局部补跑」，
        # 否则页面上的「同步状态」会被最后一次补跑的小数字覆盖。
        note = "; ".join(notes[:20])
        if args.only:
            note = f"scope={args.only}" + (f"; {note}" if note else "")
        conn.execute(
            "UPDATE sync_run SET finished_at=?, status=?, ok_count=?, fail_count=?, note=? WHERE id=?",
            (db.now_iso(), status, ok, fail, note, run_id),
        )
        conn.commit()

        print(
            f"\n=== 完成 status={status} 成功 {ok} / 失败 {fail} / 跳过 {skip} "
            f"耗时 {time.time()-t0:.1f}s ==="
        )
        for t in ("quote", "valuation", "fund_flow", "sector_flow", "breadth", "financial", "market_cap"):
            c = conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            print(f"    {t:<14} {c} 行")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
