# -*- coding: utf-8 -*-
"""深挖雪球 xq_a_token 的获取路径。

现状：首页 200 可访问，API 返回 JSON 400016（"请刷新页面或重新登录"）
      → 说明不是 IP 黑名单，而是缺 xq_a_token 这个匿名 token。
"""
import json
import re
import time
import warnings

warnings.filterwarnings("ignore")
from curl_cffi import requests as creq

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
API = "https://stock.xueqiu.com/v5/stock/hot_stock/list.json?size=20&_type=10&type=10"


def dump_ck(s, tag):
    ck = {k: v for k, v in s.cookies.items()}
    tok = ck.get("xq_a_token")
    print(f"  [{tag}] cookie keys={list(ck.keys())} "
          f"xq_a_token={'有(' + tok[:14] + '…)' if tok else '无'}", flush=True)
    return tok


def try_paths():
    s = creq.Session(impersonate="chrome124")
    paths = [
        ("首页 /", "https://xueqiu.com/"),
        ("个股页 /S/SH600000", "https://xueqiu.com/S/SH600000"),
        ("热榜页 /hq", "https://xueqiu.com/hq"),
        ("讨论页 /today", "https://xueqiu.com/today"),
        ("行情中心", "https://xueqiu.com/quote"),
    ]
    for tag, url in paths:
        try:
            t0 = time.time()
            r = s.get(url, timeout=25, headers={"User-Agent": UA,
                                                "Referer": "https://xueqiu.com/"})
            ms = int((time.time() - t0) * 1000)
            tok = dump_ck(s, f"{tag} {r.status_code} {ms}ms")
            # 页面里也可能内联 token
            m = re.search(r'xq_a_token["\']?\s*[:=]\s*["\']([0-9a-f\-]{20,})', r.text)
            if m:
                print(f"      页面内联 token: {m.group(1)[:20]}…")
            if tok:
                return s, tok
        except Exception as e:
            print(f"  [{tag}] EXC {type(e).__name__}: {str(e)[:70]}", flush=True)
    return s, None


def call_api(s, tok=None):
    hdr = {"User-Agent": UA, "Referer": "https://xueqiu.com/",
           "Accept": "application/json, text/plain, */*",
           "X-Requested-With": "XMLHttpRequest"}
    if tok:
        hdr["Cookie"] = f"xq_a_token={tok}"
    try:
        t0 = time.time()
        r = s.get(API, timeout=25, headers=hdr)
        ms = int((time.time() - t0) * 1000)
        txt = r.text
        if txt.lstrip().startswith("<"):
            print(f"  API: HTTP {r.status_code} {ms}ms 返回HTML(被拦) {txt[:70]}")
            return None
        j = r.json()
        if j.get("error_code"):
            print(f"  API: HTTP {r.status_code} {ms}ms error_code={j['error_code']} "
                  f"{j.get('error_description')}")
            return None
        items = (j.get("data") or {}).get("items") or j.get("data") or []
        print(f"  API: HTTP {r.status_code} {ms}ms ✓ 条目={len(items)}")
        if items:
            print("      样例:", json.dumps(items[0], ensure_ascii=False)[:300])
        return j
    except Exception as e:
        print(f"  API: EXC {type(e).__name__}: {str(e)[:90]}")
        return None


if __name__ == "__main__":
    print("───── 雪球 token 深挖 ─────")
    s, tok = try_paths()
    print("\n  用当前 session 直接打 API：")
    got = call_api(s)
    if not got and tok:
        print("\n  用内联 token 显式打 API：")
        call_api(s, tok)
    print("\n  结论:", "雪球可通" if got else "仍缺 token / 被拦")
