#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""
信号自检脚本 —— 每次改完 daily_signal.py 或配置后必须跑一遍。

校验口径（与 src/exec_engine.py 的可执行口径一致）：
  1. bar_date < sig_date（铁律：绝不能用 sig_date 打分）
  2. 数据新鲜度（stale / bars_behind）
  3. 整手约束（100 股/份）
  4. 去重（同一标的不重复出现）
  5. 资金平衡（Σcost + 现金 == 本金）
  6. 三道不追高闸门（20日涨幅 / 52周高 / CMF）
  7. 仓位纪律（个股 30% / ETF 70%，股价 ≤ 120 元）
  8. 板块权限（科创/北交/B股）
  9. 佣金口径（0.025%，最低 5 元；个股卖出印花税 0.05%）

用法：python3.11 selfcheck_signal.py [signal.json 路径]
退出码：0 全通过 / 1 有失败项
"""

import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SIG = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "api", "daily_signal.json")

OK, FAIL = [], []


def chk(cond, name, detail=""):
    (OK if cond else FAIL).append(f"{name}" + (f" — {detail}" if detail else ""))
    return cond


def is_a_share(c):
    """板块权限：剔除科创板 688/689、北交所 4xx/8xx、B 股 900/200"""
    if not (isinstance(c, str) and len(c) == 6 and c.isdigit()):
        return False
    if c.startswith(("688", "689")):
        return False
    if c.startswith(("4", "8")):
        return False
    if c.startswith(("900", "200")):
        return False
    return True


def main():
    with open(SIG, encoding="utf-8") as f:
        d = json.load(f)

    sig_date = d["sig_date"]
    bar_date = d["bar_date"]
    capital = d["capital"]
    picks = d.get("picks") or []
    etfs = d.get("etfs") or []
    summ = d.get("summary") or {}
    fresh = d.get("data_freshness") or {}

    print(f"自检对象：{SIG}")
    print(f"  bar_date {bar_date} → sig_date {sig_date} | 本金 {capital:,.0f}")
    print("-" * 62)

    # 1. 日期铁律
    chk(bar_date < sig_date, "1. bar_date < sig_date",
        f"{bar_date} < {sig_date}")
    for fmt in (bar_date, sig_date):
        try:
            datetime.strptime(fmt, "%Y-%m-%d")
            good = True
        except Exception:
            good = False
        chk(good, f"1b. 日期格式合法 {fmt}")

    # 2. 数据新鲜度
    chk(fresh.get("stale") is False, "2. 数据未过期 stale=False",
        f"bars_behind={fresh.get('bars_behind')}")
    chk((fresh.get("bars_behind") or 0) == 0, "2b. bars_behind == 0")

    # 3. 整手约束 + 4. 去重
    used = set()
    dup = []
    lots_bad = []
    for p in picks:
        c = p["code"]
        if c in used:
            dup.append(c)
        used.add(c)
        if p["shares"] % 100 != 0:
            lots_bad.append(f"{c}:{p['shares']}")
        # 金额自洽：shares * px ≈ amount（px 含滑点 0.1%）
        if p.get("px"):
            calc = p["shares"] * p["px"]
            if abs(calc - p["amount"]) > max(0.5, p["amount"] * 0.002):
                lots_bad.append(f"{c} 金额不符 {calc:.2f} vs {p['amount']:.2f}")
    for e in etfs:
        c = e["code"]
        if c in used:
            dup.append(c)
        used.add(c)
        if e["shares"] % 100 != 0:
            lots_bad.append(f"{c}:{e['shares']}")

    chk(not dup, "3. 去重（used_codes 无重复）", ",".join(dup) if dup else "")
    chk(not lots_bad, "4. 整手约束 + 金额自洽",
        "; ".join(lots_bad) if lots_bad else f"{len(picks)+len(etfs)} 只全通过")

    # 5. 资金平衡
    total_cost = sum(p["cost"] for p in picks) + sum(e["cost"] for e in etfs)
    cash = capital - total_cost
    chk(abs(total_cost - summ.get("invested", -1)) < 1.0,
        "5. Σcost == summary.invested",
        f"{total_cost:,.2f} vs {summ.get('invested'):,.2f}")
    chk(cash >= -0.01, "5b. 现金不为负", f"现金 {cash:,.2f}")
    chk(abs(cash - summ.get("cash", -1)) < 1.0, "5c. 现金 == summary.cash",
        f"{cash:,.2f} vs {summ.get('cash'):,.2f}")
    chk(cash / capital >= 0.02, "5d. 保留 ≥2% 现金缓冲",
        f"{cash/capital*100:.2f}%")

    # 6. 三道闸门（热点仓）
    gates = d.get("hot_gates") or []
    hot = [p for p in picks if p.get("leg") == "热点仓"]
    if hot and gates:
        # hot_gates 是 [{"name","value","pass"}] 列表；
        # 注意单位：前两项是百分数（−22.386 表示 −22.386%），CMF 是比率
        gmap = {}
        for g in gates:
            n = g["name"]
            if "20日涨幅" in n:
                gmap["ret20"] = g
            elif "52周" in n:
                gmap["pct52w"] = g
            elif "CMF" in n:
                gmap["cmf"] = g
        for p in hot:
            g1 = gmap.get("ret20")
            g2 = gmap.get("pct52w")
            g3 = gmap.get("cmf")
            chk(g1 is not None and g1["value"] <= 15.0 + 1e-9,
                f"6a. 热点 {p['code']} 近20日涨幅 ≤ +15%",
                f"{g1['value']:.2f}%" if g1 else "缺闸门")
            chk(g2 is not None and g2["value"] <= 95.0 + 1e-9,
                f"6b. 热点 {p['code']} 现价 ≤ 52周高 95%",
                f"{g2['value']:.2f}%" if g2 else "缺闸门")
            chk(g3 is not None and g3["value"] >= -0.15 - 1e-9,
                f"6c. 热点 {p['code']} CMF(20) ≥ −0.15",
                f"{g3['value']:.3f}" if g3 else "缺闸门")
            chk(all(g["pass"] for g in gates),
                f"6d. 热点 {p['code']} 三闸门全过")
    elif hot:
        chk(False, "6. 三道闸门", "有热点仓但 hot_gates 为空 —— 闸门可能静默失效")
    else:
        chk(True, "6. 三道闸门（本期无热点仓，跳过）")

    # 7. 仓位纪律
    for p in picks:
        chk(p["price"] <= 120.0 + 1e-9, f"7a. 个股 {p['code']} 股价 ≤120",
            f"{p['price']}")
        chk(is_a_share(p["code"]), f"7b. 个股 {p['code']} 板块可买")
    stock_amt = sum(p["cost"] for p in picks)
    etf_amt = sum(e["cost"] for e in etfs)
    chk(stock_amt <= capital * 0.30 + 100, "7c. 个股腿 ≤30% 本金",
        f"{stock_amt:,.0f} = {stock_amt/capital*100:.2f}%")
    chk(etf_amt <= capital * 0.70 + 100, "7d. ETF 腿 ≤70% 本金",
        f"{etf_amt:,.0f} = {etf_amt/capital*100:.2f}%")
    chk(len(picks) > 0, "7e. 个股腿不为零（铁律：ETF 不替代个股）",
        f"{len(picks)} 只")
    defense = [p for p in picks if p.get("leg") == "防御仓"]
    chk(len(defense) >= 1, "7f. 防御仓不为零", f"{len(defense)} 只")

    # 8. 佣金口径
    fee_bad = []
    for p in picks + etfs:
        raw = p["amount"] * 0.00025
        exp = max(5.0, raw)
        if abs(p["fee"] - exp) > 0.02:
            fee_bad.append(f"{p['code']} {p['fee']} vs {exp:.2f}")
    chk(not fee_bad, "8. 佣金 0.025%（最低 5 元）",
        "; ".join(fee_bad) if fee_bad else f"{len(picks)+len(etfs)} 只全通过")

    # 9. 调仓日历
    rb = d.get("rebalance") or {}
    chk(rb.get("sig_date") == sig_date, "9. rebalance.sig_date 一致")
    if not rb.get("stocks_rebalance_day"):
        chk(len(picks) == 0 or rb.get("next_stock_rebalance"),
            "9b. 非调仓日给出下次调仓日", rb.get("next_stock_rebalance"))

    # 10. 止损扫描字段完整性
    sl = d.get("stop_loss_scan") or []
    for s in sl:
        chk(s.get("code") and ("pnl_pct" in s or "ret" in s or "pnl" in s),
            f"10. 止损扫描字段完整 {s.get('code')}")

    # 输出
    print()
    for x in OK:
        print(f"  ✓ {x}")
    if FAIL:
        print()
        for x in FAIL:
            print(f"  ✗ {x}")
    print("-" * 62)
    print(f"通过 {len(OK)} / 失败 {len(FAIL)}")
    if FAIL:
        print("结论：存在问题，请修复后重跑")
        return 1
    print("结论：全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
