#!/usr/bin/env python3
"""NeoData 访问层：客户端 + markdown 表格解析。

NeoData 返回的是「自然语言召回 + markdown 表格」的混合结构。
本模块负责把它变成干净的行列数据。

对外主要接口：
    query(text)                  -> dict           原始返回
    recall_blocks(resp)          -> list[Block]    展平后的内容块
    parse_tables(content)        -> list[Table]    markdown 表格
    num(s)                       -> float|None    数值清洗
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SKILL_ROOT = Path(
    "/Users/zhangzhijian/.workbuddy/plugins/cache/cb_teams_marketplace/"
    "finance-data/1.6.0/skills/neodata-financial-search"
)
QUERY_PY = SKILL_ROOT / "scripts" / "query.py"

PYTHON = os.environ.get("RUISI_PYTHON") or sys.executable


class TokenError(RuntimeError):
    """凭证缺失或过期 —— 需要重新获取后再同步。"""


def query_once(text, timeout=90):
    """单次调用 NeoData。凭证在 skill 侧自动缓存（12 小时）。"""
    if not QUERY_PY.exists():
        raise RuntimeError(f"找不到 NeoData 查询脚本：{QUERY_PY}")
    proc = subprocess.run(
        [PYTHON, str(QUERY_PY), "--query", text],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out, err = proc.stdout or "", proc.stderr or ""
    if "TOKEN_MISSING" in out or "TOKEN_EXPIRED" in out or "TOKEN_MISSING" in err or "TOKEN_EXPIRED" in err:
        raise TokenError("NeoData 凭证缺失或已过期")
    if proc.returncode != 0:
        if any(k in (out + err) for k in ("401", "403", "40101", "鉴权", "认证")):
            raise TokenError(f"NeoData 鉴权失败：{(err or out).strip()[:200]}")
        raise RuntimeError(f"NeoData 查询失败：{(err or out).strip()[:300]}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"NeoData 返回非 JSON：{out[:300]}") from e


class TransientError(RuntimeError):
    """网络/超时类错误 —— 可重试。"""


def query(text, timeout=90, attempts=3, base_delay=3.0):
    """带退避重试的查询。

    只重试网络类错误（超时、连接失败、5xx）。凭证问题与业务错误立即抛出，
    重试它们没有意义，还会平白拖长同步时间。
    """
    import time as _t

    last = None
    for i in range(attempts):
        try:
            return query_once(text, timeout=timeout)
        except TokenError:
            raise
        except (subprocess.TimeoutExpired, OSError) as e:
            last = TransientError(f"网络超时/失败：{e}")
        except RuntimeError as e:
            msg = str(e)
            if any(k in msg for k in ("timed out", "Timeout", "Connection", "连接", "SSL", "502", "503", "504")):
                last = TransientError(msg)
            else:
                raise
        if i < attempts - 1:
            _t.sleep(base_delay * (2 ** i))
    raise last or TransientError("未知网络错误")


class Block:
    __slots__ = ("tag", "type", "content", "query")

    def __init__(self, tag, typ, content, q=""):
        self.tag = tag
        self.type = typ
        self.content = content
        self.query = q

    def __repr__(self):
        return f"<Block {self.tag}/{self.type} {len(self.content)}c>"


def recall_blocks(resp, q=""):
    """展平 apiData.apiRecall 为 Block 列表。"""
    data = (resp or {}).get("data") or {}
    api = data.get("apiData") or {}
    out = []
    for b in api.get("apiRecall") or []:
        out.append(Block(b.get("tag", ""), b.get("type", ""), b.get("content") or "", q))
    return out


def entities(resp):
    data = (resp or {}).get("data") or {}
    api = data.get("apiData") or {}
    return api.get("entity") or []


TABLE_SEP = re.compile(r"^\|[\s:\-|]+\|$")


def parse_tables(content):
    """从 markdown 文本中抽出所有表格。

    返回 [{'header': [...], 'rows': [[...], ...]}, ...]
    行内多余的 | 会被保守处理：不拆分单元格内部的转义管道。
    """
    lines = content.splitlines()
    tables = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        if line.startswith("|") and i + 1 < n and TABLE_SEP.match(lines[i + 1].strip()):
            header = _split_row(line)
            rows = []
            j = i + 2
            while j < n and lines[j].strip().startswith("|"):
                rows.append(_split_row(lines[j].strip()))
                j += 1
            tables.append({"header": header, "rows": rows})
            i = j
        else:
            i += 1
    return tables


def _split_row(line):
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


UNITS = {"万亿": 1e12, "千亿": 1e11, "百亿": 1e10, "亿": 1e8, "千万": 1e7, "百万": 1e6, "万": 1e4}

NULLS = {"", "-", "--", "—", "–", "n/a", "na", "null", "none", "nan", "无", "未披露", "不适用"}


def num(s):
    """把表格单元格清洗成 float。识别不了就返回 None（绝不猜）。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip()
    if t.lower() in NULLS:
        return None
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1]
    if t.startswith("（") and t.endswith("）"):
        neg, t = True, t[1:-1]
    t = t.replace(",", "").replace("，", "").replace(" ", "").replace("\u3000", "")
    pct = t.endswith("%")
    if pct:
        t = t[:-1]
    mult = 1.0
    for u, m in UNITS.items():
        if t.endswith(u):
            mult, t = m, t[: -len(u)]
            break
    if t.startswith("+"):
        t = t[1:]
    t = t.replace("−", "-").replace("－", "-")
    try:
        v = float(t) * mult
    except ValueError:
        return None
    return -v if neg else v


def cell(row, header, *names):
    """按列名取值（支持多个候选列名，容忍表头微调）。返回原始字符串。"""
    for nm in names:
        for idx, h in enumerate(header):
            if h == nm:
                return row[idx] if idx < len(row) else None
    for nm in names:
        for idx, h in enumerate(header):
            if nm in h:
                return row[idx] if idx < len(row) else None
    return None


def num_cell(row, header, *names):
    return num(cell(row, header, *names))


def headings(content):
    """抽出所有 # 开头的标题行。"""
    return [l.strip("# ").strip() for l in content.splitlines() if l.strip().startswith("#")]


KV_RE = re.compile(r"([^|:：]+)\s*[:：]\s*([^|]+)")


def kv_pairs(content):
    """抽出形如 `市场: 沪市 | 币种: CNY` 的元信息。"""
    out = {}
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("|"):
            continue
        if s.count(":") + s.count("：") >= 1 and "|" in s:
            for k, v in KV_RE.findall(s):
                out[k.strip()] = v.strip()
    return out


def date_from_code_section(content):
    """从 `### 000001.SH 上证指数` 这样的标题里取代码与名称。"""
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("#"):
            s = s.strip("# ").strip()
            m = re.match(r"^([0-9A-Za-z._]+)\s+(.+)$", s)
            if m:
                return m.group(1), m.group(2).strip()
            m = re.match(r"^([0-9A-Za-z._]+)$", s)
            if m:
                return m.group(1), ""
    return None, None


def norm_date(s):
    """把各种日期写法统一成 2026-09-18。

    接口里出现过三种：20260918 / 2026-09-18 / 2026年08月29日。
    """
    if not s:
        return None
    t = str(s).strip()
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?$", t)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def today():
    return datetime.now().strftime("%Y-%m-%d")


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "上证指数最新点位涨跌幅"
    r = query(q)
    for b in recall_blocks(r, q):
        print(f"[{b.tag}] {b.type}  ({len(b.content)} chars)")
        for t in parse_tables(b.content):
            print("   表头:", t["header"][:8], "...")
            for row in t["rows"][:2]:
                print("   行  :", row[:8])
