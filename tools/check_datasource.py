#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""
行情数据源连通性自检。

必须先跑它，再跑信号生成。理由：

  GitHub Actions 的 runner 在境外，而 akshare 拉的是东方财富 / 新浪 /
  腾讯这些国内接口。境外出入口能不能连通、会不会被限流，是「每日信号
  自动化」方案能不能成立的先决条件。不先验证就上，等于每天早上
  安静地失败一次——任务状态还是绿的，但信号永远是昨天的。

检查项：
  1. 腾讯行情 qt.gtimg.cn（主行情源，取现价/市值/PB）
  2. 新浪行情 hq.sinajs.cn（备用行情源）
  3. 新浪交易日历（akshare，判定是否交易日 / 算 bar_date）
  4. 东方财富日线（akshare，K 线主源）

用法：python3.11 tools/check_datasource.py
退出码：0 全部可用 / 1 有不可用（信号生成大概率失败）/ 2 全部不可用
"""
from __future__ import annotations

import sys
import time

OK, WARN, BAD = [], [], []


def probe(name, fn, timeout=25):
    t0 = time.time()
    try:
        val = fn(timeout)
        ms = int((time.time() - t0) * 1000)
        OK.append(f"{name}：{val}（{ms}ms）")
        print(f"  ✓ {name:12s} {val}  ({ms}ms)")
        return True
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        BAD.append(f"{name}：{type(e).__name__}: {str(e)[:80]}（{ms}ms）")
        print(f"  ✗ {name:12s} {type(e).__name__}: {str(e)[:70]}  ({ms}ms)")
        return False


def p_tencent(timeout):
    import requests
    r = requests.get("https://qt.gtimg.cn/q=sh601899",
                     timeout=timeout,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    txt = r.content.decode("gbk", errors="ignore")
    if "~" not in txt or len(txt) < 60:
        raise ValueError(f"返回内容异常：{txt[:60]}")
    f = txt.split("~")
    return f"紫金矿业 现价 {f[3]}"


def p_sina(timeout):
    import requests
    r = requests.get("https://hq.sinajs.cn/list=sh601899",
                     timeout=timeout,
                     headers={"User-Agent": "Mozilla/5.0",
                              "Referer": "https://finance.sina.com.cn"})
    r.raise_for_status()
    txt = r.content.decode("gbk", errors="ignore")
    if "紫金矿业" not in txt:
        raise ValueError(f"返回内容异常：{txt[:60]}")
    return "紫金矿业 快照可取"


def p_calendar(timeout):
    import akshare as ak
    df = ak.tool_trade_date_hist_sina()
    days = [str(x)[:10] for x in df["trade_date"]]
    if len(days) < 100:
        raise ValueError(f"交易日数量异常：{len(days)}")
    return f"交易日历 {len(days)} 天，最新 {days[-1]}"


def p_kline(timeout):
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol="sh601899", adjust="qfq")
    if df is None or len(df) < 50:
        raise ValueError(f"K 线长度异常：{len(df) if df is not None else 0}")
    last = str(df["date"].iloc[-1])[:10]
    return f"日线 {len(df)} 根，最后 {last}"


def main():
    print("行情数据源连通性自检")
    print("=" * 62)
    probe("腾讯行情", p_tencent)      # 主行情源
    probe("新浪行情", p_sina)          # 备行情源
    probe("交易日历", p_calendar)      # 判定交易日
    probe("东财日线", p_kline)         # K 线主源
    print("=" * 62)

    if not BAD:
        print("全部可用 ✓ —— 可以生成信号")
        return 0
    if len(BAD) == len(OK) + len(BAD):
        print("全部不可用 ✗ —— 这个网络环境下信号生成必然失败")
        print("（GitHub Actions 境外 IP 访问国内行情接口常被限流，")
        print("  若如此请改用国内服务器或自建 runner）")
        return 2
    print(f"部分不可用（{len(BAD)} 项）⚠ —— 信号可能降级或失败")
    for b in BAD:
        print(f"    {b}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
