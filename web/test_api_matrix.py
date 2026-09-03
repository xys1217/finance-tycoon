#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""
API 全端点 × 参数矩阵测试。

存在的理由：前三轮检查都是「验证我认为对的地方」，结果每次都漏掉
没想到的路径。这个脚本改成穷举——把每个端点的正常/缺失/非法/边界
参数全打一遍，任何一处返回 500（未捕获异常）都算失败。

判定标准：
  - HTTP 500                          → 失败（未捕获异常，必须修）
  - 该拦截却放行 / 该成功却失败        → 失败（与预期不符）
  - HTTP 4xx 且带明确 error/msg        → 通过（正常拒绝）

用法：python3.11 test_api_matrix.py [base_url]
退出码：0 全通过 / 1 有失败
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8899"
PASS: list[str] = []
FAIL: list[str] = []


def call(method: str, path: str, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        raw = r.read().decode()
        try:
            return r.status, json.loads(raw)
        except Exception:
            return r.status, {"_raw": raw[:200]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw[:200]}
    except Exception as e:
        return 0, {"_exc": f"{type(e).__name__}: {e}"}


def chk(name: str, status: int, d: dict, expect: str, detail=""):
    """expect: 'ok' 期望成功 | 'reject' 期望被拒 | 'any' 只要别 500

    注意「被拒」的两种合法形态本项目都在用：
      - HTTP 4xx + error/msg
      - HTTP 200 + {"ok": false, "msg": ...}   （前端好处理，不用判状态码）
    所以判定拒绝要看 ok/msg，不能只看状态码。
    """
    ok = (d or {}).get("ok")
    err = (d or {}).get("error") or ""
    msg = (d or {}).get("msg") or ""
    if status == 0 or status >= 500:
        res = "FAIL"                       # 未捕获异常，一定是 bug
    elif expect == "ok":
        res = "pass" if (status == 200 and ok is not False and not err) else "FAIL"
    elif expect == "reject":
        res = "pass" if (ok is False or status >= 400 or err or msg) else "FAIL"
    else:
        res = "pass"
    err = err or msg
    line = f"{name}  [{status}] {str(err)[:58] or detail}"
    (PASS if res == "pass" else FAIL).append(line)
    print(f"  {'✓' if res == 'pass' else '✗'} {line}")


def main():
    print(f"目标 {BASE}")
    print("=" * 74)

    # ---------- 1. GET 端点：正常 + 缺参 + 非法参 ----------
    print("\n【1】GET 端点")
    for p, exp in [("/api/quant", "ok"), ("/api/daily_signal", "ok"),
                   ("/api/positions", "ok"), ("/api/sentiment", "ok"),
                   ("/api/mainfund", "ok"), ("/api/paper", "ok")]:
        chk(f"GET {p}", *call("GET", p), exp)

    print("  -- 缺参数 / 非法参数（应明确拒绝，不能 500）--")
    chk("GET /api/analyze 无 code", *call("GET", "/api/analyze"), "reject")
    chk("GET /api/analyze code=空", *call("GET", "/api/analyze?code="), "reject")
    chk("GET /api/analyze code=乱码", *call("GET", "/api/analyze?code=ZZZZZZ"), "reject")
    chk("GET /api/analyze code=指数", *call("GET", "/api/analyze?code=999999"), "reject")
    chk("GET /api/analyze code=超长", *call("GET", "/api/analyze?code=" + "6" * 50), "reject")
    chk("GET /api/quote 无 code", *call("GET", "/api/paper/quote"), "reject")
    chk("GET /api/quote 乱码", *call("GET", "/api/paper/quote?code=ABC"), "reject")
    chk("GET 不存在的路径", *call("GET", "/api/nope"), "any")

    # ---------- 2. 下单：非法参数 ----------
    print("\n【2】POST /api/paper/order 非法输入（全部应被拦截）")
    call("POST", "/api/paper/reset", {})     # 先复位
    illegal = [
        ("空 body", {}),
        ("无 code", {"side": "buy", "mode": "shares", "value": 100}),
        ("value=0", {"code": "600519", "side": "buy", "mode": "shares", "value": 0}),
        ("value 负数", {"code": "600519", "side": "buy", "mode": "shares", "value": -100}),
        ("value 非数字", {"code": "600519", "side": "buy", "mode": "shares", "value": "abc"}),
        ("非整手 150", {"code": "600519", "side": "buy", "mode": "shares", "value": 150}),
        ("科创板", {"code": "688981", "side": "buy", "mode": "shares", "value": 100}),
        ("北交所", {"code": "430047", "side": "buy", "mode": "shares", "value": 100}),
        ("B股沪", {"code": "900901", "side": "buy", "mode": "shares", "value": 100}),
        ("B股深", {"code": "200011", "side": "buy", "mode": "shares", "value": 100}),
        ("非法 6 位", {"code": "999999", "side": "buy", "mode": "shares", "value": 100}),
        ("字母代码", {"code": "ABCDEF", "side": "buy", "mode": "shares", "value": 100}),
        ("空代码", {"code": "", "side": "buy", "mode": "shares", "value": 100}),
        ("未知方向", {"code": "600519", "side": "hold", "mode": "shares", "value": 100}),
        ("卖未持有", {"code": "601899", "side": "sell", "mode": "shares", "value": 100}),
        ("天量买单", {"code": "600519", "side": "buy", "mode": "shares", "value": 99999999}),
    ]
    for name, body in illegal:
        chk(f"下单 {name}", *call("POST", "/api/paper/order", body), "reject")

    # ---------- 3. 正常买卖来回 ----------
    print("\n【3】买卖闭环")
    chk("买紫金 300 股", *call("POST", "/api/paper/order",
                              {"code": "601899", "side": "buy", "mode": "shares", "value": 300}), "ok")
    chk("卖紫金 100 股", *call("POST", "/api/paper/order",
                              {"code": "601899", "side": "sell", "mode": "shares", "value": 100}), "ok")
    chk("按金额买 ETF", *call("POST", "/api/paper/order",
                             {"code": "510880", "side": "buy", "mode": "amount", "value": 5000}), "ok")
    chk("全部卖出 ETF", *call("POST", "/api/paper/order",
                             {"code": "510880", "side": "sell", "mode": "shares", "value": 99999}), "reject")
    st, d = call("GET", "/api/paper")
    chk("查询账户", st, d, "ok")

    # ---------- 4. 跟单：空仓 + 重复跟单 ----------
    print("\n【4】一键跟单（含重复跟单这种容易炸的路径）")
    call("POST", "/api/paper/reset", {})
    chk("空仓跟单", *call("POST", "/api/paper/plan", {}), "ok")
    st, s1 = call("GET", "/api/paper")
    n1 = len(s1.get("positions", []))
    print(f"      跟单后持仓 {n1} 只，现金 {s1.get('cash', 0):,.0f}")
    chk("重复跟单（已有持仓再点）", *call("POST", "/api/paper/plan", {}), "ok")
    st, s2 = call("GET", "/api/paper")
    n2 = len(s2.get("positions", []))
    print(f"      重复跟单后持仓 {n2} 只，现金 {s2.get('cash', 0):,.0f}")
    if n2 < n1:
        FAIL.append(f"重复跟单后持仓从 {n1} 变成 {n2}，疑似异常")
        print(f"      ✗ 重复跟单后持仓减少：{n1} → {n2}")
    if s2.get("cash", 0) < -1:
        FAIL.append(f"重复跟单后现金为负：{s2.get('cash')}")
        print(f"      ✗ 现金为负 {s2.get('cash')}")

    # ---------- 5. 持仓成本价补录 ----------
    print("\n【5】POST /api/positions（成本价补录，止损扫描依赖）")
    chk("空 body", *call("POST", "/api/positions", {}), "reject")
    chk("null", *call("POST", "/api/positions", None), "reject")
    # 正确格式：{code: {...}} 字典
    chk("字典格式（正确）", *call("POST", "/api/positions",
                                 {"601899": {"name": "紫金矿业", "cost": 33.35, "lots": 3}}), "ok")
    # 错误格式：数组 —— 曾经直接 500（d.items() 抛 AttributeError）
    chk("数组格式（曾 500）", *call("POST", "/api/positions",
                                   [{"code": "601899", "cost": 33.35}]), "reject")
    chk("字符串", *call("POST", "/api/positions", "hello"), "reject")
    chk("值为非字典", *call("POST", "/api/positions", {"601899": 123}), "reject")
    st, after = call("GET", "/api/positions")
    chk("读回 positions", st, after, "ok")
    if isinstance(after, dict) and "601899" in after:
        print(f"      601899 已写入 cost={after['601899'].get('cost')}")

    # ---------- 6. 刷新任务 ----------
    print("\n【6】POST /api/refresh（实际注册的任务：sentiment/mainfund/signal）")
    for job in ["sentiment", "mainfund", "signal", "nope"]:
        st, d = call("POST", "/api/refresh", {"what": job}, timeout=20)
        chk(f"refresh {job}", st, d, "ok" if job != "nope" else "reject")

    # ---------- 7. 复位 ----------
    print("\n【7】复位")
    chk("reset", *call("POST", "/api/paper/reset", {}), "ok")
    st, s = call("GET", "/api/paper")
    if len(s.get("positions", [])) != 0 or s.get("n_trades", 0) != 0:
        FAIL.append("reset 后仍有持仓或流水")
        print(f"      ✗ reset 未清干净：持仓 {len(s.get('positions', []))} 流水 {s.get('n_trades')}")
    else:
        print(f"      ✓ reset 后持仓 0 流水 0 现金 {s.get('cash', 0):,.0f}")

    # ---------- 汇总 ----------
    print("\n" + "=" * 74)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("\n失败明细：")
        for f in FAIL:
            print(f"  ✗ {f}")
        return 1
    print("结论：全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
