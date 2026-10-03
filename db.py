#!/usr/bin/env python3
"""锐思投研 · 数据层

SQLite 库结构与访问helper。全 stdlib，无第三方依赖。

设计原则：
1. 核心字段做规范化列（便于 SQL 查询与聚合），同时保留 raw JSON
   —— NeoData 的表头可能随版本变化，raw 保证不丢信息，列保证可查。
2. 日频幂等：所有事实表以 (日期, 标的) 为主键，重复同步只覆盖不新增。
3. 每个数字都能追溯：raw_recall 留档原始返回，data_source 记录字段来源。
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "market.db"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 同步批次：每次同步一条，用于审计与失败排查
CREATE TABLE IF NOT EXISTS sync_run (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_date   TEXT NOT NULL,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  status       TEXT NOT NULL,          -- running | ok | partial | failed
  ok_count     INTEGER NOT NULL DEFAULT 0,
  fail_count   INTEGER NOT NULL DEFAULT 0,
  note         TEXT
);

-- 行情：指数 / 个股 / 板块 共用
CREATE TABLE IF NOT EXISTS quote (
  trade_date   TEXT NOT NULL,
  code         TEXT NOT NULL,
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL,          -- index | stock | sector
  open         REAL,
  high         REAL,
  low          REAL,
  close        REAL,
  prev_close   REAL,
  chg          REAL,
  chg_pct      REAL,
  amplitude    REAL,
  volume       REAL,
  amount        REAL,
  turnover     REAL,
  vol_ratio    REAL,
  fetched_at   TEXT,
  PRIMARY KEY (trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_quote_kind ON quote(trade_date, kind);

-- 估值时序：统一估值查询返回的是按日期排列的序列，全部落库以支持分位计算
CREATE TABLE IF NOT EXISTS valuation (
  date              TEXT NOT NULL,     -- 估值日期 YYYY-MM-DD
  code              TEXT NOT NULL,
  name              TEXT,
  pe_ttm            REAL,
  pe_static         REAL,
  pe_dynamic        REAL,
  pb                REAL,
  dividend_yield    REAL,
  raw               TEXT,
  fetched_at        TEXT,
  PRIMARY KEY (date, code)
);
CREATE INDEX IF NOT EXISTS idx_val_code ON valuation(code, date);

-- 资金流向（个股 / 板块）
CREATE TABLE IF NOT EXISTS fund_flow (
  trade_date      TEXT NOT NULL,
  code            TEXT NOT NULL,
  name            TEXT,
  main_net_in     REAL,                -- 主力净流入（元）
  main_in         REAL,
  main_out        REAL,
  retail_net_in   REAL,
  super_net_in    REAL,
  big_net_in      REAL,
  raw             TEXT,
  fetched_at      TEXT,
  PRIMARY KEY (trade_date, code)
);

-- 板块资金排行
CREATE TABLE IF NOT EXISTS sector_flow (
  trade_date      TEXT NOT NULL,
  sector_code     TEXT NOT NULL,
  sector_name     TEXT NOT NULL,
  chg_pct         REAL,
  amount          REAL,
  turnover        REAL,
  up_ratio        REAL,
  main_net_in     REAL,
  main_net_in_5d  REAL,
  main_net_in_20d REAL,
  lead_name       TEXT,
  lead_code       TEXT,
  lead_chg_pct    REAL,
  fetched_at      TEXT,
  PRIMARY KEY (trade_date, sector_code)
);

-- 大盘市场宽度
CREATE TABLE IF NOT EXISTS breadth (
  trade_date       TEXT NOT NULL,
  market           TEXT NOT NULL,
  volume           REAL,
  amount           REAL,
  amount_chg       REAL,
  up_count         INTEGER,
  down_count       INTEGER,
  flat_count       INTEGER,
  limit_up_count   INTEGER,
  limit_down_count INTEGER,
  raw              TEXT,
  fetched_at       TEXT,
  PRIMARY KEY (trade_date, market)
);

-- 财务（利润表等）
CREATE TABLE IF NOT EXISTS financial (
  code            TEXT NOT NULL,
  report_period   TEXT NOT NULL,
  publish_date    TEXT,
  revenue         REAL,                -- 营业总收入
  net_profit      REAL,                -- 净利润
  net_profit_attr REAL,                -- 归母净利润
  eps             REAL,
  raw             TEXT,
  fetched_at      TEXT,
  PRIMARY KEY (code, report_period)
);

-- 分红派息：用来推算 TTM 股息率（估值接口的股息率字段为空，只能自己算）
CREATE TABLE IF NOT EXISTS dividend (
  code           TEXT NOT NULL,
  event_date     TEXT NOT NULL,        -- 事件日期 YYYY-MM-DD
  reg_date       TEXT,                 -- 股权登记日
  ex_date        TEXT,                 -- 除权日
  currency       TEXT,
  per_10_shares  REAL,                 -- 每10股派息（元）
  plan           TEXT,
  fetched_at     TEXT,
  PRIMARY KEY (code, event_date)
);

-- 市值与股本：A股个股交易排行榜是唯一能一次拿到多只股票市值的入口
CREATE TABLE IF NOT EXISTS market_cap (
  trade_date  TEXT NOT NULL,
  code        TEXT NOT NULL,
  name        TEXT,
  mcap        REAL,                    -- 总市值（亿元）
  float_mcap  REAL,                    -- 流通市值（亿元）
  fetched_at  TEXT,
  PRIMARY KEY (trade_date, code)
);

-- 原始返回留档：字段级可追溯
CREATE TABLE IF NOT EXISTS raw_recall (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_date  TEXT,
  query       TEXT NOT NULL,
  api_tag     TEXT,
  api_type    TEXT,
  content     TEXT NOT NULL,
  fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_date ON raw_recall(trade_date);

-- 数据血缘：哪个标的的哪个字段来自哪次查询
CREATE TABLE IF NOT EXISTS data_source (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name  TEXT NOT NULL,
  trade_date  TEXT,
  code        TEXT,
  query       TEXT NOT NULL,
  api_tag     TEXT,
  api_type    TEXT,
  fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ds ON data_source(table_name, trade_date);

-- 自选股（用户真实数据，非演示）
CREATE TABLE IF NOT EXISTS watchlist (
  code        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  note        TEXT,
  added_at    TEXT NOT NULL
);

-- 持仓（用户真实录入）
CREATE TABLE IF NOT EXISTS portfolio (
  code        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  shares      REAL NOT NULL DEFAULT 0,   -- 持股数
  cost        REAL,                      -- 成本价
  updated_at  TEXT NOT NULL
);
"""


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path=None):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path or DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def db(path=None):
    conn = connect(path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert(conn, table, rows, keys, keep_non_null=False):
    """按 keys 做主键 upsert。rows 为 dict 列表；只写 rows 中出现的列。

    keep_non_null=True 时，新值为 NULL 不会覆盖库里已有的值。
    用于同一个主键被多个接口先后写入的表（如 sector_flow）：
    后写的接口若本就不提供某列，不应把前一个接口已写好的值擦成 NULL。
    """
    rows = [r for r in rows if r]
    if not rows:
        return 0
    cols = sorted({k for r in rows for k in r.keys()})
    placeholders = ",".join("?" for _ in cols)
    collist = ",".join(cols)
    if keep_non_null:
        update = ",".join(
            f"{c}=COALESCE(excluded.{c}, {table}.{c})" for c in cols if c not in keys
        )
    else:
        update = ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
    sql = f"INSERT INTO {table} ({collist}) VALUES ({placeholders})"
    if update:
        sql += f" ON CONFLICT({','.join(keys)}) DO UPDATE SET {update}"
    else:
        sql += f" ON CONFLICT({','.join(keys)}) DO NOTHING"
    n = 0
    for r in rows:
        vals = [r.get(c) for c in cols]
        conn.execute(sql, vals)
        n += 1
    return n


def json_dumps(o):
    return json.dumps(o, ensure_ascii=False)


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def latest_trade_date(conn):
    r = conn.execute(
        "SELECT MAX(trade_date) AS d FROM quote WHERE trade_date IS NOT NULL"
    ).fetchone()
    return r["d"] if r and r["d"] else None


if __name__ == "__main__":
    with db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM sqlite_master WHERE type='table'"
        ).fetchone()["c"]
        print(f"库已初始化：{DB_PATH}")
        print(f"表数量：{n}")
