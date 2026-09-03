# -*- coding: utf-8 -*-
"""雪球：用 /hq 下发的【完整 cookie 串】打 API。

上一版失败原因：只手动带了 xq_a_token，丢掉 acw_tc 等，
且 stock.xueqiu.com 与 xueqiu.com 不同域，session 不会自动带过去。
"""
import json
import time
import warnings

warnings.filterwarnings("ignore")
from curl_cffi import requests as creq

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

APIS = [
    ("关注榜 portfolio", "https://stock.xueqiu.com/v5/stock/portfolio/stock/list.json?size=20&category=1&pid=-1"),
    ("热榜 hot_stock",   "https://stock.xueqiu.com/v5/stock/hot_stock/list.json?size=20&_type=10&type=10"),
    ("涨幅榜",           "https://stock.xueqiu.com/v5/stock/screener/quote/list.json?size=20&order=desc&order_by=percent&market=CN&type=sh_sz"),
    ("成交额榜",         "https://stock.xueqiu.com/v5/stock/screener/quote/list.json?size=20&order=desc&order_by=amount&market=CN&type=sh_sz"),
]


def get_cookie():
    s = creq.Session(impersonate="chrome124")
    r = s.get("https://xueqiu.com/hq", timeout=25,
              headers={"User-Agent": UA, "Referer": "https://xueqiu.com/"})
    ck = "; ".join(f"{k}={v}" for k, v in s.cookies.items())
    print(f"  /hq → HTTP {r.status_code}, cookie {len(ck)} 字, "
          f"xq_a_token={'有' if 'xq_a_token' in ck else '无'}")
    return ck


def call(ck, name, url):
    s = creq.Session(impersonate="chrome124")
    try:
        t0 = time.time()
        r = s.get(url, timeout=25, headers={
            "User-Agent": UA,
            "Referer": "https://xueqiu.com/hq",
            "Accept": "application/json, text/plain, */*",
            "Cookie": ck,
        })
        ms = int((time.time() - t0) * 1000)
        txt = r.text
        if txt.lstrip().startswith("<"):
            print(f"  [{name}] HTTP {r.status_code} {ms}ms 返回HTML(被拦)")
            return None
        j = r.json()
        if j.get("error_code"):
            print(f"  [{name}] {ms}ms error_code={j['error_code']} {j.get('error_description')}")
            return None
        data = j.get("data") or {}
        items = data.get("items") or data.get("list") or data.get("stocks") or []
        print(f"  [{name}] HTTP {r.status_code} {ms}ms ✓ 条目={len(items)}")
        if items:
            print("       样例:", json.dumps(items[0], ensure_ascii=False)[:280])
        return j
    except Exception as e:
        print(f"  [{name}] EXC {type(e).__name__}: {str(e)[:90]}")
        return None


if __name__ == "__main__":
    ck = get_cookie()
    if "xq_a_token" not in ck:
        raise SystemExit("未拿到 token，停止")
    print()
    ok = 0
    for name, url in APIS:
        if call(ck, name, url):
            ok += 1
    print(f"\n  成功 {ok}/{len(APIS)} 路")
