#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""
离线版（file:// 双击打开那个）检查。

离线版的定位是「没网也能看当天信号」，所以只验证：
  - 5 个模块都能渲染出内容（数据已内嵌）
  - 没有 JS 错误
  - 页面上没有 undefined / NaN / [object Object]

不验证买入/跟单这类需要联网的交互——离线版本来就不支持，
fetch 本地 API 在 file:// 下必然失败，这是预期行为不算 bug。

用法：python3.11 test_offline.py [index_static.html 路径]
退出码：0 通过 / 1 有问题
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "index_static.html")
PROBLEMS: list[str] = []


def step(name, ok, detail=""):
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        PROBLEMS.append(f"{name} {detail}")


def main():
    if not os.path.exists(PAGE):
        print(f"  ✗ 找不到 {PAGE}，先跑 python3.11 build_static.py")
        return 1

    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page()
        errors: list[str] = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        pg.goto(f"file://{PAGE}", wait_until="load", timeout=60000)
        pg.wait_for_timeout(2500)

        tabs = pg.query_selector_all(".tab, [data-tab], .nav button, button.tab")
        print(f"  找到 {len(tabs)} 个 tab")
        total_text = 0
        for i in range(len(tabs)):
            try:
                tabs[i].click()
                pg.wait_for_timeout(1200)
                body = pg.inner_text("body") or ""
                total_text = max(total_text, len(body))
            except Exception:
                pass

        # 逐个模块检查内容
        for i in range(len(tabs)):
            try:
                tabs[i].click()
                pg.wait_for_timeout(1000)
                body = pg.inner_text("body") or ""
                step(f"模块 {i + 1} 有内容", len(body.strip()) > 100, f"{len(body)} 字符")
            except Exception as e:
                step(f"模块 {i + 1}", False, str(e)[:60])

        body = pg.inner_text("body") or ""
        for bad in ["undefined", "NaN", "[object Object]"]:
            n = body.count(bad)
            step(f"页面无 '{bad}'", n == 0, f"{n} 次" if n else "")

        # ★ 内容正确性断言 —— 只检查「有内容」是不够的。
        #
        # 2026-09-04 的教训：J() 里用了 r.text()，而静态版 fetch 劫持层
        # 只实现了 json()，于是 #sigBox 显示「信号加载失败」，但其它模块
        # 照常渲染 —— 字符数够、也没未捕获的 JS 错误，「有内容 / 无错误」
        # 的检查全过，唯独当日信号是空的。所以必须断言信号区真的显示了
        # 今天的 sig_date，而且没在报错。
        sig_txt = ""
        for sel in ["#sigBox", "#quantBox"]:
            if pg.query_selector(sel):
                sig_txt += pg.inner_text(sel) or ""
        broke = [k for k in ["失败", "错误", "Error", "TypeError"] if k in sig_txt]
        step("信号区没在报错", not broke, sig_txt[:70] if broke else "")

        want = pg.evaluate("() => (window.__SIGNAL__ && window.__SIGNAL__.sig_date) || ''")
        if want:
            step(f"信号区显示当日 sig_date {want}", want in body,
                 "页面里找不到该日期" if want not in body else "")
        else:
            step("页面内嵌了 __SIGNAL__", False, "window.__SIGNAL__ 为空")

        # 离线版 fetch 本地 API 必然失败，这类错误不算 bug
        real = [e for e in errors
                if "favicon" not in e.lower()
                and "Failed to load resource" not in e
                and "net::ERR" not in e
                and "HTTP 404" not in e]
        step("无非预期的 JS 错误", not real,
             "; ".join(real[:2]) if real else "")

        b.close()

    print()
    if PROBLEMS:
        print("问题明细：")
        for p in PROBLEMS:
            print(f"  ✗ {p}")
        return 1
    print("离线版检查通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
