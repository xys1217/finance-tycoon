# -*- coding: utf-8 -*-
"""模拟盘引擎 —— 20 万本金，随时买卖，成本口径与回测完全一致。

可执行口径（项目铁律，与 src/exec_engine.py 保持一致）：
  1. 整手约束：100 股/份
  2. 佣金 0.025%，最低 5 元（买卖双向）
  3. 滑点 0.1%：买入价上浮、卖出价下浮
  4. 卖出印花税 0.05%，ETF 免征
  5. 剔除科创板 688/689、北交所 4xx/8xx、B 股 9xx
  6. 个股股价 ≤ 120 元（ETF 不适用）
  7. 个股止损线 −25%
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Dict, List, Optional

import quote as Q

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(os.path.join(HERE, "..", "..", "api", "paper.json"))

INIT_CASH = 200_000.0
LOT = 100
COMM_RATE = 0.00025      # 0.025%
COMM_MIN = 5.0
SLIP = 0.001             # 0.1%
TAX_RATE = 0.0005        # 0.05%，仅卖出
MAX_PRICE = 120.0        # 个股股价上限
STOP_LOSS = -0.25        # 个股 −25%
POS_LIMIT = 0.065        # 单只个股仓位上限 6.5%
ETF_POS_LIMIT = 0.12     # 单只 ETF 上限 12%

# v5 定案建议组合（2026-09-02 收盘信号），用于「一键跟单」
V5_PLAN = [
    # code,     名称,               腿,     目标权重
    ("601398", "工商银行",       "防御仓", 0.065),
    ("601288", "农业银行",       "防御仓", 0.065),
    ("600016", "民生银行",       "防御仓", 0.065),
    ("601166", "兴业银行",       "防御仓", 0.065),
    ("601872", "招商轮船",       "热点仓", 0.040),
    # ETF 腿 12 只分摊 58%：与原始信号「已投入 88%、现金 12%」对齐
    # （个股腿 26% 防御 + 4% 热点 = 30%，30% + 58% = 88%）
    ("512040", "价值ETF富国",     "ETF腿",  0.0483),
    ("512890", "红利低波ETF华泰柏瑞", "ETF腿", 0.0483),
    ("510880", "红利ETF华泰柏瑞",   "ETF腿",  0.0483),
    ("512530", "沪深300红利ETF建信", "ETF腿", 0.0483),
    ("510030", "价值ETF华宝",     "ETF腿",  0.0483),
    ("512390", "中国低波ETF平安",   "ETF腿",  0.0483),
    ("512750", "基本面50ETF嘉实",  "ETF腿",  0.0483),
    ("510010", "180治理ETF交银",  "ETF腿",  0.0483),
    ("511260", "十年国债ETF国泰",  "ETF腿",  0.0483),
    ("511020", "国债ETF平安",     "ETF腿",  0.0483),
    ("159934", "黄金ETF易方达",    "ETF腿",  0.0483),
    ("518880", "黄金ETF华安",     "ETF腿",  0.0483),
]

SIGNAL_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "..", "api", "daily_signal.json")


def load_signal() -> dict | None:
    """读取当日信号。没有就返回 None（调用方回落到内置 V5_PLAN）。"""
    try:
        with open(SIGNAL_JSON, encoding="utf-8") as f:
            d = json.load(f)
        return d if d.get("picks") or d.get("etfs") else None
    except Exception:
        return None


def sig_plan(sig: dict) -> List[tuple]:
    """把当日信号转成 (code, name, leg, budget, target_shares) 跟单计划。

    budget 必须取信号算好的 `budget`（如防御仓单只 13,000、ETF 单只 9,667），
    不能拿 `weight_actual` 反推 —— weight_actual 是**已扣掉成本之后**的实际
    占比，再乘本金当预算等于自我收紧：只要实时价比 bar_date 收盘价高一点点
    就会「差几块钱买不起 1 手」（三环集团预算 13,000，反推只有 11,100，
    而 100 股实时价要 11,105），随后被第二轮补位灌成重仓。

    target_shares 是信号算好的股数，跟单时优先照它下单，最忠于信号。
    """
    out = []
    for x in sig.get("picks", []) + sig.get("etfs", []):
        budget = float(x.get("budget") or 0)
        shares = int(x.get("shares") or 0)
        if budget <= 0 and shares <= 0:
            continue
        out.append((x["code"], x.get("name", ""),
                    x.get("leg") or "ETF腿", budget, shares))
    return out


# 行业标签，用于集中度提醒（只覆盖本组合，够用即可）
SECTOR = {
    "601398": "银行", "601288": "银行", "600016": "银行", "601166": "银行",
    "601872": "航运",
    "512040": "宽基价值", "510030": "宽基价值", "512750": "宽基价值",
    "510010": "宽基价值", "512390": "宽基价值",
    "512890": "红利", "510880": "红利", "512530": "红利",
    "511260": "国债", "511020": "国债",
    "159934": "黄金", "518880": "黄金",
}


# ---------------------------------------------------------------- 可执行口径
def tradable(code: str) -> tuple[bool, str]:
    """板块权限校验。

    注意 B 股只有 900xxx（沪市）与 200xxx（深市）两个号段，
    不能图省事写成 startswith("9") —— 那会把 999999 这类
    根本不存在的代码误报成「B 股」，排查时非常误导。
    """
    c = str(code).zfill(6)
    if not re.fullmatch(r"\d{6}", c):
        return False, f"代码格式不对：{code}（需要 6 位数字）"
    if c.startswith(("688", "689")):
        return False, "科创板（无权限）"
    if c.startswith(("4", "8")):
        return False, "北交所（无权限）"
    if c.startswith(("900", "200")):
        return False, "B 股（无权限）"
    if Q.is_etf(c):
        return True, ""
    if not re.match(r"^(60|68|00|30)", c):
        return False, f"非沪深 A 股：{c}"
    return True, ""


def commission(amount: float) -> float:
    return max(amount * COMM_RATE, COMM_MIN) if amount > 0 else 0.0


# ---------------------------------------------------------------- 账户
def _blank() -> dict:
    return {
        "meta": {"init_cash": INIT_CASH,
                 "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        "cash": INIT_CASH,
        "positions": {},
        "trades": [],
        "snapshots": [],
    }


def load() -> dict:
    if os.path.exists(OUT):
        try:
            d = json.load(open(OUT, encoding="utf-8"))
            for k, v in _blank().items():
                d.setdefault(k, v)
            d["meta"].setdefault("init_cash", INIT_CASH)
            return d
        except Exception:
            pass
    return _blank()


def save(d: dict) -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)


def reset() -> dict:
    d = _blank()
    save(d)
    return d


# ---------------------------------------------------------------- 交易
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def buy(acc: dict, code: str, shares: int, price: Optional[float] = None
        ) -> tuple[bool, str, Optional[dict]]:
    code = str(code).zfill(6)
    ok, why = tradable(code)
    if not ok:
        return False, why, None
    if shares <= 0 or shares % LOT:
        return False, f"下单数量必须是 {LOT} 的整数倍", None

    q = Q.one(code)
    if not q or not q.get("price"):
        return False, "取不到实时行情，无法成交", None
    if price is None:
        price = q["price"]
    etf = Q.is_etf(code)
    if not etf and price > MAX_PRICE:
        return False, (f"股价 {price} 元 > 上限 {MAX_PRICE} 元，"
                       f"1 手需 {price*LOT:,.0f} 元，20 万本金买不出合规仓位"), None

    px = round(price * (1 + SLIP), 4)          # 滑点：买入上浮
    amount = px * shares
    fee = commission(amount)
    total = amount + fee
    if total > acc["cash"] + 1e-6:
        maxsh = int((acc["cash"] * 0.999) // (px * LOT)) * LOT
        return False, (f"现金不足：需 {total:,.2f} 元，可用 {acc['cash']:,.2f} 元"
                       f"（最多可买 {maxsh} 份/股）"), None

    p = acc["positions"].get(code)
    if p:
        old_amt = p["avg_cost"] * p["shares"]
        p["shares"] += shares
        p["avg_cost"] = round((old_amt + total) / p["shares"], 4)
    else:
        acc["positions"][code] = {
            "code": code, "name": q.get("name") or code, "shares": shares,
            "avg_cost": round(total / shares, 4), "etf": etf,
            "first_buy": _now(),
        }
    acc["cash"] = round(acc["cash"] - total, 2)

    t = {"ts": _now(), "side": "买入", "code": code,
         "name": q.get("name") or code, "shares": shares, "price": px,
         "raw_price": price, "amount": round(amount, 2), "fee": round(fee, 2),
         "tax": 0.0, "cash_after": acc["cash"], "etf": etf}
    acc["trades"].append(t)
    save(acc)
    return True, f"买入 {q.get('name')} {shares} 份 @ {px:.3f}（含滑点）", t


def sell(acc: dict, code: str, shares: int, price: Optional[float] = None
         ) -> tuple[bool, str, Optional[dict]]:
    code = str(code).zfill(6)
    p = acc["positions"].get(code)
    if not p:
        return False, "未持有该标的", None
    # 顺序有讲究：先比持仓，再验整手 —— 用户输入 9999 时，
    # 「持仓不足」比「非整手」更能说明问题
    if shares > p["shares"]:
        return False, f"持仓不足：持有 {p['shares']} 份，要卖 {shares} 份", None
    if shares <= 0 or shares % LOT:
        return False, f"下单数量必须是 {LOT} 的整数倍", None

    q = Q.one(code)
    if not q or not q.get("price"):
        return False, "取不到实时行情，无法成交", None
    if price is None:
        price = q["price"]
    etf = p.get("etf") or Q.is_etf(code)

    px = round(price * (1 - SLIP), 4)          # 滑点：卖出下浮
    amount = px * shares
    fee = commission(amount)
    tax = 0.0 if etf else amount * TAX_RATE    # ETF 免印花税
    net = amount - fee - tax

    cost = p["avg_cost"] * shares
    pnl = net - cost                            # 已实现盈亏（已扣全部成本）
    p["shares"] -= shares
    acc["cash"] = round(acc["cash"] + net, 2)
    if p["shares"] <= 0:
        acc["positions"].pop(code, None)

    t = {"ts": _now(), "side": "卖出", "code": code,
         "name": p.get("name") or q.get("name") or code, "shares": shares,
         "price": px, "raw_price": price, "amount": round(amount, 2),
         "fee": round(fee, 2), "tax": round(tax, 2),
         "pnl": round(pnl, 2), "pnl_pct": round(pnl / cost * 100, 2) if cost else 0,
         "cash_after": acc["cash"], "etf": etf}
    acc["trades"].append(t)
    save(acc)
    return True, (f"卖出 {p.get('name')} {shares} 份 @ {px:.3f}，"
                  f"已实现 {pnl:+,.2f} 元（{t['pnl_pct']:+.2f}%）"), t


def buy_amount(acc: dict, code: str, target: float,
               price: Optional[float] = None) -> tuple[bool, str, Optional[dict]]:
    """按目标金额买入，自动向下取整到整手。"""
    q = Q.one(code)
    px = (price or (q or {}).get("price") or 0) * (1 + SLIP)
    if px <= 0:
        return False, "取不到实时行情，无法计算手数", None
    shares = int(target // (px * LOT)) * LOT
    if shares <= 0:
        return False, f"目标金额 {target:,.0f} 元买不满 1 手（需 {px*LOT:,.0f} 元）", None
    return buy(acc, code, shares, price)


def _buy_for_plan(acc: dict, code: str, budget: float,
                  target_shares: int) -> tuple[bool, str, Optional[dict]]:
    """按信号给定的目标股数下单，实时价偏离时逐手递减。

    优先照信号算好的 shares 买（最忠于信号，模拟盘与信号文档能对上）。
    但模拟盘用实时价、信号用 bar_date 收盘价，若实时价涨了导致超预算，
    就逐手往下减，直到金额落回预算内（留 2% 容差）；减到 0 手才算失败。

    预算兜底：信号没给 shares 时（回落到 V5_PLAN 的情况），按金额买。
    """
    if not target_shares:
        return buy_amount(acc, code, budget)

    q = Q.one(code)
    px = ((q or {}).get("price") or 0) * (1 + SLIP)
    if px <= 0:
        return False, "取不到实时行情，无法计算手数", None

    limit = budget * 1.02 if budget > 0 else float("inf")
    lots = target_shares // LOT
    while lots > 0:
        amt = lots * LOT * px
        if amt + max(COMM_MIN, amt * COMM_RATE) <= limit:
            break
        lots -= 1

    if lots <= 0:
        return (False,
                f"实时价 {px:.2f} 元，{target_shares} 股需 "
                f"{target_shares * px:,.0f} 元，超出预算 {budget:,.0f} 元（含 2% 容差）",
                None)

    shares = lots * LOT
    ok, msg, t = buy(acc, code, shares)
    if ok and shares < target_shares:
        msg += f"（实时价偏高，由 {target_shares} 股减至 {shares} 股）"
    return ok, msg, t


# ---------------------------------------------------------------- 一键跟单
def follow_plan(acc: dict) -> dict:
    """按 v5 定案组合一键建仓（各腿按目标权重分配，向下取整到整手）。

    两轮分配：
      第一轮按目标权重买；
      第二轮把剩余现金补给「目标金额买不满 1 手」的标的（国债 ETF 单价
      135/118 元，1 手要 1.1~1.4 万，按 5.8% 权重根本买不起，只能靠溢出资金补）。
    """
    sig = load_signal()
    equity0 = acc["cash"] + sum(
        (Q.one(c) or {}).get("price", 0) * p["shares"]
        for c, p in acc["positions"].items())

    if sig:
        plan = sig_plan(sig)
    else:  # 兜底：内置 V5_PLAN 只有权重，按权益折算预算
        plan = [(c, n, l, equity0 * w, 0) for c, n, l, w in V5_PLAN]

    log: List[dict] = []
    pending: List[tuple] = []
    for code, name, leg, budget, tgt in plan:
        ok, why = tradable(code)
        if not ok:
            log.append({"code": code, "name": name, "leg": leg, "ok": False, "msg": why})
            continue

        # 幂等跟单：只补到目标股数，已够就不再买。
        #
        # 之前不查已有持仓，点第二次「一键跟单」= 把 17 只全买第二遍：
        #   现金 26,840（13.4%）→ 4,025（2.0%），仓位悄悄翻倍。
        # 真实场景极易误触发（按钮点了没反应再点一下），而 v5 定案的
        # 仓位纪律是按「每只 6.5%」算的，翻倍后纪律荡然无存。
        have = int((acc["positions"].get(code) or {}).get("shares", 0))
        if tgt and have >= tgt:
            log.append({"code": code, "name": name, "leg": leg, "ok": True,
                        "msg": f"已持有 {have} 份，达到目标 {tgt} 份，无需买入",
                        "shares": 0, "amount": 0, "skipped": True})
            continue
        need = (tgt - have) if tgt else 0
        # 补差额时预算按比例缩，避免用整只的预算去买 1/3 的量
        budget = budget * need / tgt if (tgt and need > 0) else budget

        ok, msg, t = _buy_for_plan(acc, code, budget, need or 0)
        rec = {"code": code, "name": name, "leg": leg, "ok": ok, "msg": msg,
               "shares": (t or {}).get("shares"), "amount": (t or {}).get("amount")}
        log.append(rec)
        if not ok:
            pending.append((code, name, leg, budget, rec))

    # 第二轮：剩余现金补位——**只对 ETF 腿**。
    #
    # 这段补位是为「国债 ETF 单价 135/118 元、单只预算 9,667 买不起 1 手」
    # 设计的。原先它对任何买不起的标的都生效且无上限，个股一旦因实时价
    # 涨了几分钱买不起 1 手，就会被灌进全部剩余现金：
    #   三环集团目标 5.55%（11,100 元），实际被买成 400 股 44,419 元（22%），
    #   账户现金从 13.4% 直接打到 0.7%。
    # 现在限定：只有 ETF 腿能补，单只补位上限 = 预算 × 1.6，且账户总投入
    # 不得超过本金的 95%（留 5% 现金垫）。
    pending_etf = [p for p in pending if "ETF" in (p[2] or "")]
    dropped = [p for p in pending if "ETF" not in (p[2] or "")]
    for code, name, leg, budget, rec in dropped:
        rec["msg"] = (rec["msg"] or "") + "（个股腿不参与现金补位，避免超配）"

    if pending_etf and acc["cash"] > 0:
        share = acc["cash"] / len(pending_etf)
        for code, name, leg, budget, rec in pending_etf:
            cap = budget * 1.6 if budget > 0 else share
            room = equity0 * 0.95 - (equity0 - acc["cash"])
            want = max(0.0, min(share, cap, room))
            if want <= 0:
                rec["msg"] = (rec["msg"] or "") + "（已达 95% 投入上限，不补位）"
                continue
            ok, msg, t = buy_amount(acc, code, want)
            if ok:
                rec.update({"ok": True, "msg": msg + "（由剩余资金补齐）",
                            "shares": (t or {}).get("shares"),
                            "amount": (t or {}).get("amount")})
            else:
                rec["msg"] = msg

    src = f"{sig.get('sig_date')} 当日信号（bar {sig.get('bar_date')}）" if sig \
          else "内置 V5_PLAN（当日信号不可用时的兜底，标的为 09-02 快照）"
    warn = None
    if sig and not sig.get("rebalance", {}).get("stocks_rebalance_day"):
        warn = (f"今日非个股调仓日（下次 {sig.get('rebalance',{}).get('next_stock_rebalance')}），"
                f"按 v5 定案不应换股。本次跟单只是把账户建成当日信号的样子，"
                f"不是今天的真实指令。")
    return {"ok": True, "log": log, "source": src, "warn": warn,
            "sig_date": (sig or {}).get("sig_date"),
            "bar_date": (sig or {}).get("bar_date")}


# ---------------------------------------------------------------- 估值 / 快照
def snapshot(acc: dict, force: bool = False) -> None:
    """每日记录一次净值快照，用于画收益曲线。同一天只记一次（除非 force）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    sn = acc["snapshots"]
    if sn and sn[-1]["date"] == today and not force:
        return
    st = statement(acc)
    idx = Q.index_quote("000300")
    row = {"date": today, "equity": round(st["equity"], 2),
           "hs300": (idx or {}).get("price")}
    if sn and sn[-1]["date"] == today:
        sn[-1] = row
    else:
        sn.append(row)
    save(acc)


def statement(acc: dict) -> dict:
    """账户全貌：持仓估值、浮动盈亏、风险提醒。"""
    codes = list(acc["positions"].keys())
    qs = Q.quotes(codes) if codes else {}
    qs.pop("_ms", None)

    pos, mv, cost = [], 0.0, 0.0
    for c, p in acc["positions"].items():
        q = qs.get(c) or {}
        price = q.get("price")
        if not price:
            price = p["avg_cost"]          # 取不到行情时按成本价估值，不虚增浮盈
        m = price * p["shares"]
        cst = p["avg_cost"] * p["shares"]
        mv += m
        cost += cst
        pnl = m - cst
        pct = (price / p["avg_cost"] - 1) * 100 if p["avg_cost"] else 0
        pos.append({
            "code": c, "name": p.get("name") or q.get("name") or c,
            "shares": p["shares"], "avg_cost": p["avg_cost"], "price": price,
            "market_value": round(m, 2), "cost": round(cst, 2),
            "pnl": round(pnl, 2), "pnl_pct": round(pct, 2),
            "pct_today": q.get("pct"), "etf": p.get("etf") or Q.is_etf(c),
            "sector": SECTOR.get(c, "其他"),
        })
    pos.sort(key=lambda x: -x["market_value"])

    equity = acc["cash"] + mv
    init = acc["meta"].get("init_cash", INIT_CASH)
    realized = sum(t.get("pnl", 0) for t in acc["trades"] if t["side"] == "卖出")

    # ── 风险提醒 ──
    alerts = []
    for r in pos:
        if r["pnl_pct"] <= STOP_LOSS * 100:
            alerts.append({"level": "high", "code": r["code"], "name": r["name"],
                           "msg": f"触及止损线：浮亏 {r['pnl_pct']:.1f}%（阈值 −25%）"})
        w = r["market_value"] / equity if equity else 0
        lim = ETF_POS_LIMIT if r["etf"] else POS_LIMIT
        if w > lim * 1.15:
            alerts.append({"level": "mid", "code": r["code"], "name": r["name"],
                           "msg": f"单只仓位 {w*100:.1f}% 超上限 {lim*100:.1f}%"})
    sec: Dict[str, float] = {}
    for r in pos:
        sec[r["sector"]] = sec.get(r["sector"], 0) + r["market_value"]
    for s, v in sorted(sec.items(), key=lambda x: -x[1]):
        w = v / equity if equity else 0
        if s != "其他" and w > 0.30:
            alerts.append({"level": "mid", "code": "", "name": s,
                           "msg": f"行业集中度 {w*100:.1f}%（{s}），超过 30%"})
    cash_w = acc["cash"] / equity if equity else 0
    if cash_w < 0.03:
        alerts.append({"level": "low", "code": "", "name": "现金",
                       "msg": f"现金仅 {cash_w*100:.1f}%，低于 3%，无补仓空间"})

    # ── 基准对比 ──
    idx = Q.index_quote("000300")
    sn = acc["snapshots"]
    ret = (equity / init - 1) * 100 if init else 0
    base_ret = None
    if sn and sn[0].get("hs300") and idx:
        b0 = sn[0]["hs300"]
        base_ret = (idx["price"] / b0 - 1) * 100 if b0 else None

    return {
        "ts": _now(),
        "cash": round(acc["cash"], 2),
        "market_value": round(mv, 2),
        "equity": round(equity, 2),
        "init_cash": init,
        "total_pnl": round(equity - init, 2),
        "total_pnl_pct": round(ret, 2),
        "float_pnl": round(mv - cost, 2),
        "realized_pnl": round(realized, 2),
        "cash_weight": round(cash_w * 100, 2),
        "positions": pos,
        "sectors": [{"sector": s, "value": round(v, 2),
                     "weight": round(v / equity * 100, 2) if equity else 0}
                    for s, v in sorted(sec.items(), key=lambda x: -x[1])],
        "alerts": alerts,
        "trades": list(reversed(acc["trades"]))[:100],
        "n_trades": len(acc["trades"]),
        "snapshots": sn,
        "hs300": idx,
        "base_ret": round(base_ret, 2) if base_ret is not None else None,
        "alpha": round(ret - base_ret, 2) if base_ret is not None else None,
        "quote_ms": 0,
    }


def quote_for(code: str) -> dict:
    """下单前查价：返回实时价 + 可执行性判断 + 可买手数。"""
    code = str(code).zfill(6)
    ok, why = tradable(code)
    q = Q.one(code)
    if not q or not q.get("price"):
        return {"code": code, "ok": False, "error": "取不到实时行情"}
    px, etf = q["price"], Q.is_etf(code)
    if not ok:
        return {"code": code, "name": q.get("name"), "price": px, "ok": False,
                "error": why, "trading": Q.is_trading_now(q)}
    buy_px = round(px * (1 + SLIP), 4)
    lot_cost = buy_px * LOT + commission(buy_px * LOT)
    return {
        "code": code, "name": q.get("name"), "price": px,
        "prev_close": q.get("prev_close"), "pct": q.get("pct"),
        "buy_price": buy_px, "etf": etf, "ok": True,
        "trading": Q.is_trading_now(q), "ts": q.get("ts"),
        "lot_cost": round(lot_cost, 2),
        "price_limit_ok": True if etf else px <= MAX_PRICE,
        "max_price": None if etf else MAX_PRICE,
        "limit_up": q.get("limit_up"), "limit_down": q.get("limit_down"),
        "error": None if (etf or px <= MAX_PRICE) else
                 f"股价 {px} 元 > 上限 {MAX_PRICE} 元（20 万本金 1 手需 {px*LOT:,.0f} 元）",
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "plan":
        a = load()
        r = follow_plan(a)
        for x in r["log"]:
            print(f"  {'✓' if x['ok'] else '✗'} {x['code']} {x['name']:<18} "
                  f"{str(x.get('shares') or ''):>6} 份  {x['msg']}")
        print(json.dumps(statement(a), ensure_ascii=False, indent=1)[:1200])
    else:
        a = load()
        s = statement(a)
        print(f"现金 {s['cash']:,.2f} | 市值 {s['market_value']:,.2f} | "
              f"权益 {s['equity']:,.2f} | 总盈亏 {s['total_pnl']:+,.2f} "
              f"({s['total_pnl_pct']:+.2f}%)")
        for p in s["positions"]:
            print(f"  {p['code']} {p['name']:<16} {p['shares']:>6}份 "
                  f"成本{p['avg_cost']:>8.3f} 现价{p['price']:>8.3f} "
                  f"{p['pnl']:>+10.2f} ({p['pnl_pct']:+.2f}%)")
