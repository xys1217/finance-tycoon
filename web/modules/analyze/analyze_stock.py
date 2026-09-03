# -*- coding: utf-8 -*-
"""
个股分析引擎：给一个代码 → 量化因子 + 情绪热度 + 主力资金 + 新闻 → 值不值得买
设计原则：只输出**可验证的因子值**和**明确的不确定性**，不做模糊的"看好/推荐"。
"""
from __future__ import annotations

import json
import os
import re
import sys
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
try:
    import akshare as ak
except Exception:
    ak = None

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
API = os.path.join(ROOT, "api")

# 可执行口径：剔除科创板/北交所/B股
def _tradable(code: str) -> bool:
    return bool(re.fullmatch(r"(600|601|603|605|000|001|002|003|300|301)\d{3}", str(code)))


def _prefix(code: str) -> str:
    return "sh" if code.startswith("6") else ("sz" if code.startswith(("0", "3")) else "bj")


def _hist(code: str, days: int = 400) -> pd.DataFrame | None:
    """行情：东财 stock_zh_a_hist 实测连接被拒，改用新浪 stock_zh_a_daily（前复权）。"""
    try:
        df = ak.stock_zh_a_daily(symbol=_prefix(code) + code, adjust="qfq")
    except Exception:
        return None
    if df is None or len(df) < 60:
        return None
    df = df.tail(days).copy()
    df = df.rename(columns={"date": "日期", "open": "开盘", "close": "收盘",
                            "high": "最高", "low": "最低",
                            "volume": "成交量", "amount": "成交额"})
    return df.reset_index(drop=True)


def _cmf(df: pd.DataFrame, n: int = 20) -> float:
    """Chaikin Money Flow（简化版，用收盘价位置代替真实高低点区间）。"""
    try:
        h, l, c, v = df["最高"], df["最低"], df["收盘"], df["成交量"]
        rng = (h - l).replace(0, np.nan)
        mfv = ((c - l) - (h - c)) / rng * v
        return float(mfv.tail(n).sum() / v.tail(n).sum())
    except Exception:
        return float("nan")


def _factors(code: str, df: pd.DataFrame) -> dict:
    c = df["收盘"]
    last = float(c.iloc[-1])
    out = {"price": round(last, 2)}

    def ret(n):
        return (last / float(c.iloc[-n - 1]) - 1) * 100 if len(c) > n else float("nan")

    out["r20"] = round(ret(20), 2)          # 近20日涨幅
    out["r60"] = round(ret(60), 2)
    out["r250"] = round(ret(250), 2) if len(c) > 250 else None
    # 12-1 动量：近12个月 − 最近1个月
    if len(c) > 250:
        out["mom_12_1"] = round((float(c.iloc[-21]) / float(c.iloc[-251]) - 1) * 100, 2)
    else:
        out["mom_12_1"] = None

    r = np.log(c / c.shift()).dropna()
    out["vol60"] = round(float(r.tail(60).std() * np.sqrt(252) * 100), 2)  # 年化波动%
    out["cmf20"] = round(_cmf(df), 3)

    hi = float(df["最高"].tail(250).max()) if len(df) >= 250 else float(df["最高"].max())
    out["high250"] = round(hi, 2)
    out["pct_from_high"] = round(last / hi * 100, 1)   # 现价距52周高点%
    out["cmf20"] = out["cmf20"] if out["cmf20"] == out["cmf20"] else None
    return out


def _gates(f: dict) -> dict:
    """热点仓三道不追高闸门（项目硬约束）。"""
    g = []
    r20 = f.get("r20")
    g.append({"name": "近20日涨幅 ≤ +15%", "value": f"{r20:+.1f}%" if r20 is not None else "—",
              "limit": "+15%", "pass": (r20 is not None and r20 <= 15)})
    ph = f.get("pct_from_high")
    g.append({"name": "现价 ≤ 52周高点的 95%", "value": f"{ph:.1f}%" if ph else "—",
              "limit": "95%", "pass": (ph is not None and ph <= 95)})
    cmf = f.get("cmf20")
    g.append({"name": "资金流 CMF(20) ≥ −0.15", "value": f"{cmf:+.3f}" if cmf is not None else "—",
              "limit": "−0.15", "pass": (cmf is not None and cmf >= -0.15)})
    return {"gates": g, "all_pass": all(x["pass"] for x in g)}


def _exec_gate(f: dict, capital: float = 200_000.0,
               max_price: float = 120.0, pos_pct: float = 0.065) -> dict:
    """第零道闸门：可执行性（项目铁律，比三道不追高闸门更前置）。

    20 万本金买不起高价股 1 手，所以股价必须先过 ≤120 元；
    再按单只仓位上限（防御仓 6.5%）算能买几手——买不满 1 手等于不可执行。
    """
    px = f.get("price")
    budget = capital * pos_pct          # 单只可用资金上限
    lot = (px or 0) * 100               # 1 手金额
    hands = int(budget // lot) if lot > 0 else 0
    ok_price = px is not None and px <= max_price
    return {
        "price": px, "max_price": max_price,
        "lot_amount": round(lot, 2),
        "budget": round(budget, 2),
        "hands": hands,
        "cost": round(hands * lot, 2),
        "pos_pct": round(hands * lot / capital * 100, 2) if capital else 0,
        "ok": bool(ok_price and hands >= 1),
        "reason": (None if ok_price else f"股价 {px} 元 > 上限 {max_price} 元，"
                                         f"1 手需 {lot:,.0f} 元，占本金 {lot/capital*100:.1f}%")
                  if not ok_price else
                  (None if hands >= 1 else f"单只预算 {budget:,.0f} 元买不满 1 手（{lot:,.0f} 元）"),
    }


def _total_sources() -> int:
    """数据源总路数，从情绪状态文件读，避免写死后与采集配置打架。"""
    try:
        p = os.path.join(API, "sentiment_status.json")
        if os.path.exists(p):
            d = json.load(open(p, encoding="utf-8"))
            n = (d.get("meta") or {}).get("sources")
            if n:
                return int(n)
            return len(d.get("status") or {})
    except Exception:
        pass
    return 14


def _sentiment_hit(code: str) -> dict:
    """从已采集的情绪 JSON 里查该股的热度。"""
    p = os.path.join(API, "sentiment_status.json")
    if not os.path.exists(p):
        return {"ok": False, "hits": [], "score": None}
    try:
        d = json.load(open(p, encoding="utf-8"))
        hits = []
        for r in d.get("merged", []):
            if r["code"] == code:
                hits = r["srcs"]
                score = r["score"]
                break
        else:
            score = None
        return {"ok": True, "hits": hits, "score": score,
                "nsrc": len(hits), "ts": d.get("ts")}
    except Exception:
        return {"ok": False, "hits": [], "score": None}


def _fund_hit(code: str) -> dict:
    """从主力资金流 JSON 里查该股。"""
    p = os.path.join(API, "mainfund.json")
    if not os.path.exists(p):
        return {"ok": False}
    try:
        d = json.load(open(p, encoding="utf-8"))
        for r in d.get("fund", {}).get("top_in", []):
            if str(r.get("代码")) == code:
                return {"ok": True, "side": "流入前列", **{k: r[k] for k in
                        ("股票简称", "最新价", "涨跌幅", "净额") if k in r}}
        for r in d.get("fund", {}).get("top_out", []):
            if str(r.get("代码")) == code:
                return {"ok": True, "side": "流出前列", **{k: r[k] for k in
                        ("股票简称", "最新价", "涨跌幅", "净额") if k in r}}
        return {"ok": True, "side": "未进前20", "净额": None}
    except Exception:
        return {"ok": False}


def _news_hit(name: str, code: str) -> dict:
    """同花顺要闻里的提及（公司名 + 代码双通道）。"""
    if ak is None or not name:
        return {"ok": False, "n": 0, "titles": []}
    try:
        df = ak.stock_info_global_ths()
        hit = []
        for _, row in df.iterrows():
            t = f"{row.get('标题','')} {row.get('内容','')}"
            if (name and name in t) or code in t:
                hit.append(str(row.get("标题"))[:60])
        return {"ok": True, "n": len(hit), "titles": hit[:5]}
    except Exception:
        return {"ok": False, "n": 0, "titles": []}


def _name_of(code: str) -> str:
    try:
        df = ak.stock_info_a_code_name()
        m = df[df["code"].astype(str).str.zfill(6) == code]
        if len(m):
            n = re.sub(r"\s+", "", str(m.iloc[0]["name"]))
            # 防御：akshare 偶发回退到别的表，会返回纯数字串，判为无效
            return "" if (not n or n.isdigit()) else n
    except Exception:
        pass
    return ""


def analyze(code: str) -> dict:
    code = str(code).zfill(6)
    res: dict = {"code": code, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    if not _tradable(code):
        res["error"] = "该代码不在可交易 A 股范围（已剔除科创板/北交所/B股）"
        return res

    df = _hist(code)
    if df is None or len(df) < 60:
        res["error"] = "历史行情不足 60 个交易日，无法计算因子"
        return res

    name = _name_of(code)
    res["name"] = name
    res["bars"] = int(len(df))
    f = _factors(code, df)
    res["factors"] = f
    res["exec"] = _exec_gate(f)
    res["gates"] = _gates(f)
    res["sentiment"] = _sentiment_hit(code)
    res["fund"] = _fund_hit(code)
    res["news"] = _news_hit(name, code)

    # ── 综合判定（规则透明、可复核）──
    score, reasons, risks = 0, [], []
    mom = f.get("mom_12_1")
    if mom is not None:
        if mom > 20:
            score += 2; reasons.append(f"12-1 动量 {mom:+.1f}%，中期趋势向上")
        elif mom < 0:
            score -= 1; risks.append(f"12-1 动量 {mom:+.1f}%，中期趋势走弱")
    g = res["gates"]
    if g["all_pass"]:
        score += 2; reasons.append("三道不追高闸门全过（不追高）")
    else:
        fails = [x["name"] for x in g["gates"] if not x["pass"]]
        score -= 2; risks.append("未过闸门：" + "、".join(fails))
    if f.get("vol60") and f["vol60"] > 50:
        risks.append(f"年化波动 {f['vol60']:.0f}%，波动偏大")
    if res["sentiment"].get("nsrc", 0) >= 3:
        score += 1; reasons.append(f"情绪共振：{res['sentiment']['nsrc']} 个数据源同时点名")
    elif res["sentiment"].get("nsrc", 0) == 0:
        risks.append(f"情绪热度：{_total_sources()} 路数据源无一命中（冷门股）")
    if res["fund"].get("side") == "流入前列":
        score += 1; reasons.append("主力资金流入前列")
    elif res["fund"].get("side") == "流出前列":
        score -= 1; risks.append("主力资金流出前列")
    if res["news"].get("n", 0) > 0:
        score += 1; reasons.append(f"近期要闻提及 {res['news']['n']} 次")

    # 第零道闸门：不可执行的票，因子分再高也一票否决
    ex = res["exec"]
    res["executable"] = bool(ex["ok"])
    if not ex["ok"]:
        score = min(score, -3)
        risks.insert(0, f"不可执行：{ex['reason']}")

    res["score"] = score
    if not ex["ok"]:
        verdict, color = "不可执行", "red"
    elif score >= 3:
        verdict, color = "值得关注", "green"
    elif score >= 0:
        verdict, color = "中性观望", "amber"
    else:
        verdict, color = "倾向回避", "red"
    res["verdict"] = verdict
    res["verdict_color"] = color
    res["reasons"] = reasons
    res["risks"] = risks
    res["caveat"] = ("本判定为规则打分的结果，非投资建议。回测区间偏牛，"
                     "B vs A 方案 P(更优)=86.9%，未达 95% 统计显著。")
    return res


if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else "601872"
    print(json.dumps(analyze(c), ensure_ascii=False, indent=1))
