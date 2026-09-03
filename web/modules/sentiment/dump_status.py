# -*- coding: utf-8 -*-
"""
逐源采集（子进程隔离，单源卡死不影响全局）→ 写 web/api/sentiment_status.json
用法:
    python3 dump_status.py              # 采集全部 14 路
    python3 dump_status.py --timeout 90 # 自定义单源超时(秒)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(os.path.join(HERE, "..", "..", "api", "sentiment_status.json"))

SOURCES = [
    "em_comment", "em_hot_rank", "em_notice",
    "xq_follow", "xq_tweet", "xq_deal",
    "cls_news", "em_news", "ths_news", "sina_news", "jin10",
    "gelonghui", "taoguba", "jiuyangongshe",
]

LABEL = {
    "em_comment": "东方财富·股吧关注度",
    "em_hot_rank": "东方财富·人气榜",
    "em_notice": "东方财富·公告热度(巨潮生态)",
    "xq_follow": "雪球·关注榜",
    "xq_tweet": "雪球·讨论榜",
    "xq_deal": "雪球·交易榜",
    "cls_news": "财联社·电报提及",
    "em_news": "东方财富·全球财经(消息面主力)",
    "ths_news": "同花顺·要闻提及",
    "sina_news": "新浪·全球财经",
    "jin10": "金十数据·快讯",
    "gelonghui": "格隆汇·页面热点",
    "taoguba": "淘股吧·页面热点",
    "jiuyangongshe": "韭研公社·页面热点",
}

# 合并榜用的短名（3 个东财源要区分开，否则显示成「东方财富·东方财富·东方财富」）
SHORT = {
    "em_comment": "股吧", "em_hot_rank": "人气榜", "em_notice": "公告",
    "xq_follow": "雪球关注", "xq_tweet": "雪球讨论", "xq_deal": "雪球交易",
    "cls_news": "财联社", "em_news": "东财财经", "ths_news": "同花顺",
    "sina_news": "新浪财经", "jin10": "金十",
    "gelonghui": "格隆汇", "taoguba": "淘股吧", "jiuyangongshe": "韭研公社",
}


def run_one(name: str, timeout: int) -> dict:
    """单源跑一个独立子进程，避免全局被拖死。"""
    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, "-u", os.path.join(HERE, "fetch_sources.py"), "--only", name],
            capture_output=True, text=True, timeout=timeout, cwd=HERE,
            env={**os.environ, "TQDM_DISABLE": "1"},
        )
        for line in p.stdout.splitlines():
            if line.startswith("@@JSON@@"):
                return json.loads(line[8:])
        return {"name": name, "label": LABEL[name], "ok": False, "rows": 0,
                "ms": int((time.time() - t0) * 1000),
                "error": (p.stderr or "无输出").strip()[-200:], "covered": 0, "top": {}}
    except subprocess.TimeoutExpired:
        return {"name": name, "label": LABEL[name], "ok": False, "rows": 0,
                "ms": int(timeout * 1000), "error": f"超时 {timeout}s（源无响应）",
                "covered": 0, "top": {}}
    except Exception as e:
        return {"name": name, "label": LABEL[name], "ok": False, "rows": 0,
                "ms": int((time.time() - t0) * 1000),
                "error": f"{type(e).__name__}: {e}"[:200], "covered": 0, "top": {}}


def main():
    timeout = 70
    if "--timeout" in sys.argv:
        timeout = int(sys.argv[sys.argv.index("--timeout") + 1])

    status: dict = {}
    top: dict = {}
    scores: dict[str, dict[str, float]] = {}   # code -> {src: score}

    print(f"采集 {len(SOURCES)} 路数据源（单源超时 {timeout}s）", flush=True)
    for i, name in enumerate(SOURCES, 1):
        r = run_one(name, timeout)
        status[name] = {k: r[k] for k in ("ok", "rows", "ms", "error")}
        status[name]["label"] = LABEL[name]
        status[name]["covered"] = r.get("covered", 0)
        top[name] = r.get("top", {})
        flag = "✅" if r["ok"] and r["covered"] > 0 else ("⚠️" if r["ok"] else "❌")
        print(f"  [{i:>2}/{len(SOURCES)}] {flag} {LABEL[name]:<24} "
              f"{r['rows']:>5}条 {r['ms']:>6}ms 覆盖{r.get('covered',0):>5}  "
              f"{(r.get('error') or '')[:40]}", flush=True)

        # 用全量分数字典合并（top 仅用于前端展示）
        for code, s in (r.get("all") or r.get("top") or {}).items():
            scores.setdefault(code, {})[name] = s

    # 合并：跨源等权平均
    merged = []
    for code, d in scores.items():
        merged.append({
            "code": code,
            "score": sum(d.values()) / len(d),
            "nsrc": len(d),
            "srcs": [SHORT.get(k, k) for k in d],
        })
    # 多源共振优先：被 N 个源同时点名，比单源排名更高更有信号价值
    merged.sort(key=lambda x: (-x["nsrc"], -x["score"]))

    out = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "meta": {"universe": len(scores), "sources": len(SOURCES)},
        "status": status,
        "top": top,
        "merged": merged[:60],
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    ok = sum(1 for s in status.values() if s["ok"] and s["covered"] > 0)
    print(f"\n结果：{ok}/{len(SOURCES)} 路有效产出 → {OUT}")
    print(f"覆盖个股 {len(scores)} 只（基于各源 Top 合并）")


if __name__ == "__main__":
    main()
