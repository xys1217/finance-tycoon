# -*- coding: utf-8 -*-
"""持仓风险提醒 端到端验证。

不依赖网络：直接 monkeypatch engine.Q 的行情接口，构造不同持仓场景，
断言 statement() 的 alerts 是否正确触发四级提醒：
  high 止损线 −25% / mid 单只超仓 / mid 行业集中>30% / low 现金<3%。

这是「工具必须能验证」原则的一环：风险提醒之前只是 engine 里的代码，
从没人证明它真的会在条件满足时弹出。本测试抓的就是「提醒是死代码」这类静默失效。

用法：
  python3.11 test_risk_alert.py
退出码：0 = 全过，1 = 有失败。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "modules", "paper"))

import engine as PE          # noqa: E402

# ── 行情 mock ──
PRICES = {}                  # code -> 现价
NAMES = {}                   # code -> 名称
HS300 = 4000.0               # 沪深300 点位（仅用于基准对比，不影响 alert）


def _install_mock():
    PRICES.clear()

    def fake_quotes(codes):
        out = {}
        for c in codes:
            if c in PRICES:
                out[c] = {"price": PRICES[c],
                          "name": NAMES.get(c, c),
                          "pct": 0.0}
        return out

    PE.Q.quotes = fake_quotes
    PE.Q.index_quote = lambda code="000300": {"price": HS300}
    # 简化 ETF 判定：5 开头且非 51xxx 国债/宽基 的宽基，这里只按前缀粗分
    PE.Q.is_etf = lambda c: c[:2] in ("51", "56", "15")


def mk_acc(positions: dict, cash: float) -> dict:
    """构造最小可用账户。复用 engine 的 _blank() 模板，再塞持仓。"""
    acc = PE._blank()
    acc["cash"] = cash
    acc["positions"] = positions
    return acc


def pos(code, name, shares, avg_cost, etf=False):
    NAMES[code] = name
    return {code: {"name": name, "shares": shares,
                   "avg_cost": avg_cost, "etf": etf}}


def has(alerts, level, substr):
    return any(a.get("level") == level and substr in a.get("msg", "")
               for a in alerts)


# ── 计数式断言 ──
n_pass = n_fail = 0


def step(desc, ok, detail=""):
    global n_pass, n_fail
    mark = "✓" if ok else "✗"
    print(f"  {mark} {desc}" + (f" ｜ {detail}" if detail and not ok else ""))
    if ok:
        n_pass += 1
    else:
        n_fail += 1


def main() -> int:
    _install_mock()
    print("=== 持仓风险提醒 端到端验证 ===")
    print(f"  阈值：止损 {PE.STOP_LOSS*100:.0f}% ｜ 个股上限 "
          f"{PE.POS_LIMIT*100:.1f}% ｜ ETF上限 {PE.ETF_POS_LIMIT*100:.0f}%"
          f" ｜ 行业>30% ｜ 现金<3%\n")

    # 1) 健康：1 只小幅盈利个股，仓位极小，现金充足 → 无任何 alert
    PRICES.clear()
    PRICES["600000"] = 11.0
    acc = mk_acc(pos("600000", "浦发银行", 100, 10.0), cash=198_900)
    al = PE.statement(acc)["alerts"]
    step("健康场景：小幅盈利 + 轻仓 + 现金充足，不产生任何提醒",
         len(al) == 0, f"实际产生了 {len(al)} 条: {al}")

    # 2) 止损 high：浮亏 −26% < −25%
    PRICES.clear()
    PRICES["600000"] = 74.0          # 成本 100 → 浮亏 −26%
    acc = mk_acc(pos("600000", "浦发银行", 1000, 100.0), cash=126_000)
    al = PE.statement(acc)["alerts"]
    step("止损场景：浮亏 −26% 触发 high 级「触及止损线」",
         has(al, "high", "止损"), f"alerts={al}")

    # 3) 单只超仓 mid（不触发止损，盈利）：市值占 equity 远超 6.5%×1.15
    PRICES.clear()
    PRICES["600000"] = 105.0         # 成本 100 → 浮盈 +5%
    acc = mk_acc(pos("600000", "浦发银行", 1000, 100.0), cash=95_000)
    al = PE.statement(acc)["alerts"]
    step("超仓场景：单只仓位 52.5% 触发 mid 级「单只仓位超上限」",
         has(al, "mid", "单只仓位"), f"alerts={al}")

    # 4) 现金不足 low：现金权重 1.5% < 3%
    PRICES.clear()
    PRICES["512040"] = 19.5          # ETF，成本同价
    acc = mk_acc(pos("512040", "价值ETF", 10000, 19.5, etf=True), cash=3_000)
    al = PE.statement(acc)["alerts"]
    step("现金不足场景：现金仅 1.5% 触发 low 级「现金仅」",
         has(al, "low", "现金仅"), f"alerts={al}")

    # 5) 行业集中 mid：两只银行股市值合计占 equity 83% > 30%
    PRICES.clear()
    PRICES["600000"] = 10.0
    PRICES["601398"] = 10.0
    acc = mk_acc({**pos("600000", "浦发银行", 1000, 10.0),
                  **pos("601398", "工商银行", 1000, 10.0)},
                 cash=4_000)
    al = PE.statement(acc)["alerts"]
    step("行业集中场景：银行股合计 83% 触发 mid 级「行业集中度」",
         has(al, "mid", "行业集中度"), f"alerts={al}")

    # 6) 负向：把止损个股价格改回盈利，high 必须消失
    #    （证明 alert 是条件触发的，不是恒为真）
    PRICES.clear()
    PRICES["600000"] = 110.0         # 成本 100 → 浮盈 +10%
    acc = mk_acc(pos("600000", "浦发银行", 1000, 100.0), cash=126_000)
    al = PE.statement(acc)["alerts"]
    step("负向：同持仓但浮盈 +10% 时，high 止损提醒必须消失",
         not has(al, "high", "止损"), f"alerts={al}")

    print(f"\n  结果：{n_pass} 通过 / {n_fail} 失败")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
