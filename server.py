#!/usr/bin/env python3
"""锐思投研 · 本地服务

零依赖（Python 标准库）。职责：
  1. 静态托管 web/ 前端
  2. 提供 /api/* 只读数据接口（读 SQLite）
  3. 提供自选股 / 持仓的读写接口（用户真实数据，落本机）
  4. 按需为新增自选股实时补齐数据

只监听 127.0.0.1 —— 数据留在本机，不对外暴露。
"""

import json
import re
import threading
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db
import neodata as nd
from sync import FOCUS_POOL, INDEX_CODES

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

# 重点研究池：这些标的取过完整数据（估值/资金流/财务/分红）。
# 榜单里顺手抓进来的标的只有行情，界面上必须区分，否则用户点开会以为平台坏了。
FOCUS_SET = {c for c, _ in FOCUS_POOL}
INDEX_SET = {c for c, _ in INDEX_CODES}

HOST = "127.0.0.1"
PORT = 8770

_fetch_lock = threading.Lock()


# ---------------------------------------------------------------- helpers

def jsonable(v):
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if hasattr(v, "keys"):
        return {k: jsonable(v[k]) for k in v.keys()}
    return v


def pct_rank(values, v):
    """v 在 values 中的分位（0-100）。values 为空或 v 为 None 时返回 None。"""
    vals = sorted(x for x in values if x is not None)
    if not vals or v is None:
        return None
    below = sum(1 for x in vals if x <= v)
    return round(100.0 * below / len(vals), 1)


def rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def one(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def latest_date(conn):
    r = one(conn, "SELECT MAX(trade_date) AS d FROM quote")
    return (r or {}).get("d")


# ---------------------------------------------------------------- builders

def build_meta(conn):
    d = latest_date(conn)
    # 优先取「完整同步」记录：--only 局部补跑会被打上 scope= 前缀，
    # 其 ok_count 只覆盖少数任务，不能代表整轮同步（否则页面状态会被小数字覆盖）。
    run = one(
        conn,
        "SELECT * FROM sync_run WHERE trade_date=? AND IFNULL(note,'') NOT LIKE 'scope=%'"
        " ORDER BY id DESC LIMIT 1",
        (d,),
    ) or one(
        conn,
        "SELECT * FROM sync_run WHERE trade_date=? ORDER BY id DESC LIMIT 1",
        (d,),
    ) or {}
    counts = {}
    for t in ("quote", "valuation", "fund_flow", "sector_flow", "breadth", "financial", "market_cap", "raw_recall"):
        counts[t] = one(conn, f"SELECT COUNT(*) c FROM {t}")["c"]
    avail = one(
        conn,
        "SELECT MIN(fetched_at) a, MAX(fetched_at) b FROM raw_recall WHERE trade_date=?",
        (d,),
    ) or {}
    return {
        "trade_date": d,
        "generated_at": db.now_iso(),
        "sync": {
            "status": run.get("status"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "ok_count": run.get("ok_count"),
            "fail_count": run.get("fail_count"),
        },
        "row_counts": counts,
        "fetched_between": [avail.get("a"), avail.get("b")],
        "source": "NeoData（腾讯金融数据服务）",
        "note": (
            "数据为同步时点从 NeoData 拉取的快照。本服务按需/按日同步，"
            "不是逐笔实时推送；页面展示的数值在两次同步之间不会变化。"
        ),
    }


def build_indices(conn, d):
    return rows(
        conn,
        "SELECT code,name,open,high,low,close,prev_close,chg,chg_pct,amount,volume,"
        "turnover,vol_ratio FROM quote WHERE trade_date=? AND kind='index' ORDER BY amount DESC",
        (d,),
    )


def build_breadth(conn, d):
    return rows(conn, "SELECT * FROM breadth WHERE trade_date=?", (d,))


def build_sectors(conn, d):
    return rows(
        conn,
        "SELECT sector_name,sector_code,chg_pct,amount,turnover,up_ratio,main_net_in,"
        "main_net_in_5d,main_net_in_20d,lead_name,lead_code,lead_chg_pct "
        "FROM sector_flow WHERE trade_date=? ORDER BY chg_pct DESC",
        (d,),
    )


def build_stock_list(conn, d):
    """全部有行情的个股，附最新估值与资金流（有则带，无则 null）。"""
    q = rows(
        conn,
        "SELECT code,name,close,chg_pct,amount,turnover,vol_ratio,amplitude "
        "FROM quote WHERE trade_date=? AND kind='stock'",
        (d,),
    )
    mc = {r["code"]: r for r in rows(conn, "SELECT * FROM market_cap WHERE trade_date=?", (d,))}
    val = {}
    for r in rows(
        conn,
        "SELECT code, name, pe_ttm, pe_dynamic, pb, dividend_yield FROM valuation "
        "WHERE date=(SELECT MAX(date) FROM valuation v2 WHERE v2.code=valuation.code)",
    ):
        val[r["code"]] = r
    fl = {
        r["code"]: r
        for r in rows(conn, "SELECT * FROM fund_flow WHERE trade_date=?", (d,))
    }
    out = []
    for r in q:
        c = r["code"]
        m = mc.get(c) or {}
        v = val.get(c) or {}
        f = fl.get(c) or {}
        has_val = v.get("pe_ttm") is not None or v.get("pb") is not None
        has_fin = one(conn, "SELECT 1 x FROM financial WHERE code=? LIMIT 1", (c,)) is not None
        out.append(
            {
                **r,
                "mcap": m.get("mcap"),
                "float_mcap": m.get("float_mcap"),
                "pe_ttm": v.get("pe_ttm"),
                "pe_dynamic": v.get("pe_dynamic"),
                "pb": v.get("pb"),
                "dividend_yield": v.get("dividend_yield"),
                "main_net_in": f.get("main_net_in"),
                "retail_net_in": f.get("retail_net_in"),
                # 数据完整度：区分「重点研究池」与「仅有行情的榜单标的」
                "focus": c in FOCUS_SET,
                "has_valuation": has_val,
                "has_financial": has_fin,
                "depth": ("full" if (has_val and has_fin) else ("quote+valuation" if has_val else "quote_only")),
            }
        )
    # 重点池排前面，让默认选中的标的总是有完整数据
    out.sort(key=lambda s: (not s["focus"], s.get("code") or ""))
    return out


def build_stock_detail(conn, code):
    q = one(conn, "SELECT * FROM quote WHERE code=? AND kind='stock' ORDER BY trade_date DESC LIMIT 1", (code,))
    if not q:
        return None
    name = q["name"]
    val_hist = rows(
        conn,
        "SELECT date,pe_ttm,pe_dynamic,pe_static,pb,dividend_yield FROM valuation "
        "WHERE code=? ORDER BY date",
        (code,),
    )
    pe_series = [v["pe_ttm"] for v in val_hist]
    latest_val = val_hist[-1] if val_hist else {}
    flow_hist = rows(
        conn,
        "SELECT trade_date,main_net_in,main_in,main_out,retail_net_in FROM fund_flow "
        "WHERE code=? ORDER BY trade_date DESC LIMIT 20",
        (code,),
    )
    fin = rows(
        conn,
        "SELECT report_period,publish_date,revenue,net_profit,net_profit_attr,eps "
        "FROM financial WHERE code=? ORDER BY report_period DESC LIMIT 8",
        (code,),
    )
    mc = one(conn, "SELECT * FROM market_cap WHERE code=? ORDER BY trade_date DESC LIMIT 1", (code,)) or {}
    wd = [v["date"] for v in val_hist]
    has_val = latest_val.get("pe_ttm") is not None
    return {
        "code": code,
        "name": name,
        "quote": q,
        "market_cap": mc,
        "focus": code in FOCUS_SET,
        "has_valuation": has_val,
        "has_financial": len(fin) > 0,
        "depth": "full" if (has_val and fin) else ("quote+valuation" if has_val else "quote_only"),
        "valuation": {
            "latest": latest_val,
            "history": val_hist,
            "pe_percentile": pct_rank(pe_series, latest_val.get("pe_ttm")),
            "window": {
                "points": len(val_hist),
                "from": min(wd) if wd else None,
                "to": max(wd) if wd else None,
                "label": f"库内估值序列（{len(val_hist)} 个交易日）" if val_hist else "无可比序列",
            },
        },
        "fund_flow": {"recent": flow_hist},
        "financial": fin,
        "in_watchlist": one(conn, "SELECT 1 x FROM watchlist WHERE code=?", (code,)) is not None,
    }


def build_screener(conn, d, f):
    """选股器：在库内标的上按条件筛选。

    重要：不同字段的覆盖范围不同，返回里必须标明，否则会误导。
      - 行情类指标（涨跌幅/换手/量比/成交额/市值）：覆盖「全池」
      - 估值类指标（PE/PB/股息率）：只覆盖已逐只取过估值的标的
    """
    stocks = build_stock_list(conn, d)
    universe = len(stocks)

    def keep(s):
        if f.get("min_chg") is not None and (s.get("chg_pct") is None or s["chg_pct"] < f["min_chg"]):
            return False
        if f.get("max_chg") is not None and (s.get("chg_pct") is None or s["chg_pct"] > f["max_chg"]):
            return False
        if f.get("min_mcap") is not None and (s.get("mcap") is None or s["mcap"] < f["min_mcap"]):
            return False
        if f.get("min_turnover") is not None and (s.get("turnover") is None or s["turnover"] < f["min_turnover"]):
            return False
        if f.get("max_pe") is not None and (s.get("pe_ttm") is None or s["pe_ttm"] > f["max_pe"]):
            return False
        if f.get("min_pe") is not None and (s.get("pe_ttm") is None or s["pe_ttm"] < f["min_pe"]):
            return False
        if f.get("max_pb") is not None and (s.get("pb") is None or s["pb"] > f["max_pb"]):
            return False
        if f.get("min_dy") is not None and (s.get("dividend_yield") is None or s["dividend_yield"] < f["min_dy"]):
            return False
        if f.get("require_valuation") and s.get("pe_ttm") is None:
            return False
        return True

    sel = [s for s in stocks if keep(s)]
    sort = f.get("sort") or "chg_pct"
    desc = f.get("desc", True)
    if sort in ("chg_pct", "turnover", "vol_ratio", "amount", "mcap", "pe_ttm", "pb", "dividend_yield"):
        sel.sort(key=lambda s: (s.get(sort) is None, -(s.get(sort) or 0) if desc else (s.get(sort) or 0)))

    with_val = sum(1 for s in stocks if s.get("pe_ttm") is not None)
    return {
        "universe": universe,
        "selected": len(sel),
        "coverage": {
            "quote_fields": {"available": universe, "total": universe},
            "valuation_fields": {"available": with_val, "total": universe},
            "note": (
                f"行情类指标覆盖全部 {universe} 只；估值类指标（PE/PB/股息率）"
                f"当前仅覆盖 {with_val} 只 —— 接口为单标的粒度，无法批量取全市场估值。"
                "用「按需补齐」可以把你关心的标的补进来。"
            ),
        },
        "items": sel,
    }


def build_portfolio(conn, d):
    """持仓：用户真实录入，盈亏用库内最新收盘价算。"""
    pos = rows(conn, "SELECT * FROM portfolio")
    out = []
    total_mv = 0.0
    for p in pos:
        q = one(
            conn,
            "SELECT close, chg_pct, name FROM quote WHERE code=? AND kind='stock' "
            "ORDER BY trade_date DESC LIMIT 1",
            (p["code"],),
        ) or {}
        px = q.get("close")
        mv = (px or 0) * (p["shares"] or 0)
        cost = (p.get("cost") or 0) * (p["shares"] or 0)
        total_mv += mv
        v = one(
            conn,
            "SELECT pe_ttm,pb,dividend_yield FROM valuation WHERE code=? ORDER BY date DESC LIMIT 1",
            (p["code"],),
        ) or {}
        out.append(
            {
                **p,
                "current_price": px,
                "chg_pct": q.get("chg_pct"),
                "market_value": round(mv, 2),
                "cost_value": round(cost, 2) if p.get("cost") else None,
                "pnl": round(mv - cost, 2) if p.get("cost") else None,
                "pnl_pct": round((mv / cost - 1) * 100, 2) if p.get("cost") and cost else None,
                "pe_ttm": v.get("pe_ttm"),
                "pb": v.get("pb"),
                "dividend_yield": v.get("dividend_yield"),
            }
        )
    for it in out:
        it["weight_pct"] = round(100.0 * it["market_value"] / total_mv, 2) if total_mv else None
    return {"positions": out, "total_market_value": round(total_mv, 2), "count": len(out)}


def build_watchlist(conn, d):
    wl = rows(conn, "SELECT * FROM watchlist ORDER BY added_at DESC")
    for w in wl:
        q = one(
            conn,
            "SELECT close,chg_pct,turnover FROM quote WHERE code=? AND kind='stock' "
            "ORDER BY trade_date DESC LIMIT 1",
            (w["code"],),
        ) or {}
        v = one(
            conn,
            "SELECT pe_ttm,pb,dividend_yield FROM valuation WHERE code=? ORDER BY date DESC LIMIT 1",
            (w["code"],),
        ) or {}
        w.update({"close": q.get("close"), "chg_pct": q.get("chg_pct"), "turnover": q.get("turnover"), **v})
    return wl


# ---------------------------------------------------------------- on-demand fetch

def resolve_name(code, fallback=None):
    """向数据源确认标的的权威名称。

    调用方传来的 name 不可信：可能是个占位符，也可能打错字。它会被写进行情/估值表，
    之后整站都显示错名字 —— 所以以数据源返回的 entity 为准，取不到才退回调用方的值。

    匹配规则（实测得出）：
      1. entity.code 与标的代码完全相同 → 直接采用
      2. 没有精确命中、但只返回了一个 entity → 采用它的名字。上游的自然语言检索
         有时会把 A 股匹配成同名的港股（如 601398.SH → 01398.HK），但中文名一致，
         此时唯一命中反而可信；返回多个 entity 说明有歧义，就不猜了。
    """
    for q in (f"{code} 今日最新价", f"{code} 最新行情", f"{code} 今日收盘价"):
        try:
            es = [e for e in nd.entities(nd.query(q)) if e.get("name")]
        except Exception:
            continue
        for e in es:
            if str(e.get("code", "")).upper() == code.upper():
                return str(e["name"]).strip()
        if len(es) == 1:
            return str(es[0]["name"]).strip()
    return fallback or code


def backfill_display_name(conn, table, code):
    """把「名称就是代码」这种占位回填成权威名称。

    调用方经常只给 code（前端点「加自选」就是如此），此时表里会存成代码本身，
    界面上显示的就是「601398.SH」而不是「工商银行」。库里已有名称就直接用，
    没有才向数据源确认一次 —— 这一步不能省，否则整站都会显示错名字。
    """
    cur = one(conn, f"SELECT name FROM {table} WHERE code=?", (code,))
    if not cur or (cur["name"] and cur["name"] != code):
        return None
    r = one(conn, "SELECT name FROM quote WHERE code=? AND name IS NOT NULL AND name<>''"
                  " ORDER BY trade_date DESC LIMIT 1", (code,))
    nm = r["name"] if r and r["name"] and r["name"] != code else resolve_name(code)
    if nm and nm != code:
        conn.execute(f"UPDATE {table} SET name=? WHERE code=?", (nm, code))
        conn.commit()
        return nm
    return None


def fetch_instrument(code, name=None):
    """为一个标的按需补齐行情/估值/财务。供自选股新增时调用。"""
    import sync as sync_mod

    with _fetch_lock:
        with db.db() as conn:
            name = resolve_name(code, name)
            d = db.latest_trade_date(conn) or None
            ctx_base = {"fetched_at": db.now_iso(), "trade_date": d}
            jobs = []
            r = sync_mod.query_one_job(code, name)
            jobs.extend(r)
            results = []
            for job in jobs:
                try:
                    n, err = sync_mod.run_job(conn, job, ctx_base, d or db.now_iso()[:10])
                    conn.commit()
                    results.append({"job": job["id"], "written": n, "error": err})
                except Exception as e:
                    results.append({"job": job["id"], "error": f"{e.__class__.__name__}: {e}"})
            return results


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "RuisiResearch/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(jsonable(body), ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _err(self, code, msg, detail=None):
        self._send(code, {"ok": False, "error": msg, "detail": detail})

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if path.startswith("/api/"):
                return self.api_get(path, qs)
            return self.static(path)
        except Exception as e:
            traceback.print_exc()
            return self._err(500, str(e), traceback.format_exc()[-800:])

    def api_get(self, path, qs):
        with db.db() as conn:
            d = latest_date(conn)
            if path == "/api/meta":
                return self._send(200, build_meta(conn))
            if d is None:
                return self._send(200, {"empty": True, "hint": "库内无数据，请先执行 python3 sync.py"})
            if path == "/api/summary":
                return self._send(
                    200,
                    {
                        "trade_date": d,
                        "indices": build_indices(conn, d),
                        "breadth": build_breadth(conn, d),
                        "sectors": build_sectors(conn, d),
                        "stocks": build_stock_list(conn, d),
                    },
                )
            if path == "/api/indices":
                return self._send(200, {"trade_date": d, "items": build_indices(conn, d)})
            if path == "/api/sectors":
                return self._send(200, {"trade_date": d, "items": build_sectors(conn, d)})
            if path == "/api/stocks":
                return self._send(200, {"trade_date": d, "items": build_stock_list(conn, d)})
            if path == "/api/stock":
                code = qs.get("code")
                if not code:
                    return self._err(400, "缺少 code 参数")
                det = build_stock_detail(conn, code)
                if not det:
                    return self._err(404, f"库内没有 {code} 的数据")
                return self._send(200, det)
            if path == "/api/screener":
                def fnum(k):
                    v = qs.get(k)
                    if v in (None, "", "null"):
                        return None
                    try:
                        return float(v)
                    except ValueError:
                        return None

                f = {
                    "min_chg": fnum("min_chg"),
                    "max_chg": fnum("max_chg"),
                    "min_mcap": fnum("min_mcap"),
                    "min_turnover": fnum("min_turnover"),
                    "min_pe": fnum("min_pe"),
                    "max_pe": fnum("max_pe"),
                    "max_pb": fnum("max_pb"),
                    "min_dy": fnum("min_dy"),
                    "require_valuation": qs.get("require_valuation") == "1",
                    "sort": qs.get("sort") or "chg_pct",
                    "desc": qs.get("desc", "1") != "0",
                }
                return self._send(200, {"trade_date": d, **build_screener(conn, d, f)})
            if path == "/api/watchlist":
                return self._send(200, {"trade_date": d, "items": build_watchlist(conn, d)})
            if path == "/api/portfolio":
                return self._send(200, {"trade_date": d, **build_portfolio(conn, d)})
            if path == "/api/raw_recall":
                q = qs.get("query")
                sql = "SELECT id,trade_date,query,api_tag,api_type,LENGTH(content) len FROM raw_recall"
                args = ()
                if q:
                    sql += " WHERE query LIKE ?"
                    args = (f"%{q}%",)
                sql += " ORDER BY id DESC LIMIT 100"
                return self._send(200, {"items": rows(conn, sql, args)})
            return self._err(404, f"未知接口 {path}")

    def do_POST(self):
        u = urlparse(self.path)
        ln_raw = self.headers.get("Content-Length")
        if ln_raw is None:
            # 浏览器 fetch 一定会带 Content-Length；缺了就明确报错，
            # 免得静默当成空 body 解析出一个看不懂的 400。
            te = (self.headers.get("Transfer-Encoding") or "").lower()
            return self._err(
                411,
                "缺少 Content-Length，无法读取请求体"
                + ("（收到 Transfer-Encoding: chunked，本服务不解析分块编码）" if "chunked" in te else ""),
            )
        try:
            ln = int(ln_raw)
            raw = self.rfile.read(ln) if ln else b"{}"
            body = json.loads(raw or b"{}")
        except Exception as e:
            return self._err(400, f"请求体解析失败：{e}")
        try:
            if u.path == "/api/watchlist":
                return self._post_watchlist(body)
            if u.path == "/api/portfolio":
                return self._post_portfolio(body)
            if u.path == "/api/fetch":
                code = (body.get("code") or "").strip()
                if not re.match(r"^\d{6}\.(SH|SZ|BJ)$", code):
                    return self._err(400, "code 需形如 600519.SH")
                res = fetch_instrument(code, body.get("name"))
                return self._send(200, {"ok": True, "results": res})
            return self._err(404, f"未知接口 {u.path}")
        except Exception as e:
            traceback.print_exc()
            return self._err(500, str(e))

    def _post_watchlist(self, body):
        action = body.get("action") or "add"
        code = (body.get("code") or "").strip().upper()
        if not re.match(r"^\d{6}\.(SH|SZ|BJ)$", code):
            return self._err(400, "code 需形如 600519.SH")
        with db.db() as conn:
            if action == "remove":
                conn.execute("DELETE FROM watchlist WHERE code=?", (code,))
                conn.commit()
                return self._send(200, {"ok": True, "removed": code})
            name = (body.get("name") or "").strip() or code
            conn.execute(
                "INSERT INTO watchlist(code,name,note,added_at) VALUES(?,?,?,?)"
                " ON CONFLICT(code) DO UPDATE SET name=excluded.name, note=excluded.note",
                (code, name, body.get("note"), db.now_iso()),
            )
            conn.commit()
            have = one(conn, "SELECT 1 x FROM quote WHERE code=?", (code,))
        fetched = None
        if not have:
            try:
                fetched = fetch_instrument(code, name)
            except Exception as e:
                fetched = [{"error": str(e)}]
        # 抓取过程会向数据源确认权威名称，回写到自选表
        with db.db() as conn:
            backfill_display_name(conn, "watchlist", code)
        return self._send(200, {"ok": True, "added": code, "fetched": fetched})

    def _post_portfolio(self, body):
        action = body.get("action") or "upsert"
        code = (body.get("code") or "").strip().upper()
        if action == "remove":
            with db.db() as conn:
                conn.execute("DELETE FROM portfolio WHERE code=?", (code,))
                conn.commit()
            return self._send(200, {"ok": True, "removed": code})
        if not re.match(r"^\d{6}\.(SH|SZ|BJ)$", code):
            return self._err(400, "code 需形如 600519.SH")
        try:
            shares = float(body.get("shares") or 0)
            cost = float(body["cost"]) if body.get("cost") not in (None, "") else None
        except ValueError:
            return self._err(400, "shares / cost 需为数字")
        if shares <= 0:
            return self._err(400, "shares 必须大于 0")
        name = (body.get("name") or "").strip() or code
        with db.db() as conn:
            conn.execute(
                "INSERT INTO portfolio(code,name,shares,cost,updated_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(code) DO UPDATE SET name=excluded.name, shares=excluded.shares,"
                " cost=excluded.cost, updated_at=excluded.updated_at",
                (code, name, shares, cost, db.now_iso()),
            )
            conn.commit()
            have = one(conn, "SELECT 1 x FROM quote WHERE code=?", (code,))
        if not have:
            try:
                fetch_instrument(code, name)
            except Exception:
                pass
        # 同自选股：别让持仓表里存的是代码
        with db.db() as conn:
            backfill_display_name(conn, "portfolio", code)
        return self._send(200, {"ok": True, "saved": code})

    def static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        fp = (WEB_DIR / path.lstrip("/")).resolve()
        if not str(fp).startswith(str(WEB_DIR.resolve())) or not fp.is_file():
            return self._err(404, "not found")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(fp.suffix, "application/octet-stream")
        return self._send(200, fp.read_bytes(), ctype)


def serve(port=PORT):
    with db.db() as conn:
        d = latest_date(conn)
    srv = ThreadingHTTPServer((HOST, port), Handler)
    print(f"锐思投研 · 本地服务已启动")
    print(f"  地址：http://{HOST}:{port}")
    print(f"  数据时点：{d or '（库内暂无数据，请先运行 python3 sync.py）'}")
    print(f"  数据目录：{db.DB_PATH}")
    print("  按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    serve(a.port)
