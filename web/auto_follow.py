#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动跟单：照当日信号把模拟盘建仓 / 调仓到目标状态。

供 GitHub Actions 在「每日信号生成」之后调用，实现用户外出期间的
无人值守维护。

特性：
- follow_plan 幂等：已持仓则只补到目标股数，到调仓日才换股；
  非调仓日本质上是 no-op（维持），所以每天跑安全。
- 依赖实时行情取现价做 2% 容差判断；若取不到会走预算路径，
  不会因行情失败而建不出仓。
- 退出码 0 = 成功，1 = 跟单逻辑报错（供 CI 判断是否失败）。

注意：本脚本不依赖 cwd，engine.OUT 按文件位置推算 web/api/paper.json。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "modules", "paper"))

import engine as PE  # noqa: E402


def main() -> int:
    acc = PE.load()
    before = len(acc["positions"])
    r = PE.follow_plan(acc)
    PE.snapshot(acc, force=True)
    st = PE.statement(acc)
    print("跟单 ok=%s msg=%s" % (r.get("ok"), r.get("msg")))
    print("持仓 %d 只 -> %d 只 | 现金 %.2f | 净值 %.2f"
          % (before, len(st["positions"]), st["cash"], st["equity"]))
    alerts = st.get("alerts", [])
    if alerts:
        print("风险提醒 %d 条:" % len(alerts))
        for a in alerts:
            print("  [%s] %s" % (a.get("level"), a.get("msg")))
    else:
        print("风险提醒：无")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
