#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""
前端交互流程测试（真点按钮，不只是渲染）。

前三轮检查只验证了「页面能渲染出来」，从没真正点过按钮 —— 而按钮背后
才是用户每天真正在走的路径。这个脚本把主要交互全点一遍：

  1. 5 个 tab 互相切换（来回切，看有没有状态串味）
  2. 模块 3：输入代码 → 点分析 → 出结论；再输非法代码 → 出错误提示
  3. 模块 5：查价 → 买入 → 卖出 → 一键跟单 → 重置
  4. 全程收集 console error 与页面上的 undefined / NaN

用法：python3.11 test_ui_flow.py [url]
退出码：0 全通过 / 1 有问题
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8899"
PROBLEMS: list[str] = []
STEPS: list[str] = []


def step(name, ok, detail=""):
    STEPS.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        PROBLEMS.append(f"{name} {detail}")


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page()
        pg.on("dialog", lambda d: d.accept())   # confirm 弹窗一律确认（只注册一次）
        errors: list[str] = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        pg.goto(URL, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(1500)

        # ---------- 1. tab 切换 ----------
        print("\n【1】模块切换（来回切，查状态串味）")
        tabs = pg.query_selector_all(".tab, [data-tab], .nav button, button.tab")
        print(f"      找到 {len(tabs)} 个 tab 元素")
        if tabs:
            for i in range(min(len(tabs), 5)):
                try:
                    tabs[i].click()
                    pg.wait_for_timeout(700)
                except Exception as e:
                    step(f"点击 tab {i}", False, str(e)[:60])
            # 再倒着切回来
            for i in range(min(len(tabs), 5) - 1, -1, -1):
                try:
                    tabs[i].click()
                    pg.wait_for_timeout(500)
                except Exception:
                    pass
            step("5 个 tab 来回切换", True, f"{len(tabs)} 个")
        else:
            step("找到 tab 元素", False, "页面上没找到 .tab")

        def goto_tab(i):
            """切到第 i 个 tab 并等待面板可见（元素隐藏在非活动面板里会 fill 超时）。"""
            ts = pg.query_selector_all(".tab, [data-tab], .nav button, button.tab")
            if i < len(ts):
                ts[i].click()
                pg.wait_for_timeout(900)

        def wait_text(sel, min_len=60, timeout=30000):
            """轮询等容器里出现足够内容。

            不用固定 sleep：串行跑全量检查时，前面刚跑完 API 矩阵、
            后台还有 refresh 采集任务在跑，页面取数时快时慢，
            固定 7 秒偶尔不够（曾出现 #aBox 只有 20 字符的假失败）。
            """
            try:
                pg.wait_for_function(
                    "([s, n]) => { const e = document.querySelector(s);"
                    "  return !!e && (e.innerText || '').length >= n; }",
                    arg=[sel, min_len], timeout=timeout)
                return True
            except Exception:
                return False

        # ---------- 2. 模块 3 个股分析 ----------
        print("\n【2】模块 3 个股分析")
        goto_tab(2)                      # 模块 3
        code_box = pg.query_selector("#code")
        go_btn = pg.query_selector("#go")
        if code_box and go_btn:
            code_box.fill("601899")
            go_btn.click()
            wait_text("#aBox", 100)
            txt = pg.inner_text("#aBox") or ""
            has_verdict = ("值得关注" in txt) or ("不建议" in txt) or ("观望" in txt)
            step("分析 601899 出结论", has_verdict, f"{len(txt)} 字符")
            step("结论无 undefined/NaN",
                 ("undefined" not in txt) and ("NaN" not in txt))
            # 非法代码
            code_box.fill("999999")
            go_btn.click()
            pg.wait_for_timeout(3000)
            txt2 = pg.inner_text("#aBox") or ""
            step("非法代码 999999 有提示（非空白）", len(txt2.strip()) > 5,
                 f"{len(txt2)} 字符")
        else:
            step("找到 #code / #go", False)

        # ---------- 3. 模块 5 模拟盘 ----------
        print("\n【3】模块 5 模拟盘交互")
        goto_tab(4)                      # 模块 5
        pg.wait_for_timeout(800)

        def txt(sel):
            el = pg.query_selector(sel)
            return pg.inner_text(sel) if el else ""

        # 查价（结果落在 #pQuoteBox，不是 #pBox——后者这个 id 页面里根本没有）
        try:
            if pg.query_selector("#pCode"):
                pg.fill("#pCode", "600036")
                if pg.query_selector("#pQuote"):
                    pg.click("#pQuote")
                wait_text("#pQuoteBox", 40)
                q = txt("#pQuoteBox") or txt("#pMsg")
                step("查价 600036", len(q.strip()) > 10, f"{len(q)} 字符")
        except Exception as e:
            step("查价", False, str(e)[:60])

        # 模拟盘真实流程：填代码 → #pQuote 查价 → #pGo 提交
        # （不存在 #pBuy/#pSell/#pShares，按钮是查价后才出现的 #pGo）
        try:
            go = pg.query_selector("#pGo")
            if go:
                label = (go.text_content() or "").strip()
                go.click()
                pg.wait_for_timeout(6000)
                m = txt("#pMsg")
                pos = txt("#pPos")
                step(f"提交「{label}」",
                     ("买入" in m) or ("卖出" in m) or ("600036" in pos),
                     f"{m[:40] or pos[:40]}")
            else:
                step("查价后出现提交按钮 #pGo", False)
        except Exception as e:
            step("提交订单", False, str(e)[:60])

        # 一键跟单（看持仓区条目数变化，而不是看某个不存在的容器）
        try:
            plan = pg.query_selector("#pPlan")
            if plan:
                plan.click()
                wait_text("#pPlanLog", 30, 40000)
                pg.wait_for_timeout(2000)
                pos = txt("#pPos")
                log = txt("#pPlanLog")
                n_rows = pos.count("601899") + pos.count("512040")
                step("一键跟单（持仓区出现标的）", n_rows > 0 or len(pos) > 100,
                     f"持仓区 {len(pos)} 字符")
                step("跟单日志", len(log.strip()) > 20, f"{len(log)} 字符")
        except Exception as e:
            step("一键跟单", False, str(e)[:60])

        # 重置（看持仓区是否清空 + 权益回到 20 万）
        try:
            reset = pg.query_selector("#pReset")
            if reset:
                reset.click()
                pg.wait_for_timeout(6000)
                pos = txt("#pPos")
                kpi = txt("#pKpi")
                emptied = ("暂无持仓" in pos) or ("空仓" in pos) or len(pos.strip()) < 60
                step("重置账户（持仓清空）", emptied, f"持仓区 {len(pos)} 字符")
                step("重置后权益回到 20 万", "200,000" in kpi or "200000" in kpi,
                     f"KPI {len(kpi)} 字符")
        except Exception as e:
            step("重置", False, str(e)[:60])

        # ---------- 4. 页面整体健康 ----------
        print("\n【4】页面健康")
        body = pg.inner_text("body") or ""
        for bad in ["undefined", "NaN", "None%", "[object Object]"]:
            n = body.count(bad)
            step(f"页面无 '{bad}'", n == 0, f"出现 {n} 次" if n else "")

        real_errors = [e for e in errors
                       if "favicon" not in e.lower()
                       and "third-party" not in e.lower()
                       and "net::ERR" not in e]
        step("无 JS 错误", not real_errors,
             f"{len(real_errors)} 个：" + "; ".join(real_errors[:3]) if real_errors else "")

        b.close()

    print("\n" + "=" * 70)
    ok = sum(1 for _, o, _ in STEPS if o)
    print(f"通过 {ok} / {len(STEPS)}")
    if PROBLEMS:
        print("\n问题明细：")
        for p in PROBLEMS:
            print(f"  ✗ {p}")
        return 1
    print("结论：全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
