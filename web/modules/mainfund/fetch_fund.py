# -*- coding: utf-8 -*-
"""
主力 / 机构动向采集
重点：龙虎榜「上榜后 1/2/5/10 日」真实涨跌 —— 用来实证「跟着主力买能不能赚」，
而不是靠感觉。
用法:
    python3 fetch_fund.py            # 采集全部并写 api/mainfund.json
    python3 fetch_fund.py --quick    # 只做龙虎榜统计（快）
"""
from __future__ import annotations

import json
import os
import sys
import time
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
try:
    import akshare as ak
except Exception:
    ak = None

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(os.path.join(HERE, "..", "..", "api", "mainfund.json"))


def _f(v):
    """'5.54亿' / '-9684.33万' / '1.2%' → float（单位：元 or 百分数原值）"""
    if v is None:
        return np.nan
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--", "nan"):
        return np.nan
    mul = 1.0
    if s.endswith("%"):
        s = s[:-1]
    elif s.endswith("万亿"):
        s, mul = s[:-2], 1e12
    elif s.endswith("亿"):
        s, mul = s[:-1], 1e8
    elif s.endswith("万"):
        s, mul = s[:-1], 1e4
    try:
        return float(s) * mul
    except Exception:
        return np.nan


def _fund_rank() -> dict:
    """全市场个股资金流即时排名（流入/流出/净额）。"""
    t0 = time.time()
    try:
        df = ak.stock_fund_flow_individual(symbol="即时")
        df = df.copy()
        df["净额_num"] = df["净额"].map(_f)
        df["代码"] = df["股票代码"].astype(str).str.zfill(6)
        df = df.dropna(subset=["净额_num"])
        df = df.sort_values("净额_num", ascending=False)
        top_in = df.head(20)[["代码", "股票简称", "最新价", "涨跌幅", "净额"]]
        top_out = df.tail(20)[["代码", "股票简称", "最新价", "涨跌幅", "净额"]].iloc[::-1]
        return {
            "ok": True, "rows": len(df), "ms": int((time.time() - t0) * 1000),
            "top_in": top_in.to_dict("records"),
            "top_out": top_out.to_dict("records"),
        }
    except Exception as e:
        return {"ok": False, "rows": 0, "ms": int((time.time() - t0) * 1000),
                "error": f"{type(e).__name__}: {e}"[:200], "top_in": [], "top_out": []}


def _sector_flow() -> dict:
    """行业 + 概念板块资金流。"""
    t0 = time.time()
    out = {"ok": False, "industry": [], "concept": [], "ms": 0, "error": None}
    try:
        ind = ak.stock_fund_flow_industry(symbol="即时")
        ind = ind.sort_values("净额", ascending=False)
        out["industry"] = ind.head(15)[["行业", "行业-涨跌幅", "净额", "领涨股"]].to_dict("records")
        out["industry_out"] = ind.tail(10)[["行业", "行业-涨跌幅", "净额", "领涨股"]].to_dict("records")
    except Exception as e:
        out["error"] = f"行业: {type(e).__name__}: {e}"[:150]
    try:
        con = ak.stock_fund_flow_concept(symbol="即时")
        con = con.sort_values("净额", ascending=False)
        out["concept"] = con.head(15)[["行业", "行业-涨跌幅", "净额", "领涨股"]].to_dict("records")
        out["concept_out"] = con.tail(10)[["行业", "行业-涨跌幅", "净额", "领涨股"]].to_dict("records")
        out["ok"] = True
    except Exception as e:
        out["error"] = (out["error"] or "") + f" | 概念: {type(e).__name__}: {e}"[:150]
    out["ms"] = int((time.time() - t0) * 1000)
    return out


def _lhb_stats(days: int = 120) -> dict:
    """龙虎榜实证：机构净买入的股票，上榜之后到底涨没涨？
    这是回答「跟着主力能不能赚钱」的唯一硬证据。"""
    t0 = time.time()
    end = datetime.now().date()
    start = end - timedelta(days=days)
    res = {"ok": False, "ms": 0, "error": None, "n": 0,
           "buy": {}, "sell": {}, "verdict": "", "recent": []}
    try:
        df = ak.stock_lhb_detail_em(start_date=start.strftime("%Y%m%d"),
                                    end_date=end.strftime("%Y%m%d"))
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:200]
        res["ms"] = int((time.time() - t0) * 1000)
        return res

    if df is None or df.empty:
        res["error"] = "区间内无龙虎榜数据"
        return res

    df = df.copy()
    net = pd.to_numeric(df.get("龙虎榜净买额"), errors="coerce")
    df["_net"] = net
    for c in ("上榜后1日", "上榜后2日", "上榜后5日", "上榜后10日"):
        df[c] = pd.to_numeric(df.get(c), errors="coerce")

    def stat(sub, tag):
        d = {"n": int(len(sub))}
        for c in ("上榜后1日", "上榜后2日", "上榜后5日", "上榜后10日"):
            v = sub[c].dropna()
            d[c] = {"mean": round(float(v.mean()), 3) if len(v) else None,
                    "win": round(float((v > 0).mean() * 100), 1) if len(v) else None,
                    "n": int(len(v))}
        return d

    sub = df.dropna(subset=["_net"])
    buy = sub[sub["_net"] > 0]
    sell = sub[sub["_net"] < 0]
    res["buy"] = stat(buy, "buy")
    res["sell"] = stat(sell, "sell")
    res["n"] = int(len(sub))
    res["ok"] = True

    # 结论（只陈述事实，不夸大）
    try:
        b5 = res["buy"]["上榜后5日"]["mean"]
        s5 = res["sell"]["上榜后5日"]["mean"]
        if b5 is not None and s5 is not None:
            diff = round(b5 - s5, 3)
            verdict = (f"近 {days} 天 {len(sub)} 条龙虎榜：机构净买入组上榜后 5 日平均 "
                       f"{b5:+.2f}%，净卖出组 {s5:+.2f}%，价差 {diff:+.2f}pp。")
            if b5 < 0 and s5 < 0:
                verdict += ("两组均值都为负 —— 龙虎榜本身就是异动股，追进去整体是亏的；"
                            "跟随主力只能让你「少亏」，不能让你「赚钱」。"
                            "正确用法是拿它做排除/确认，不是当买入信号。")
            elif abs(diff) < 2:
                verdict += ("买入组略占优，但两组均值都接近 0 —— "
                            "「跟主力」不是稳定盈利口，只适合做排除/确认。")
            else:
                verdict += "买入组明显占优，值得进一步做分组回测验证持续性。"
            res["verdict"] = verdict
    except Exception:
        res["verdict"] = "样本不足，无法给出可靠结论。"

    # 最近机构净买入 top
    try:
        rr = df.dropna(subset=["_net"]).sort_values("_net", ascending=False).head(12)
        keep = ["代码", "名称", "收盘价", "涨跌幅", "龙虎榜净买额", "净买额占总成交比",
                "上榜后1日", "上榜后5日", "解读"]
        keep = [c for c in keep if c in rr.columns]
        res["recent"] = rr[keep].to_dict("records")
    except Exception:
        pass

    res["ms"] = int((time.time() - t0) * 1000)
    return res


def main():
    quick = "--quick" in sys.argv
    print("采集主力/机构动向…", flush=True)

    lhb = _lhb_stats(120)
    print(f"  龙虎榜实证: ok={lhb['ok']} n={lhb['n']} {lhb['ms']}ms", flush=True)
    if lhb.get("verdict"):
        print(f"    → {lhb['verdict']}", flush=True)

    fund = {"ok": False, "rows": 0, "top_in": [], "top_out": []}
    sector = {"ok": False, "industry": [], "concept": []}
    if not quick:
        fund = _fund_rank()
        print(f"  个股资金流: ok={fund['ok']} rows={fund['rows']} {fund.get('ms')}ms", flush=True)
        sector = _sector_flow()
        print(f"  板块资金流: ok={sector['ok']} {sector.get('ms')}ms", flush=True)

    out = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fund": fund, "sector": sector, "lhb": lhb,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n写入 {OUT}")


if __name__ == "__main__":
    main()
