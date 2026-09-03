# -*- coding: utf-8 -*-
"""财联社替代接法探测 + 消息面替代源覆盖率实测。

已知：akshare 的 stock_info_global_cls 会无限期挂起（实测 >3min），不能用。
"""
import re
import time
import warnings

warnings.filterwarnings("ignore")
from curl_cffi import requests as creq

try:
    import akshare as ak
except Exception:
    ak = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
VALID = ("600", "601", "603", "605", "000", "001", "002", "003", "300", "301")


def is_a(code):
    return len(code) == 6 and code.isdigit() and code.startswith(VALID) and code != "000000"


def probe_cls():
    print("───── 财联社 ─────")
    s = creq.Session(impersonate="chrome124")
    cands = [
        ("PC 电报页", "https://www.cls.cn/telegraph", "page"),
        ("移动电报页", "https://m.cls.cn/telegraph", "page"),
        ("nodeapi 免签", "https://www.cls.cn/nodeapi/telegraphList?app=CailianpressWeb&os=web&sv=7.7.5", "json"),
        ("v3 depth", "https://www.cls.cn/v3/depth/home/assembled/1000", "json"),
        ("api/sw", "https://www.cls.cn/api/sw?app=CailianpressWeb&os=web&sv=7.7.5", "json"),
    ]
    for name, url, kind in cands:
        try:
            t0 = time.time()
            r = s.get(url, timeout=20, headers={
                "User-Agent": UA, "Referer": "https://www.cls.cn/",
                "Accept": "application/json,text/html,*/*"})
            ms = int((time.time() - t0) * 1000)
            txt = r.text
            if kind == "json":
                try:
                    j = r.json()
                    s2 = __import__("json").dumps(j, ensure_ascii=False)
                    codes = {c for c in re.findall(r"\b(\d{6})\b", s2) if is_a(c)}
                    print(f"  [OK ] {name:<16} {ms:>5}ms JSON {len(txt)}字 代码{len(codes)}个 {sorted(codes)[:6]}")
                    if codes:
                        return True
                except Exception:
                    print(f"  [FAIL] {name:<16} {ms:>5}ms 非JSON {txt[:60]}")
            else:
                codes = {c for c in re.findall(r"\b(\d{6})\b", txt) if is_a(c)}
                nd = "__NEXT_DATA__" in txt
                sub = ""
                if nd:
                    m = re.search(r'__NEXT_DATA__\s*=\s*(\{.*?\})\s*</script>', txt, re.S)
                    if m:
                        try:
                            s2 = __import__("json").loads(m.group(1))
                            codes = {c for c in re.findall(r"\b(\d{6})\b", __import__("json").dumps(s2, ensure_ascii=False)) if is_a(c)}
                            sub = " (NEXT_DATA内)"
                        except Exception:
                            pass
                print(f"  [{'OK ' if codes or nd else 'FAIL'}] {name:<16} {ms:>5}ms len={len(txt)} "
                      f"NEXT_DATA={'有' if nd else '无'} 代码{len(codes)}个{sub} {sorted(codes)[:8]}")
                if codes:
                    return True
        except Exception as e:
            print(f"  [FAIL] {name:<16} {type(e).__name__}: {str(e)[:70]}")
    return False


def probe_news_alt():
    """消息面替代源：统计能提取到多少只 A 股（含简称→代码映射）。"""
    print("\n───── 消息面替代源 ─────")
    if ak is None:
        print("akshare 不可用"); return
    # 简称 → 代码
    n2c = {}
    try:
        df = ak.stock_info_a_code_name()
        for code, name in zip(df["code"], df["name"]):
            c = str(code).zfill(6)
            if not is_a(c):
                continue
            n = re.sub(r"\s+", "", str(name))
            if len(n) >= 3:
                n2c.setdefault(n, c)
        print(f"  简称索引 {len(n2c)} 条")
    except Exception as e:
        print("  简称索引失败", e)

    for name, fn, cols in [
        ("东财全球财经", "stock_info_global_em", ["标题", "摘要"]),
        ("新浪全球财经", "stock_info_global_sina", ["内容"]),
        ("同花顺要闻", "stock_info_global_ths", ["标题", "内容"]),
    ]:
        t0 = time.time()
        try:
            df = getattr(ak, fn)()
            ms = int((time.time() - t0) * 1000)
            hits = {}
            for _, row in df.iterrows():
                blob = " ".join(str(row.get(c, "")) for c in cols)
                for c in set(re.findall(r"\b(\d{6})\b", blob)):
                    if is_a(c):
                        hits.setdefault(c, row.get(cols[0], ""))
                for nm, c in n2c.items():
                    if nm in blob:
                        hits.setdefault(c, nm)
            print(f"  [OK ] {name:<12} {ms:>5}ms {df.shape} → 命中 {len(hits)} 只 "
                  f"{list(hits)[:8]}")
        except Exception as e:
            print(f"  [FAIL] {name:<12} {type(e).__name__}: {str(e)[:70]}")


if __name__ == "__main__":
    probe_cls()
    probe_news_alt()
