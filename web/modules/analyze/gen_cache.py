# -*- coding: utf-8 -*-
"""为静态离线版预生成个股分析快照 —— 让模块3 在没有 server.py 时也能查热门股。

选股池 = 情绪共振榜 Top + 主力净流入/流出 Top + 量化持仓（防御仓4 + 热点仓1 + ETF 代码）
每只独立子进程跑，单只超时 45s，避免一只卡死拖垮整批。
输出 /workspace/web/api/analyze_cache.json
"""
import json
import os
import re
import subprocess
import sys

WEB = "/workspace/web"
API = f"{WEB}/api"
HERE = os.path.dirname(os.path.abspath(__file__))


def L(name):
    p = f"{API}/{name}"
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def pick_codes():
    codes = []

    s = L("sentiment_status.json")
    for r in (s.get("merged") or [])[:30]:
        c = r.get("code")
        if c:
            codes.append(c)

    m = L("mainfund.json")
    fu = m.get("fund") or {}
    for r in (fu.get("top_in") or [])[:10]:
        c = str(r.get("代码") or r.get("股票代码") or "").zfill(6)
        if c:
            codes.append(c)
    for r in (fu.get("top_out") or [])[:5]:
        c = str(r.get("代码") or r.get("股票代码") or "").zfill(6)
        if c:
            codes.append(c)

    q = L("quant_partial.json")
    for c in re.findall(r"\b(\d{6})\b", q.get("html", "")):
        codes.append(c)

    # 保底：v5 当前持仓（防御仓4银行 + 热点仓招商轮船）
    codes += ["601398", "601288", "600016", "601166", "601872"]

    seen, out = set(), []
    for c in codes:
        c = str(c).zfill(6)
        # 只留沪深主板/中小/创业，剔除科创688、北交所、ETF(5xx/1xx)
        if not re.match(r"^(60|00|30)", c):
            continue
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def main():
    codes = pick_codes()
    print(f"待分析 {len(codes)} 只：{' '.join(codes[:20])}{' ...' if len(codes) > 20 else ''}")

    cache, fail = {}, []
    for i, c in enumerate(codes, 1):
        try:
            p = subprocess.run(
                [sys.executable, f"{HERE}/analyze_stock.py", c],
                capture_output=True, text=True, timeout=45,
            )
            raw = p.stdout.strip()
            if not raw:
                raise RuntimeError("空输出 " + (p.stderr.strip()[:80] or ""))
            d = json.loads(raw)
            if d.get("name") is None and not d.get("score"):
                raise RuntimeError("无效结果")
            cache[c] = d
            nm = d.get("name") or "?"
            sc = d.get("score")
            print(f"  [{i}/{len(codes)}] {c} {nm} 评分={sc} ✓")
        except Exception as e:
            fail.append((c, str(e)[:60]))
            print(f"  [{i}/{len(codes)}] {c} ✗ {str(e)[:60]}")

    json.dump(cache, open(f"{API}/analyze_cache.json", "w", encoding="utf-8"),
              ensure_ascii=False)
    print(f"\n写入 api/analyze_cache.json：成功 {len(cache)} 只，失败 {len(fail)} 只")
    for c, e in fail:
        print(f"  失败 {c}: {e}")


if __name__ == "__main__":
    main()
