# -*- coding: utf-8 -*-
"""实时行情：腾讯为主、新浪为备。

为什么不用 akshare / 东财：
  ak.stock_zh_a_spot_em、stock_bid_ash_em、stock_zh_a_hist 在本环境一律
  ConnectionError（Remote end closed connection），东财行情域名被拒。
  实测腾讯 qt.gtimg.cn（~50ms）与新浪 hq.sinajs.cn（~45ms）稳定可用，
  腾讯字段更全（含涨跌停价、市值、换手率），故作主力源。

腾讯字段索引（按 ~ 切分）：
  1 名称 / 2 代码 / 3 现价 / 4 昨收 / 5 今开 / 30 时间 / 31 涨跌 / 32 涨跌幅
  33 最高 / 34 最低 / 38 换手率 / 47 涨停价 / 48 跌停价
新浪字段索引（按 , 切分）：
  0 名称 / 1 今开 / 2 昨收 / 3 现价 / 4 最高 / 5 最低 / 30 日期 / 31 时间
"""
from __future__ import annotations

import re
import time
from typing import Dict, Optional

try:
    import requests
except Exception:
    requests = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

TIMEOUT = 8


def pfx(code: str) -> str:
    """沪深前缀：6/5 开头沪市，其余深市/北交所按 sz 处理。"""
    c = str(code).zfill(6)
    return "sh" if c[0] in ("6", "5") else "sz"


def is_etf(code: str) -> bool:
    """ETF / LOF：沪市 50/51/52/56/58，深市 15/16/18。免印花税。"""
    c = str(code).zfill(6)
    return c.startswith(("50", "51", "52", "56", "58", "15", "16", "18"))


def _f(x, d=0.0) -> float:
    try:
        v = float(x)
        return v
    except Exception:
        return d


def _tencent(codes: list[str]) -> Dict[str, dict]:
    if requests is None or not codes:
        return {}
    q = ",".join(pfx(c) + str(c).zfill(6) for c in codes)
    r = requests.get(f"https://qt.gtimg.cn/q={q}", headers={"User-Agent": UA},
                     timeout=TIMEOUT)
    r.encoding = "gbk"
    out: Dict[str, dict] = {}
    for line in r.text.split(";"):
        m = re.search(r'v_(s[hz]\d{6})="(.*)"', line)
        if not m:
            continue
        sym, body = m.group(1), m.group(2)
        p = body.split("~")
        if len(p) < 35 or not p[3]:
            continue
        code = sym[2:]
        out[code] = {
            "code": code,
            "name": p[1],
            "price": _f(p[3]),
            "prev_close": _f(p[4]),
            "open": _f(p[5]),
            "high": _f(p[33]) if len(p) > 33 else _f(p[3]),
            "low": _f(p[34]) if len(p) > 34 else _f(p[3]),
            "change": _f(p[31]) if len(p) > 31 else 0.0,
            "pct": _f(p[32]) if len(p) > 32 else 0.0,
            "turnover": _f(p[38]) if len(p) > 38 else None,
            "limit_up": _f(p[47]) if len(p) > 47 else None,
            "limit_down": _f(p[48]) if len(p) > 48 else None,
            "ts": p[30] if len(p) > 30 else "",
            "etf": is_etf(code),
            "src": "tx",
        }
    return out


def _sina(codes: list[str]) -> Dict[str, dict]:
    if requests is None or not codes:
        return {}
    q = ",".join(pfx(c) + str(c).zfill(6) for c in codes)
    r = requests.get(f"https://hq.sinajs.cn/list={q}",
                     headers={"User-Agent": UA,
                              "Referer": "https://finance.sina.com.cn"},
                     timeout=TIMEOUT)
    r.encoding = "gbk"
    out: Dict[str, dict] = {}
    for line in r.text.split(";"):
        m = re.search(r'hq_str_(s[hz]\d{6})="(.*)"', line)
        if not m:
            continue
        code, body = m.group(1)[2:], m.group(2)
        p = body.split(",")
        if len(p) < 32 or not p[3]:
            continue
        px, pre = _f(p[3]), _f(p[2])
        out[code] = {
            "code": code,
            "name": p[0],
            "price": px,
            "prev_close": pre,
            "open": _f(p[1]),
            "high": _f(p[4]),
            "low": _f(p[5]),
            "change": round(px - pre, 4) if pre else 0.0,
            "pct": round((px - pre) / pre * 100, 4) if pre else 0.0,
            "turnover": None,
            "limit_up": round(pre * 1.1, 3) if pre else None,
            "limit_down": round(pre * 0.9, 3) if pre else None,
            "ts": f"{p[30]} {p[31]}" if len(p) > 31 else "",
            "etf": is_etf(code),
            "src": "sina",
        }
    return out


def index_quote(code: str = "000300") -> Optional[dict]:
    """指数行情（沪深300 等）。腾讯对指数走另一套精简格式 s_sh000300：
       1 名称 / 2 代码 / 3 现价 / 4 涨跌 / 5 涨跌幅 / 6 成交量
       完整格式 v_sh000300 的字段到时间戳就截断，取不到涨跌幅。
    """
    if requests is None:
        return None
    c = str(code).zfill(6)
    # 指数不能用股票前缀规则：沪市指数 000xxx（上证/沪深300），深市指数 399xxx
    mkt = "sz" if c.startswith("39") else "sh"
    try:
        r = requests.get(f"https://qt.gtimg.cn/q=s_{mkt}{c}",
                         headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.encoding = "gbk"
        m = re.search(r'v_s_(s[hz]\d{6})="(.*)"', r.text)
        if not m:
            return None
        p = m.group(2).split("~")
        if len(p) < 6:
            return None
        px = _f(p[3])
        return {"code": str(code).zfill(6), "name": p[1], "price": px,
                "change": _f(p[4]), "pct": _f(p[5]), "src": "tx-index"}
    except Exception:
        return None


def quotes(codes: list[str]) -> Dict[str, dict]:
    """批量取实时行情，腾讯失败自动回落新浪。"""
    codes = [str(c).zfill(6) for c in codes if c]
    if not codes:
        return {}
    t0 = time.time()
    got: Dict[str, dict] = {}
    try:
        got = _tencent(codes)
    except Exception:
        got = {}
    miss = [c for c in codes if c not in got]
    if miss:
        try:
            got.update(_sina(miss))
        except Exception:
            pass
    for c in codes:
        if c not in got:
            got[c] = {"code": c, "name": "", "price": None, "error": "行情获取失败"}
    got["_ms"] = int((time.time() - t0) * 1000)  # type: ignore
    return got


def one(code: str) -> Optional[dict]:
    q = quotes([code])
    v = q.get(str(code).zfill(6))
    return v if v and v.get("price") else None


def is_trading_now(q: dict) -> bool:
    """行情时间戳落在交易时段内 → 视为实时价；否则是收盘价。"""
    ts = (q or {}).get("ts", "")
    m = re.search(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})", str(ts))
    if not m:
        return False
    hh, mm = int(m.group(4)), int(m.group(5))
    t = hh * 60 + mm
    return (9 * 60 + 30) <= t <= (11 * 60 + 30) or (13 * 60) <= t <= (15 * 60)


if __name__ == "__main__":
    import sys
    cs = sys.argv[1:] or ["601872", "000001", "601398", "512040", "159934"]
    r = quotes(cs)
    print(f"耗时 {r.pop('_ms')}ms")
    for c in cs:
        v = r.get(c, {})
        print(f"  {c} {v.get('name',''):<12} 现价 {v.get('price')} "
              f"({v.get('pct'):+.2f}%) 昨收 {v.get('prev_close')} "
              f"ETF={v.get('etf')} 源={v.get('src')} {v.get('ts')}")
