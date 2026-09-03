# -*- coding: utf-8 -*-
"""
情绪/资金流数据源采集器 —— 12 路信号
用法:
    python3 fetch_sources.py            # 探测全部源连通性
    python3 fetch_sources.py --json     # 输出 JSON 结果
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from typing import Dict, List

import numpy as np
import pandas as pd

try:
    import akshare as ak
except Exception:
    ak = None

try:
    from curl_cffi import requests as creq
except Exception:
    creq = None


# ---------------------------------------------------------------- 基础工具
# 6 位股票代码（前后不得再跟数字，避免误吃日期/金额）
CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# 可交易的 A 股代码段（严格白名单）
# 沪主板 600/601/603/605 | 深主板 000/001/002/003 | 创业板 300/301
# 剔除：科创板 688/689、北交所 4xx/8xx/920、B 股 900/200
VALID_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003", "300", "301")


def _is_a_share_code(code: str) -> bool:
    """严格校验：必须是真实存在的 A 股代码段。
    宽松匹配会把页面里的 ID、时间戳、金额（606266 / 000000 / 349813）当成股票。"""
    if not (isinstance(code, str) and len(code) == 6 and code.isdigit()):
        return False
    if code == "000000":
        return False
    return code.startswith(VALID_PREFIX)


def _norm_code(raw) -> str:
    """从 'sh600519' / '600519.SH' / '600519' 等提取 6 位纯数字代码。"""
    if raw is None:
        return ""
    s = str(raw).strip()
    m = CODE_RE.search(s)
    return m.group(1) if m else ""


def _value_to_score(series: pd.Series) -> pd.Series:
    """把任意数值列归一化到 0~1（用百分位排名，抗极值）。"""
    v = pd.to_numeric(series, errors="coerce")
    if v.notna().sum() == 0:
        return pd.Series(np.zeros(len(series)), index=series.index)
    return v.rank(pct=True).fillna(0.0).clip(0, 1)


def _rank_to_score(rank: int, n: int) -> float:
    """第 rank 名（1-based）/ 共 n 名 → 0~1，第 1 名得 1.0。"""
    if n <= 0 or rank <= 0:
        return 0.0
    return float((n - rank + 1) / n)


def _safe_call(kind: str, fn, status: Dict[str, dict]):
    """包一层 akshare 调用，失败只记账不抛异常。"""
    t0 = time.time()
    try:
        df = fn()
        if df is None or (hasattr(df, "empty") and df.empty):
            raise RuntimeError("空数据")
        _mark(status, kind, True, rows=len(df), ms=int((time.time() - t0) * 1000))
        return df
    except Exception as e:
        _mark(status, kind, False, error=f"{type(e).__name__}: {e}"[:240], ms=int((time.time() - t0) * 1000))
        return None


def _mark(status: Dict[str, dict], name: str, ok: bool, rows: int = 0,
          error: str | None = None, ms: int = 0):
    status[name] = {
        "ok": ok,
        "rows": rows,
        "ms": ms,
        "error": error,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- 采集器
def _collect_em_comment(status: Dict[str, dict]) -> Dict[str, float]:
    """东方财富股吧关注度 / 评论情绪。"""
    if ak is None:
        status["em_comment"] = {"ok": False, "error": "no akshare", "rows": 0, "ms": 0}
        return {}
    df = _safe_call("em_comment", ak.stock_comment_em, status)
    if df is None:
        return {}
    code_col = "代码" if "代码" in df.columns else df.columns[1]
    focus_col = "关注指数" if "关注指数" in df.columns else None
    score_col = "综合得分" if "综合得分" in df.columns else None
    rank_col = "目前排名" if "目前排名" in df.columns else None

    out: Dict[str, float] = {}
    tmp = pd.DataFrame({"code": df[code_col].map(_norm_code)})
    parts = []
    if focus_col:
        parts.append(_value_to_score(df[focus_col]))
    if score_col:
        parts.append(_value_to_score(df[score_col]))
    if rank_col:
        ranks = pd.to_numeric(df[rank_col], errors="coerce")
        n = int(ranks.max()) if ranks.notna().any() else len(df)
        parts.append(ranks.map(lambda r: _rank_to_score(int(r), n) if pd.notna(r) else 0.0))
    if not parts:
        return {}
    tmp["s"] = sum(parts) / len(parts)
    for code, s in zip(tmp["code"], tmp["s"]):
        if code:
            out[code] = float(s)
    return out


def _collect_xq(kind: str, status: Dict[str, dict]) -> Dict[str, float]:
    """雪球 关注 / 讨论 / 成交 三路。"""
    if ak is None:
        status[kind] = {"ok": False, "error": "no akshare", "rows": 0, "ms": 0}
        return {}
    fn_map = {
        "xq_follow": lambda: ak.stock_hot_follow_xq(symbol="最热门"),
        "xq_tweet": lambda: ak.stock_hot_tweet_xq(symbol="最热门"),
        "xq_deal": lambda: ak.stock_hot_deal_xq(symbol="最热门"),
    }
    t0 = time.time()
    try:
        df = fn_map[kind]()
        if df is None or df.empty:
            raise RuntimeError("空数据")
    except Exception as e:
        # 实测定位（2026-09-03）：
        #  xueqiu.com/hq 页面可访问，并能下发 xq_a_token（说明不是 IP 黑名单）；
        #  但 stock.xueqiu.com 的 JSON API 一律返回带 geetest/captcha 的 HTML，
        #  即极验行为验证码墙 —— 与 token 无关，自动化环境过不去，需住宅出口 IP 或付费接口。
        _mark(status, kind, False,
              error="雪球 stock.xueqiu.com 走极验(geetest)行为验证码墙："
                    "已验证 xueqiu.com/hq 能下发 xq_a_token，故非 IP 封禁，"
                    "是 API 侧的滑块验证。需住宅出口 IP / 人工过一次验证 / 付费接口。"
                    f"（{type(e).__name__}）",
              ms=int((time.time() - t0) * 1000))
        status[kind]["blocked"] = True
        return {}
    _mark(status, kind, True, rows=len(df), ms=int((time.time() - t0) * 1000))
    code_col = "股票代码" if "股票代码" in df.columns else df.columns[0]
    val_col = "关注" if "关注" in df.columns else df.columns[2]
    tmp = pd.DataFrame({
        "code": df[code_col].map(_norm_code),
        "v": pd.to_numeric(df[val_col], errors="coerce"),
    }).dropna()
    # 取前 500 名做排名分，避免全市场尾部噪声
    tmp = tmp.sort_values("v", ascending=False).head(500).reset_index(drop=True)
    tmp["s"] = [_rank_to_score(i + 1, len(tmp)) for i in range(len(tmp))]
    return {c: float(s) for c, s in zip(tmp["code"], tmp["s"]) if c}


_NAME2CODE: Dict[str, str] = {}


def _load_name_index() -> Dict[str, str]:
    """全市场「公司简称 → 代码」映射。
    新闻里写的是「九州一轨」而不是「688485」，只匹配 6 位代码会永远零覆盖。

    这里只需读全局字典（setdefault 是调用方法、不是重新绑定变量），
    所以不需要 `global` 声明 —— 加了反而被 pyflakes 判为冗余。
    """
    if _NAME2CODE or ak is None:
        return _NAME2CODE
    try:
        df = ak.stock_info_a_code_name()
        for code, name in zip(df["code"], df["name"]):
            c = str(code).zfill(6)
            if not _is_a_share_code(c):
                continue
            n = re.sub(r"\s+", "", str(name))       # '万  科Ａ' → '万科Ａ'
            if len(n) < 3:                          # 2 字简称误匹配率过高，跳过
                continue
            _NAME2CODE.setdefault(n, c)
    except Exception:
        pass
    return _NAME2CODE


def _mentions_from_text(text: str) -> set:
    """单条文本 → 命中的股票代码集合（代码 + 公司简称双通道）。"""
    out = set()
    for c in CODE_RE.findall(text):
        if _is_a_share_code(c):
            out.add(c)
    idx = _load_name_index()
    if idx:
        for n, c in idx.items():
            if n in text:
                out.add(c)
    return out


def _mentions_from_news(df: pd.DataFrame, text_cols: List[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for _, row in df.iterrows():
        text = " ".join(str(row.get(c, "") or "") for c in text_cols)
        for c in _mentions_from_text(text):
            counts[c] = counts.get(c, 0) + 1
    return counts


def _fetch_cls_text(status: Dict[str, dict]):
    """财联社电报正文。页面是 SPA 空壳，正文由带签名的 XHR 提供，抓不到即判定不可用。"""
    t0 = time.time()
    if creq is None:
        _mark(status, "cls_news", False, error="缺少 curl_cffi")
        status["cls_news"]["blocked"] = True
        return None
    try:
        rr = creq.get("https://www.cls.cn/telegraph", timeout=20, impersonate="chrome124",
                      headers={"Referer": "https://www.cls.cn/"})
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                      rr.text, re.S)
        parts: List[str] = []
        if m:
            def walk(o):
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k in ("title", "content", "brief") and isinstance(v, str):
                            parts.append(v)
                        else:
                            walk(v)
                elif isinstance(o, list):
                    for x in o:
                        walk(x)
            walk(json.loads(m.group(1)))
        if parts:
            _mark(status, "cls_news", True, rows=len(parts), ms=int((time.time() - t0) * 1000))
            return " ".join(parts)
        _mark(status, "cls_news", False, ms=int((time.time() - t0) * 1000),
              error="财联社电报正文走带签名 XHR（sign 参数），页面为 SPA 空壳，需签名算法或付费接口")
        status["cls_news"]["blocked"] = True
        return None
    except Exception as e:
        _mark(status, "cls_news", False, ms=int((time.time() - t0) * 1000),
              error=f"{type(e).__name__}: {e}"[:240])
        status["cls_news"]["blocked"] = True
        return None


# 消息面源：akshare 函数名 → 参与匹配的文本列
# 东财全球财经实测 200 条、可命中 39 只，是消息面覆盖最好的一路，
# 用来顶替抓不到的财联社（财联社正文走带签名 XHR，5 种接法全失败）。
NEWS_SOURCES = {
    "em_news":   ("stock_info_global_em",   ["标题", "摘要"]),
    "ths_news":  ("stock_info_global_ths",  ["标题", "内容"]),
    "sina_news": ("stock_info_global_sina", ["内容"]),
}


def _collect_news_mentions(kind: str, status: Dict[str, dict],
                           retries: int = 1) -> Dict[str, float]:
    """财联社电报 / 东财全球财经 / 同花顺要闻 / 新浪全球财经 的个股提及 → 排名分。

    同花顺实测偶发 70s 无响应（下一次 104ms 就通），所以带一次重试。
    """
    if ak is None:
        status[kind] = {"ok": False, "error": "no akshare", "rows": 0, "ms": 0}
        return {}
    if kind == "cls_news":
        # akshare 的 stock_info_global_cls 实测会无限期挂起（>3min 无响应），
        # 改为直连财联社电报页；该页是 SPA 空壳，正文走带签名的 XHR，抓不到即失败。
        text = _fetch_cls_text(status)
        if text is None:
            return {}
        counts: Dict[str, int] = {}
        for c in _mentions_from_text(text):
            counts[c] = counts.get(c, 0) + 1
        return _counts_to_rank_scores(counts)

    fn_name, cols = NEWS_SOURCES[kind]
    fn = getattr(ak, fn_name, None)
    if fn is None:
        _mark(status, kind, False, error=f"akshare 无 {fn_name}")
        return {}
    df = None
    for i in range(retries + 1):
        df = _safe_call(kind, fn, status)
        if df is not None:
            break
        if i < retries:
            time.sleep(1.5)
    if df is None:
        return {}
    cols = [c for c in cols if c in df.columns] or [df.columns[0]]
    counts = _mentions_from_news(df, cols)
    if not counts:
        return {}
    items = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    n = len(items)
    return {code: _rank_to_score(i + 1, n) for i, (code, _) in enumerate(items)}


def _counts_to_rank_scores(counts: Dict[str, int]) -> Dict[str, float]:
    if not counts:
        return {}
    items = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    n = len(items)
    return {code: _rank_to_score(i + 1, n) for i, (code, _) in enumerate(items)}


def _collect_em_hot_rank(status: Dict[str, dict]) -> Dict[str, float]:
    """东方财富人气榜（emappdata）。"""
    t0 = time.time()
    if creq is None:
        _mark(status, "em_hot_rank", False, error="缺少 curl_cffi")
        return {}
    try:
        rr = creq.post(
            "https://emappdata.eastmoney.com/stockrank/getAllCurrentList",
            json={
                "appId": "appId01",
                "globalId": "786e4c21-70dc-435a-93bb-38",
                "marketType": "",
                "pageNo": 1,
                "pageSize": 100,
            },
            timeout=15,
            impersonate="chrome124",
        )
        js = rr.json()
        data = js.get("data") or []
        if not data:
            raise RuntimeError(js.get("message") or "人气榜空数据")
        out = {}
        n = len(data)
        for row in data:
            code = _norm_code(row.get("sc"))
            rk = int(row.get("rk") or 0)
            if code and rk > 0 and _is_a_share_code(code):
                out[code] = _rank_to_score(rk, n)
        _mark(status, "em_hot_rank", True, rows=len(out), ms=int((time.time() - t0) * 1000))
        return out
    except Exception as e:
        _mark(status, "em_hot_rank", False, error=f"{type(e).__name__}: {e}"[:240], ms=int((time.time() - t0) * 1000))
        return {}


def _collect_em_notice(status: Dict[str, dict]) -> Dict[str, float]:
    """当日公告热度（东财公告列表，覆盖巨潮披露生态）。"""
    if ak is None:
        _mark(status, "em_notice", False, error="no akshare")
        return {}
    t0 = time.time()
    try:
        day = datetime.now().strftime("%Y%m%d")
        df = ak.stock_notice_report(symbol="全部", date=day)
        if df is None or df.empty:
            raise RuntimeError("公告空数据")
        code_col = "代码" if "代码" in df.columns else df.columns[0]
        counts: Dict[str, int] = {}
        for raw in df[code_col].tolist():
            code = _norm_code(raw)
            if code and _is_a_share_code(code):
                counts[code] = counts.get(code, 0) + 1
        out = _counts_to_rank_scores(counts)
        _mark(status, "em_notice", True, rows=len(out), ms=int((time.time() - t0) * 1000))
        return out
    except Exception as e:
        _mark(status, "em_notice", False, error=f"{type(e).__name__}: {e}"[:240], ms=int((time.time() - t0) * 1000))
        return {}


def _collect_jin10(status: Dict[str, dict]) -> Dict[str, float]:
    """金十数据快讯。"""
    t0 = time.time()
    if creq is None:
        _mark(status, "jin10", False, error="缺少 curl_cffi")
        return {}
    try:
        rr = creq.get(
            f"https://www.jin10.com/flash_newest.js?t={int(time.time())}",
            timeout=15,
            impersonate="chrome124",
        )
        m = re.search(r"var newest = (\[.*\])\s*;?", rr.text, re.S)
        if not m:
            raise RuntimeError("金十快讯解析失败")
        data = json.loads(m.group(1))
        counts: Dict[str, int] = {}
        for it in data:
            d = it.get("data") or {}
            text = f"{d.get('title') or ''} {d.get('content') or ''}"
            for code in CODE_RE.findall(text):
                if _is_a_share_code(code):
                    counts[code] = counts.get(code, 0) + 1
        out = _counts_to_rank_scores(counts)
        # 即使无股票代码，也算接通成功（宏观快讯本身有用）
        _mark(
            status,
            "jin10",
            True,
            rows=len(out),
            ms=int((time.time() - t0) * 1000),
            error=None if out else "快讯已通，但本批无A股代码提及",
        )
        return out
    except Exception as e:
        _mark(status, "jin10", False, error=f"{type(e).__name__}: {e}"[:240], ms=int((time.time() - t0) * 1000))
        return {}


def _collect_page_codes(name: str, url: str, status: Dict[str, dict]) -> Dict[str, float]:
    """从资讯站首页/直播页抽取股票代码（弱信号）。"""
    t0 = time.time()
    if creq is None:
        _mark(status, name, False, error="缺少 curl_cffi")
        return {}
    try:
        rr = creq.get(url, timeout=15, impersonate="chrome124")
        if rr.status_code >= 400:
            raise RuntimeError(f"HTTP {rr.status_code}")
        counts: Dict[str, int] = {}
        for code in CODE_RE.findall(rr.text or ""):
            if _is_a_share_code(code):
                counts[code] = counts.get(code, 0) + 1
        out = _counts_to_rank_scores(counts)
        if not out:
            raise RuntimeError("页面未解析到有效股票代码")
        _mark(status, name, True, rows=len(out), ms=int((time.time() - t0) * 1000))
        return out
    except Exception as e:
        _mark(status, name, False, error=f"{type(e).__name__}: {e}"[:240], ms=int((time.time() - t0) * 1000))
        return {}


def _mark_blocked(status: Dict[str, dict], name: str, reason: str) -> Dict[str, float]:
    status[name] = {
        "ok": False,
        "rows": 0,
        "ms": 0,
        "error": reason,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "blocked": True,
    }
    return {}


# ---------------------------------------------------------------- 编排
PAGE_SOURCES = {
    "gelonghui": "https://www.gelonghui.com/",
    "taoguba": "https://www.taoguba.com.cn/",
    "jiuyangongshe": "https://www.jiuyangongshe.com/",
}

SOURCE_LABEL = {
    "em_comment": "东方财富·股吧关注度",
    "em_hot_rank": "东方财富·人气榜",
    "em_notice": "东方财富·公告热度(巨潮生态)",
    "xq_follow": "雪球·关注",
    "xq_tweet": "雪球·讨论",
    "xq_deal": "雪球·成交",
    "cls_news": "财联社·电报提及",
    "em_news": "东方财富·全球财经(消息面主力)",
    "ths_news": "同花顺·要闻提及",
    "sina_news": "新浪·全球财经",
    "jin10": "金十数据·快讯",
    "gelonghui": "格隆汇·页面热点",
    "taoguba": "淘股吧·页面热点",
    "jiuyangongshe": "韭研公社·页面热点",
}


def collect_all() -> tuple[Dict[str, Dict[str, float]], Dict[str, dict]]:
    """跑全部 12 路采集器，返回 (每源的代码→分数, 连通性状态)。"""
    status: Dict[str, dict] = {}
    data: Dict[str, Dict[str, float]] = {}

    def run(name, fn):
        try:
            d = fn()
            data[name] = d
        except Exception as e:
            _mark(status, name, False, error=f"{type(e).__name__}: {e}"[:240])
            data[name] = {}

    run("em_comment", lambda: _collect_em_comment(status))
    run("em_hot_rank", lambda: _collect_em_hot_rank(status))
    run("em_notice", lambda: _collect_em_notice(status))
    run("xq_follow", lambda: _collect_xq("xq_follow", status))
    run("xq_tweet", lambda: _collect_xq("xq_tweet", status))
    run("xq_deal", lambda: _collect_xq("xq_deal", status))
    run("cls_news", lambda: _collect_news_mentions("cls_news", status))
    run("em_news", lambda: _collect_news_mentions("em_news", status))
    run("ths_news", lambda: _collect_news_mentions("ths_news", status))
    run("sina_news", lambda: _collect_news_mentions("sina_news", status))
    run("jin10", lambda: _collect_jin10(status))
    for name, url in PAGE_SOURCES.items():
        run(name, lambda n=name, u=url: _collect_page_codes(n, u, status))

    return data, status


RUNNERS = {
    "em_comment": lambda: _collect_em_comment(ST),
    "em_hot_rank": lambda: _collect_em_hot_rank(ST),
    "em_notice": lambda: _collect_em_notice(ST),
    "xq_follow": lambda: _collect_xq("xq_follow", ST),
    "xq_tweet": lambda: _collect_xq("xq_tweet", ST),
    "xq_deal": lambda: _collect_xq("xq_deal", ST),
    "cls_news": lambda: _collect_news_mentions("cls_news", ST),
    "em_news": lambda: _collect_news_mentions("em_news", ST),
    "ths_news": lambda: _collect_news_mentions("ths_news", ST),
    "sina_news": lambda: _collect_news_mentions("sina_news", ST),
    "jin10": lambda: _collect_jin10(ST),
    **{n: (lambda u=u, n=n: _collect_page_codes(n, u, ST)) for n, u in PAGE_SOURCES.items()},
}

ST: Dict[str, dict] = {}


if __name__ == "__main__":
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]

    if only:
        # 单源探测模式：输出一行 JSON，供外层逐个调用
        t0 = time.time()
        try:
            d = RUNNERS[only]()
            st = ST.get(only, {})
            ok = bool(st.get("ok"))
            print("@@JSON@@" + json.dumps({
                "name": only,
                "label": SOURCE_LABEL[only],
                "ok": ok,
                "rows": st.get("rows", 0),
                "ms": st.get("ms", 0),
                "error": st.get("error"),
                "covered": len(d),
                "top": dict(sorted(d.items(), key=lambda x: -x[1])[:8]),
                "all": d,
            }, ensure_ascii=False))
        except Exception as e:
            print("@@JSON@@" + json.dumps({
                "name": only, "label": SOURCE_LABEL.get(only, only),
                "ok": False, "rows": 0, "ms": int((time.time() - t0) * 1000),
                "error": f"{type(e).__name__}: {e}"[:240], "covered": 0, "top": {},
            }, ensure_ascii=False))
        sys.exit(0)

    print(f"akshare: {'OK ' + ak.__version__ if ak else '缺失'}")
    print(f"curl_cffi: {'OK' if creq else '缺失'}")
    print("=" * 72)

    data, status = collect_all()

    print(f"{'数据源':<30} {'状态':<6} {'条数':>6} {'耗时':>8}  说明")
    print("-" * 72)
    for name in SOURCE_LABEL:
        st = status.get(name, {})
        ok = st.get("ok")
        flag = "✅通" if ok else "❌挂"
        err = st.get("error") or ""
        print(f"{SOURCE_LABEL[name]:<26} {flag:<6} {st.get('rows', 0):>6} {str(st.get('ms', 0)) + 'ms':>8}  {err[:38]}")

    ok_n = sum(1 for s in status.values() if s.get("ok"))
    print("-" * 72)
    print(f"接通 {ok_n}/{len(SOURCE_LABEL)} 路")

    if "--json" in sys.argv:
        out = {
            "status": status,
            "top": {k: dict(sorted(v.items(), key=lambda x: -x[1])[:10]) for k, v in data.items()},
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
