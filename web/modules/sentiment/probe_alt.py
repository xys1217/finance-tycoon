# -*- coding: utf-8 -*-
"""探测 5 路不可用数据源的替代接法。

目标（不改硬约束，只找能连通的路）：
  雪球 关注/讨论/交易   → IP 验证码墙，试「先取 cookie 再带 cookie 请求」
  财联社 电报           → 试移动端 / nodeapi 免签 / RSS
  同花顺 要闻           → 上次是 70s 超时，重试判断是否偶发
"""
import json
import re
import sys
import time
import warnings

warnings.filterwarnings("ignore")

try:
    from curl_cffi import requests as creq
except Exception:
    creq = None
try:
    import akshare as ak
except Exception:
    ak = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def show(tag, ok, msg, ms):
    flag = "OK " if ok else "FAIL"
    print(f"[{flag}] {tag:<28} {ms:>6}ms  {msg}", flush=True)


# ───────────────────────── 雪球 ─────────────────────────
def probe_xq():
    print("\n───── 雪球 ─────")
    if creq is None:
        print("curl_cffi 不可用"); return
    s = creq.Session(impersonate="chrome124")
    # 1) 先访问首页拿 cookie（很多站点靠这一步下发 token）
    try:
        t0 = time.time()
        r = s.get("https://xueqiu.com/", timeout=20, headers={"User-Agent": UA})
        ck = "; ".join(f"{k}={v}" for k, v in s.cookies.items())
        has_token = "xq_a_token" in ck
        show("xueqiu.com 首页", r.status_code == 200,
             f"HTTP {r.status_code} len={len(r.text)} cookie={len(ck)}字 "
             f"xq_a_token={'有' if has_token else '无'}",
             int((time.time()-t0)*1000))
        if has_token:
            print("      cookie:", ck[:160])
    except Exception as e:
        show("xueqiu.com 首页", False, f"{type(e).__name__}: {str(e)[:90]}", 0)
        return

    # 2) 带 cookie 打热榜 API
    apis = [
        ("热榜 hot_stock", "https://stock.xueqiu.com/v5/stock/hot_stock/list.json?size=20&_type=10&type=10"),
        ("关注榜", "https://stock.xueqiu.com/v5/stock/portfolio/stock/list.json?size=20&category=1"),
        ("雪球热度", "https://xueqiu.com/statuses/hots.json?size=20"),
    ]
    for name, url in apis:
        try:
            t0 = time.time()
            r = s.get(url, timeout=20, headers={
                "User-Agent": UA, "Referer": "https://xueqiu.com/",
                "Accept": "application/json"})
            ms = int((time.time()-t0)*1000)
            txt = r.text[:120].replace("\n", " ")
            is_html = txt.lstrip().startswith("<")
            if is_html:
                show(name, False, f"HTTP {r.status_code} 返回HTML(仍被拦) {txt[:60]}", ms)
            else:
                try:
                    j = r.json()
                    n = len(j.get("data", j.get("items", [])) or [])
                    show(name, True, f"HTTP {r.status_code} JSON ok 条目={n}", ms)
                    print("      样例:", json.dumps(j, ensure_ascii=False)[:260])
                except Exception:
                    show(name, False, f"HTTP {r.status_code} 非JSON {txt[:60]}", ms)
        except Exception as e:
            show(name, False, f"{type(e).__name__}: {str(e)[:90]}", 0)


# ───────────────────────── 财联社 ─────────────────────────
def probe_cls():
    print("\n───── 财联社 ─────")
    if creq is None:
        print("curl_cffi 不可用"); return
    s = creq.Session(impersonate="chrome124")
    cands = [
        ("移动端电报 m.cls.cn", "https://m.cls.cn/telegraph", None),
        ("免签 nodeapi", "https://www.cls.cn/nodeapi/telegraphList?app=CailianpressWeb&os=web&sv=7.7.5", "json"),
        ("v1 roll 列表", "https://www.cls.cn/v1/roll/get_roll_list?app=CailianpressWeb&os=web&sv=7.7.5&last_time=0&os=web&refresh_type=1&rn=20", "json"),
        ("RSS", "https://www.cls.cn/rss.xml", None),
    ]
    for name, url, kind in cands:
        try:
            t0 = time.time()
            r = s.get(url, timeout=20, headers={
                "User-Agent": UA, "Referer": "https://www.cls.cn/",
                "Accept": "application/json,text/html,*/*"})
            ms = int((time.time()-t0)*1000)
            txt = r.text
            if kind == "json":
                try:
                    j = r.json()
                    items = (j.get("data") or {}).get("roll_data") or j.get("data") or []
                    show(name, bool(items), f"HTTP {r.status_code} JSON 条目={len(items) if hasattr(items,'__len__') else '?'}", ms)
                    if items and isinstance(items, list):
                        it = items[0]
                        print("      样例:", str({k: it.get(k) for k in ("title","brief","ctime","stock_list") if k in it})[:240])
                    return
                except Exception:
                    show(name, False, f"HTTP {r.status_code} 非JSON {txt[:70]}", ms)
            else:
                # 页面：看能不能挖到 __NEXT_DATA__ 或正文
                has_next = "__NEXT_DATA__" in txt
                codes = set(re.findall(r"\b(6\d{5}|0\d{5}|3\d{5})\b", txt))
                show(name, has_next or len(codes) > 0,
                     f"HTTP {r.status_code} len={len(txt)} __NEXT_DATA__={'有' if has_next else '无'} 代码={len(codes)}个", ms)
                if has_next:
                    m = re.search(r"__NEXT_DATA__\s*=\s*(\{.*?\})\s*</script>", txt, re.S)
                    if m:
                        try:
                            j = json.loads(m.group(1))
                            s2 = json.dumps(j, ensure_ascii=False)
                            c2 = set(re.findall(r"\b(6\d{5}|0\d{5}|3\d{5})\b", s2))
                            print(f"      NEXT_DATA 内代码 {len(c2)} 个: {sorted(c2)[:12]}")
                            return
                        except Exception as e:
                            print("      解析失败", e)
        except Exception as e:
            show(name, False, f"{type(e).__name__}: {str(e)[:90]}", 0)


# ───────────────────────── 同花顺 ─────────────────────────
def probe_ths():
    print("\n───── 同花顺 ─────")
    if ak is None:
        print("akshare 不可用"); return
    for name, fn in [("stock_info_global_ths", "stock_info_global_ths"),
                     ("stock_info_global_sina", "stock_info_global_sina"),
                     ("stock_info_global_em", "stock_info_global_em"),
                     ("stock_info_global_cls", "stock_info_global_cls")]:
        t0 = time.time()
        try:
            df = getattr(ak, fn)()
            ms = int((time.time()-t0)*1000)
            show(name, len(df) > 0, f"{df.shape} cols={list(df.columns)[:6]}", ms)
        except Exception as e:
            show(name, False, f"{type(e).__name__}: {str(e)[:90]}",
                 int((time.time()-t0)*1000))


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "xq"):  probe_xq()
    if which in ("all", "cls"): probe_cls()
    if which in ("all", "ths"): probe_ths()
