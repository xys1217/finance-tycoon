# -*- coding: utf-8 -*-
"""每日信号生成器（v5 定案配置）。

口径铁律（踩过的坑，别再犯）：
  1. 打分与约束一律用 bar_date（最后一个【完整交易日】收盘），
     绝不能用 sig_date。盘中运行时，若 K 线最后一根是今天，必须剔除，
     否则三道闸门会因盘中价失真而失效。
  2. 剔除无权限板块：科创板 688/689、北交所 4xx/8xx、B 股 9xx
  3. 个股股价 ≤ 120 元（20 万本金买不起高价股 1 手）
  4. 整手约束 100 股/份，成本：佣金 0.025%（最低 5 元）+ 滑点 0.1%
     + 卖出印花税 0.05%（ETF 免）

用法:
    python3 daily_signal.py                # 生成下一交易日信号
    python3 daily_signal.py --date 2026-09-03 --pool 300
    python3 daily_signal.py --quiet        # 只输出 JSON，不打进度
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import warnings
import fcntl
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

try:
    import akshare as ak
except Exception:
    ak = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
API = os.path.join(ROOT, "api")
DAILY = os.path.abspath(os.path.join(ROOT, "..", "daily"))

# ── v5 定案参数 ─────────────────────────────────────────────
CAPITAL = 200_000.0
LOT = 100
COMM_RATE, COMM_MIN, SLIP, TAX_RATE = 0.00025, 5.0, 0.001, 0.0005
MAX_PRICE = 120.0
STOP_LOSS = -0.25

N_DEFENSE, W_DEFENSE = 4, 0.065      # 防御仓 4 只 × 6.5% = 26%
N_HOT, W_HOT = 1, 0.040              # 热点仓 1 只 × 4%
N_ETF, W_ETF_TOTAL = 12, 0.58        # ETF 腿 12 只，合计 58%（留 12% 现金）
CASH_FLOOR = 0.05                    # 现金垫下限 5%，补位时不得击穿

# 调仓日历（v5 定案）：个股腿季频（1/4/7/10 月首个交易日），ETF 腿月频
REBALANCE_MONTHS = (1, 4, 7, 10)
REBALANCE_MONTHS_ETF = tuple(range(1, 13))

VALID_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003", "300", "301")

# 上期（2026-09-02）持仓 seed。原始 exec_engine.py 不在这个沙箱里，
# 只能从交接文档的记录重建：防御仓 4 只银行 26% + 热点仓 招商轮船 4%。
# 成本价与手数未记录 → 留空，止损扫描会跳过，需人工补录实际成交价。
SEED_POSITIONS = [
    ("601398", "工商银行", "防御仓"),
    ("601288", "农业银行", "防御仓"),
    ("600016", "民生银行", "防御仓"),
    ("601166", "兴业银行", "防御仓"),
    ("601872", "招商轮船", "热点仓"),
]

# ETF 腿：月频调仓。这里保留上次选出的 12 只，月初首个交易日重新打分。
ETF_POOL = [
    ("512040", "价值ETF富国", "宽基价值"),
    ("512890", "红利低波ETF华泰柏瑞", "红利"),
    ("510880", "红利ETF华泰柏瑞", "红利"),
    ("512530", "沪深300红利ETF建信", "红利"),
    ("510030", "价值ETF华宝", "宽基价值"),
    ("512390", "中国低波ETF平安", "宽基价值"),
    ("512750", "基本面50ETF嘉实", "宽基价值"),
    ("510010", "180治理ETF交银", "宽基价值"),
    ("511260", "十年国债ETF国泰", "国债"),
    ("511020", "国债ETF平安", "国债"),
    ("159934", "黄金ETF易方达", "黄金"),
    ("518880", "黄金ETF华安", "黄金"),
]


# ---------------------------------------------------------------- 并发 / 原子写
class _Lock:
    """进程锁。定时任务要跑 90 秒，用户若同时手动跑，
    两个进程会同时写 daily_signal.json —— 写到一半被覆盖就是截断的坏 JSON。
    """
    def __init__(self, name: str = "daily_signal"):
        self.path = os.path.join(tempfile.gettempdir(), f".{name}.lock")
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def __exit__(self, *a):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        except Exception:
            pass
        self.fh.close()
        return False


def atomic_write_json(path: str, data) -> None:
    """先写临时文件再原子替换 —— 杜绝「读到写了一半的 JSON」。

    注意 tempfile.mkstemp 建出来的文件权限是 0600（只有属主可读写）。
    原子替换后产物继承这个权限，于是 daily_signal.json 变成 -rw-------，
    而同目录其它文件是 644。本沙箱里 server 与生成脚本同属 root 所以没事，
    一旦换成别的用户 / 容器 / nginx 静态服务去读，就会「文件在但读不了」。
    写完显式放宽到 0644（受 umask 影响，够用即可）。
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)          # POSIX 原子操作
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def log(msg: str, quiet=False):
    if not quiet:
        print(msg, flush=True)


# ---------------------------------------------------------------- 工具
def is_a_share(code: str) -> bool:
    """A 股可执行性过滤。

    B 股只有 900xxx（沪市）与 200xxx（深市）两个号段，
    不能写成 startswith("9") —— 那会把 999999 这类不存在的代码
    也算成「已正确剔除的 B 股」，掩盖真实问题。
    """
    c = str(code).zfill(6)
    if not (len(c) == 6 and c.isdigit()):
        return False
    if c.startswith(("688", "689")):
        return False          # 科创板
    if c.startswith(("4", "8")):
        return False          # 北交所
    if c.startswith(("900", "200")):
        return False          # B 股
    return c.startswith(VALID_PREFIX)


def pfx(code: str) -> str:
    c = str(code).zfill(6)
    return "sh" if c[0] in ("6", "5") else "sz"


def zscore(s: pd.Series) -> pd.Series:
    """横截面 z-score，标准差为 0 时返回 0（避免除零炸掉整列）。"""
    sd = s.std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(0.0, index=s.index)
    return (s - s.mean()) / sd


# ---------------------------------------------------------------- 数据
def get_pool(pools=("000300", "000905"), quiet=False) -> pd.DataFrame:
    """成分股池：沪深300（防御仓）+ 中证500（热点仓扩池）。"""
    frames, names = [], {}
    for sym in pools:
        nm = "沪深300" if sym == "000300" else ("中证500" if sym == "000905" else sym)
        try:
            df = ak.index_stock_cons(symbol=sym)
            col = "品种代码" if "品种代码" in df.columns else df.columns[0]
            ncol = "品种名称" if "品种名称" in df.columns else df.columns[1]
            d = pd.DataFrame({"code": df[col].astype(str).str.zfill(6),
                              "name": df[ncol]})
            d["pool"] = nm
            frames.append(d)
            names[nm] = len(d)
            log(f"  {nm}: {len(d)} 只", quiet)
        except Exception as e:
            log(f"  {nm} 成分股获取失败：{type(e).__name__}: {str(e)[:70]}", quiet)
    if not frames:
        raise SystemExit("成分股池获取失败，无法生成信号")
    all_df = pd.concat(frames, ignore_index=True)
    all_df = all_df[all_df["code"].map(is_a_share)]
    # 沪深300 优先（同一只可能同时在两个池）
    all_df["_o"] = (all_df["pool"] != "沪深300").astype(int)
    all_df = all_df.sort_values(["_o", "code"]).drop_duplicates("code").drop(columns="_o")
    log(f"  合并去重后：{len(all_df)} 只（已剔除科创板/北交所/B股）", quiet)
    return all_df.reset_index(drop=True)


def _kline(code: str, days: int = 300) -> tuple[str, pd.DataFrame | None]:
    try:
        df = ak.stock_zh_a_daily(symbol=pfx(code) + code, adjust="qfq")
        if df is None or len(df) < 60:
            return code, None
        return code, df.tail(days).reset_index(drop=True)
    except Exception:
        return code, None


def get_klines(codes: list[str], workers: int = 16, days: int = 300,
               quiet=False, cache_dir: str | None = None,
               want_bar: str | None = None) -> tuple[dict[str, pd.DataFrame], bool]:
    """并发拉 K 线。单只失败不影响全局。

    cache_dir 非空时缓存到磁盘，同一交易日重复跑可直接命中，省掉 90 秒网络等待。

    【新鲜度必须按 bar_date 判定，不能按文件修改时间】
    按 mtime 判断会出这种事故：早上 8:30 缓存了 bar=09-02 的数据，
    当晚 20:00 再跑，文件才 11.5 小时「新鲜」，但 09-03 已收盘，
    于是拿 09-02 的旧因子去给 09-04 下单 —— 整整落后一个交易日。
    所以缓存目录带 manifest 记录本批数据的 bar_date，不匹配即判过期。
    """
    out, t0 = {}, time.time()
    todo = list(codes)
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        man_p = os.path.join(cache_dir, "_manifest.json")
        fresh, man = False, {}
        if os.path.exists(man_p) and want_bar:
            try:
                man = json.load(open(man_p, encoding="utf-8"))
            except Exception:
                man = {}
            if man.get("bar_date") == want_bar:
                fresh = True                       # 数据正是我们要的
            elif man.get("attempted_for") == want_bar:
                # 不久前刚为这个 bar_date 拉过，但上游还没推数据。
                # 再拉一遍也是同样的结果，白等 90 秒 —— 给个 30 分钟宽限，
                # 缓存照用（stale 告警照常触发，不会静默）。
                try:
                    age = (datetime.now() - datetime.strptime(
                        man.get("written_at", ""), "%Y-%m-%d %H:%M:%S")).total_seconds()
                except Exception:
                    age = 1e9
                if age < 1800:
                    fresh = True
                    log(f"  上游尚未更新（{int(age/60)} 分钟前已尝试），"
                        f"沿用现有数据，stale 告警照常触发", quiet)
        if fresh:
            keep = []
            for c in todo:
                p = os.path.join(cache_dir, c + ".csv")
                if os.path.exists(p):
                    try:
                        out[c] = pd.read_csv(p)
                        continue
                    except Exception:
                        pass
                keep.append(c)
            todo = keep
            log(f"  缓存命中 {len(out)} 只（bar={want_bar}），待拉 {len(todo)} 只", quiet)
        elif want_bar:
            log(f"  缓存已过期（需要 bar={want_bar}），全部重拉", quiet)
    if not todo:
        return out, False          # 纯缓存命中，未产生网络请求
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_kline, c, days): c for c in todo}
        for i, f in enumerate(as_completed(futs), 1):
            c, df = f.result()
            if df is not None:
                out[c] = df
                if cache_dir:
                    try:
                        df.to_csv(os.path.join(cache_dir, c + ".csv"), index=False)
                    except Exception:
                        pass
            if not quiet and i % 100 == 0:
                log(f"    K线 {i}/{len(todo)} …", quiet)
    # 注意：manifest 不能在这里写。此刻只知道「想要」哪个 bar_date，
    # 还不知道「实际拉到」哪个 —— 上游延迟时两者不同。
    # 写错会把旧数据永久标记成新鲜，重跑多少次都拿不到新数据。
    # 真正的写入在 generate() 里，等 bar_date 从数据算出来之后再写。
    log(f"  K线：{len(out)}/{len(codes)} 只成功，{int((time.time()-t0)*1000)}ms", quiet)
    return out, True


def write_kline_manifest(cache_dir: str, actual_bar: str, n: int,
                         attempted_for: str | None = None) -> None:
    """记录缓存里【实际】数据的 bar_date —— 必须是算出来的，不是想要的。

    attempted_for 另记一份「本次想拉哪个」，用于在数据源延迟时
    识别「刚刚已经试过了」，避免每次重跑都白等 90 秒。
    """
    try:
        os.makedirs(cache_dir, exist_ok=True)
        atomic_write_json(os.path.join(cache_dir, "_manifest.json"),
                          {"bar_date": actual_bar,
                           "attempted_for": attempted_for,
                           "written_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           "n": n})
    except Exception:
        pass


def get_snapshot(codes: list[str], quiet=False) -> pd.DataFrame:
    """腾讯批量快照：现价 / 总市值 / 市净率 / 成交额 / 换手率。

    腾讯对指数与个股格式不同，这里只查个股，字段索引：
      1 名称 / 2 代码 / 3 现价 / 37 成交额(万) / 38 换手率 / 45 总市值 / 46 市净率
    """
    import requests
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    rows = []
    B = 60
    for i in range(0, len(codes), B):
        batch = codes[i:i + B]
        q = ",".join(pfx(c) + c for c in batch)
        try:
            r = requests.get(f"https://qt.gtimg.cn/q={q}",
                             headers={"User-Agent": UA}, timeout=12)
            r.encoding = "gbk"
            for line in r.text.split(";"):
                m = re.search(r'v_(s[hz]\d{6})="(.*)"', line)
                if not m:
                    continue
                p = m.group(2).split("~")
                if len(p) < 47 or not p[3]:
                    continue
                rows.append({
                    "code": m.group(1)[2:], "name": p[1],
                    "price": float(p[3] or 0) or np.nan,
                    "amount_yi": (float(p[37]) / 1e4) if p[37] else np.nan,  # 万→亿
                    "turnover": float(p[38]) if p[38] else np.nan,
                    "mktcap": float(p[45]) if p[45] else np.nan,             # 亿元
                    "pb": float(p[46]) if p[46] else np.nan,
                    "pct": float(p[32]) if len(p) > 32 and p[32] else np.nan,
                })
        except Exception:
            continue
    df = pd.DataFrame(rows)
    log(f"  腾讯快照：{len(df)} 只", quiet)
    return df


# ---------------------------------------------------------------- 因子
def _cmf(df: pd.DataFrame, n: int = 20) -> float:
    """Chaikin Money Flow。高低价相等时该日 MFV 记 0，避免除零。"""
    h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
    rng = (h - l).replace(0, np.nan)
    mfm = ((c - l) - (h - c)) / rng
    mfv = (mfm.fillna(0) * v).rolling(n).sum()
    vol = v.rolling(n).sum()
    if not len(mfv) or not np.isfinite(vol.iloc[-1]) or vol.iloc[-1] == 0:
        return np.nan
    return float(mfv.iloc[-1] / vol.iloc[-1])


def factors(code: str, df: pd.DataFrame) -> dict | None:
    """从单只 K 线算全部因子。bar_date 取最后一根完整交易日。"""
    d = df.copy()
    # ── 口径守卫：盘中运行时最后一根可能是今天的未完成 K 线，必须剔除 ──
    last = pd.to_datetime(d["date"].iloc[-1])
    now = datetime.now()
    if last.date() == now.date() and now.hour < 15:
        d = d.iloc[:-1]
    if len(d) < 60:
        return None

    c = d["close"].astype(float)
    px = float(c.iloc[-1])
    bar_date = str(d["date"].iloc[-1])[:10]

    def ret(n: int):
        if len(c) <= n or c.iloc[-1 - n] == 0:
            return np.nan
        return float(c.iloc[-1] / c.iloc[-1 - n] - 1) * 100

    r12, r1 = ret(252), ret(21)
    mom_12_1 = (r12 - r1) if (np.isfinite(r12) and np.isfinite(r1)) else np.nan
    rev_1m = -r1 if np.isfinite(r1) else np.nan          # 1 月反转
    vol60 = float(c.pct_change().tail(60).std(ddof=0) * np.sqrt(252) * 100)
    liq = float(d["amount"].tail(20).mean() / 1e8) if "amount" in d else np.nan
    hi250 = float(d["high"].tail(250).max())
    r20 = ret(20)
    return {
        "code": code, "bar_date": bar_date, "price": px,
        "r20": r20, "r60": ret(60), "r250": r12, "r1m": r1,
        "mom_12_1": mom_12_1, "rev_1m": rev_1m, "vol60": vol60,
        "liq": liq, "cmf20": _cmf(d, 20),
        "high250": hi250,
        "pct_from_high": float(px / hi250 * 100) if hi250 else np.nan,
    }


def score_defense(df: pd.DataFrame) -> pd.DataFrame:
    """修正六因子 = (z(BP) + z(大市值) + z(12-1动量) + z(1月反转)
                     − z(60日波动) + z(流动性)) / 6

    注意：小市值方向已反转。沪深300 池内 SMB 的 IC = −0.028~−0.051、
    t = −3.2~−6.9，是负 alpha，写成 −z_size 等于常年倒着下注。
    """
    d = df.copy()
    d["bp"] = 1.0 / d["pb"].replace(0, np.nan)      # 价值 BP = 1/PB
    f = {}
    f["z_bp"] = zscore(d["bp"].fillna(d["bp"].median()))
    f["z_size"] = zscore(np.log(d["mktcap"].fillna(d["mktcap"].median()).clip(lower=1)))
    f["z_mom"] = zscore(d["mom_12_1"].fillna(d["mom_12_1"].median()))
    f["z_rev"] = zscore(d["rev_1m"].fillna(d["rev_1m"].median()))
    f["z_vol"] = -zscore(d["vol60"].fillna(d["vol60"].median()))
    f["z_liq"] = zscore(d["liq"].fillna(d["liq"].median()))
    for k, v in f.items():
        d[k] = v
    d["score"] = (d["z_bp"] + d["z_size"] + d["z_mom"] +
                  d["z_rev"] + d["z_vol"] + d["z_liq"]) / 6
    return d


def gates(r: pd.Series) -> tuple[bool, list]:
    """热点仓三道不追高闸门。"""
    g = [
        ("近20日涨幅 ≤ +15%", r.get("r20"), lambda v: v <= 15),
        ("现价 ≤ 52周高点的 95%", r.get("pct_from_high"), lambda v: v <= 95),
        ("资金流 CMF(20) ≥ −0.15", r.get("cmf20"), lambda v: v >= -0.15),
    ]
    oks = [(nm, v, bool(np.isfinite(v)) and fn(v)) for nm, v, fn in g]
    return all(o[2] for o in oks), oks


# ---------------------------------------------------------------- 交易日历 / 调仓
_CAL: list[str] | None = None


def trade_days(quiet=False) -> list[str]:
    """A 股交易日历（新浪，含 2026 全年）。失败时退化为「工作日」近似。"""
    global _CAL
    if _CAL is not None:
        return _CAL
    try:
        df = ak.tool_trade_date_hist_sina()
        _CAL = sorted(str(x)[:10] for x in df["trade_date"])
        return _CAL
    except Exception as e:
        log(f"  ⚠ 交易日历获取失败（{type(e).__name__}），退化为工作日近似", quiet)
        d, out = datetime.now().date() - timedelta(days=400), []
        for i in range(900):
            x = d + timedelta(days=i)
            if x.weekday() < 5:
                out.append(x.strftime("%Y-%m-%d"))
        _CAL = out
        return _CAL


def first_trade_day_of(year: int, month: int, cal: list[str]) -> str | None:
    p = f"{year}-{month:02d}"
    hit = [d for d in cal if d.startswith(p)]
    return hit[0] if hit else None


def rebalance_info(sig_date: str, cal: list[str]) -> dict:
    """判断 sig_date 是否调仓日，并给出下一次调仓日。

    v5 定案：个股腿季频（1/4/7/10 月首个交易日），ETF 腿月频（每月首个交易日）。
    非调仓日【不应该换股】——天天换手要付佣金+滑点+印花税，
    而 v5 的回测收益正是按季频调仓验证的，日频换手是另一个没有回测支撑的策略。
    """
    sd = sig_date
    this_month_ftd = first_trade_day_of(int(sd[:4]), int(sd[5:7]), cal)
    is_etf_day = (this_month_ftd == sd)
    is_stock_day = is_etf_day and int(sd[5:7]) in REBALANCE_MONTHS

    def _next(months):
        y, m = int(sd[:4]), int(sd[5:7])
        for _ in range(24):
            m += 1
            if m > 12:
                m, y = 1, y + 1
            if m in months:
                d = first_trade_day_of(y, m, cal)
                if d and d > sd:
                    return d
        return None

    return {
        "sig_date": sd,
        "stocks_rebalance_day": is_stock_day,
        "etf_rebalance_day": is_etf_day,
        "next_stock_rebalance": _next(REBALANCE_MONTHS),
        "next_etf_rebalance": _next(REBALANCE_MONTHS_ETF),
    }


def expected_bar_date(now: datetime, cal: list[str]) -> str:
    """现在这一刻，K 线最后一根【应该】是哪天。

    hour < 15 → 今天还没收盘（或今天根本不是交易日），用上一个交易日
    hour >= 15 → 今天已收盘，若今天是交易日就用今天
    """
    today = now.strftime("%Y-%m-%d")
    cs, past = set(cal), [d for d in cal if d < today]
    if now.hour < 15:
        return past[-1] if past else today
    return today if today in cs else (past[-1] if past else today)


def resolve_sig_date(now: datetime, cal: list[str]) -> str:
    """信号为哪个交易日的开盘准备。

    9:15（集合竞价开始）之前且今天是交易日 → 今天；
    否则 → 下一个交易日。【必须查交易日历】，
    直接 today+1 会在周五盘后给出周六这种废日期。
    """
    today = now.strftime("%Y-%m-%d")
    if now.hour * 60 + now.minute < 9 * 60 + 15 and today in set(cal):
        return today
    fut = [d for d in cal if d > today]
    return fut[0] if fut else today


# ---------------------------------------------------------------- 持仓跟踪
def load_positions(path: str) -> dict:
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_positions(path: str, d: dict):
    atomic_write_json(path, d)


def stop_loss_scan(positions: dict, quotes: dict) -> list[dict]:
    """对现有持仓做止损扫描。v5 定案：个股止损 −25%（仅防黑天鹅）。"""
    out = []
    for code, p in positions.items():
        px = quotes.get(code)
        if not px:
            continue
        cost = p.get("cost")
        if not cost:
            continue
        pnl = px / cost - 1
        out.append({"code": code, "name": p.get("name", ""), "leg": p.get("leg", ""),
                    "cost": round(cost, 3), "price": round(px, 3),
                    "lots": p.get("lots", 0),
                    "pnl_pct": round(pnl * 100, 2),
                    "hit_stop": pnl <= STOP_LOSS})
    return out


# ---------------------------------------------------------------- 下单测算
def _fill(lots: int, px: float, budget: float) -> dict:
    shares = lots * LOT
    if shares <= 0:
        return {"shares": 0, "amount": 0.0, "fee": 0.0, "cost": 0.0,
                "budget": round(budget, 2), "px": round(px, 4),
                "weight_actual": 0.0, "lots": 0}
    amount = px * shares
    fee = max(amount * COMM_RATE, COMM_MIN)
    return {"shares": shares, "lots": lots, "amount": round(amount, 2),
            "fee": round(fee, 2), "cost": round(amount + fee, 2),
            "budget": round(budget, 2), "px": round(px, 4),
            "weight_actual": round((amount + fee) / CAPITAL * 100, 2)}


def plan_order(price: float, weight: float, etf: bool = False) -> dict:
    """按目标权重算整手下单量（含滑点与佣金）。向下取整，与 v5 回测口径一致。"""
    budget = CAPITAL * weight
    px = price * (1 + SLIP)                      # 买入滑点上浮
    lots = int(budget // (px * LOT))             # 手数
    return _fill(lots, px, budget)


def plan_lots(price: float, lots: int = 1) -> dict:
    """按【手数】下单（二次分配给买不起 1 手的标的补位时用）。"""
    px = price * (1 + SLIP)
    return _fill(lots, px, round(CAPITAL * W_ETF_TOTAL / N_ETF, 2))


# ---------------------------------------------------------------- 主流程
def generate(sig_date: str | None = None, pools=("000300", "000905"),
             workers: int = 16, quiet: bool = False, cache: bool = True) -> dict:
    t0 = time.time()
    now = datetime.now()
    cal0 = trade_days(quiet)
    sig_date = sig_date or resolve_sig_date(now, cal0)
    want_bar = expected_bar_date(now, cal0)

    log(f"\n生成 {sig_date} 交易信号（v5 定案）", quiet)
    log("─" * 56, quiet)

    pool = get_pool(pools, quiet)
    codes = pool["code"].tolist()

    cache_dir = os.path.join(ROOT, "..", "data", "kline_cache") if cache else None
    if cache_dir:
        cache_dir = os.path.abspath(cache_dir)
    klines, fetched = get_klines(codes, workers=workers, quiet=quiet,
                                 cache_dir=cache_dir, want_bar=want_bar)
    if len(klines) < 50:
        raise SystemExit(f"K 线只取到 {len(klines)} 只，数据异常，拒绝出信号")

    log("  计算因子…", quiet)
    rows, bar_dates = [], []
    for c, k in klines.items():
        f = factors(c, k)
        if f:
            rows.append(f)
            bar_dates.append(f["bar_date"])
    if not rows:
        raise SystemExit("因子计算全部失败")
    F = pd.DataFrame(rows)
    bar_date = max(bar_dates)                      # 统一用最新的完整交易日
    log(f"  因子完成：{len(F)} 只，bar_date = {bar_date}", quiet)

    # 缓存 manifest 等 bar_date 从数据里算出来之后再写，
    # 记录实际值而非期望值 —— 否则上游延迟时会把旧数据锁死成「新鲜」。
    # 只有真的拉过才写：否则每次命中缓存都刷新 written_at，
    # 上游延迟的 30 分钟宽限期会被无限续期，永远等不到新数据。
    if cache_dir and fetched:
        write_kline_manifest(cache_dir, bar_date, len(klines), attempted_for=want_bar)

    # 口径自检：bar_date 必须是我们期待的那个交易日，落后就要明说
    # 注意用 < 而非 <=：sig_date 当天尚未开盘，它的行情本来就不该在因子里，
    # 不算「落后」。真正落后的是 bar_date 与 sig_date 之间的【已收盘】交易日。
    behind = len([d for d in cal0 if bar_date < d < sig_date])
    if bar_date != want_bar:
        log(f"  ⚠ 数据落后：bar_date={bar_date}，但此刻应为 {want_bar}"
            f"（数据源未更新，信号可靠性下降）", quiet)
    if behind >= 2:
        log(f"  ⚠ bar_date 落后 sig_date {behind} 个交易日，"
            f"中间的交易日行情未反映在因子里", quiet)

    # ── 新股剔除：数据不足 1 年的，12-1 动量算不出来 ──
    # 不能靠中位数填 NaN 蒙混 —— 那等于给新股发一张「平均动量」的中性票，
    # 让它混在一堆有真实动量的老票里，属于静默污染。
    n_before = len(F)
    F = F[F["mom_12_1"].notna()].copy()
    if len(F) < n_before:
        log(f"  剔除数据不足 1 年的新股 {n_before - len(F)} 只"
            f"（12-1 动量算不出，不发中性分）", quiet)

    snap = get_snapshot(list(F["code"]), quiet=quiet)
    if snap.empty:
        raise SystemExit("腾讯快照全空（网络异常），拒绝出信号——"
                         "缺市值/市净率，六因子算不出来")
    # ── 关键：F 与 snap 都有 price，merge 会拆成 price_x / price_y ──
    #    price_x = bar_date 收盘（因子口径，权威）
    #    price_y = 快照实时价（盘中会失真，绝不能用于打分与闸门）
    F = F.rename(columns={"price": "close_bar"})
    D = F.merge(snap, on="code", how="left").rename(columns={"price": "snap_price"})
    D = D.merge(pool[["code", "pool"]], on="code", how="left")
    D["name"] = D["name"].fillna(D["code"])
    # 打分与闸门统一用 bar_date 收盘价（铁律）
    D["price"] = D["close_bar"]

    # ── 基本面口径对齐 ──
    # 快照的 pb / mktcap 是【当下】的，而动量、波动、流动性都算自 bar_date 收盘。
    # 盘前跑两者一致；盘中跑就会一半昨日、一半此时，属于隐性混口径。
    # 按 close_bar / snap_price 的比例把基本面折回 bar_date 口径。
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = D["close_bar"] / D["snap_price"].replace(0, np.nan)
    ratio = ratio.replace([np.inf, -np.inf], np.nan).fillna(1.0)
    D["mktcap"] = D["mktcap"] * ratio
    D["pb"] = D["pb"] * ratio
    if not quiet and (ratio != 1.0).abs().gt(0.005).any():
        n_adj = int((ratio != 1.0).__and__(ratio.notna()).sum())
        log(f"  基本面已折回 bar_date 口径（{n_adj} 只，现价与收盘价有偏离）", quiet)

    miss = int(D["mktcap"].isna().sum() + D["pb"].isna().sum())
    if miss > len(D) * 0.5:
        raise SystemExit(f"快照缺失率过高（{miss} 处空值），拒绝出信号")

    # ── 可执行口径过滤 ──
    n0 = len(D)
    D = D[(D["price"].notna()) & (D["price"] > 0) & (D["price"] <= MAX_PRICE)]
    log(f"  股价 ≤ {MAX_PRICE} 元过滤后：{len(D)} 只（剔除 {n0-len(D)} 只高价股）", quiet)

    # ── 防御仓：沪深300 池内修正六因子 ──
    hs = D[D["pool"] == "沪深300"].copy()
    if len(hs) < N_DEFENSE * 2:
        hs = D.copy()
        log("  ⚠ 沪深300 池样本不足，改用全池选防御仓", quiet)
    S = score_defense(hs)
    S = S.sort_values("score", ascending=False)
    defense = S.head(N_DEFENSE)

    # ── 热点仓：12-1 动量 + 三道闸门 ──
    hot = D.dropna(subset=["mom_12_1"]).copy()
    hot = hot[hot["mom_12_1"] > 0].sort_values("mom_12_1", ascending=False)
    hot_pick, gate_log = None, []
    for _, r in hot.head(60).iterrows():
        ok, gl = gates(r)
        if ok:
            hot_pick, gate_log = r, gl
            break
        if not gate_log:
            gate_log = gl
    if hot_pick is None:
        log("  ⚠ 无一通过三道闸门，热点仓留空（宁可空仓也不追高）", quiet)

    # ── 组装 ──
    legs = []
    for _, r in defense.iterrows():
        o = plan_order(float(r["price"]), W_DEFENSE)
        legs.append({"leg": "防御仓", "code": r["code"], "name": r["name"],
                     "score": round(float(r["score"]), 3),
                     "price": round(float(r["price"]), 3), **o,
                     "why": f"修正六因子 {float(r['score']):+.3f}",
                     "detail": {k: (None if not np.isfinite(float(r[k])) else round(float(r[k]), 3))
                                for k in ("z_bp", "z_size", "z_mom", "z_rev", "z_vol", "z_liq")}})
    if hot_pick is not None:
        o = plan_order(float(hot_pick["price"]), W_HOT)
        legs.append({"leg": "热点仓", "code": hot_pick["code"], "name": hot_pick["name"],
                     "score": round(float(hot_pick["mom_12_1"]), 2),
                     "price": round(float(hot_pick["price"]), 3), **o,
                     "why": f"12-1 动量 {float(hot_pick['mom_12_1']):+.1f}%，三道闸门全过",
                     "detail": {"r20": round(float(hot_pick["r20"]), 2),
                                "pct_from_high": round(float(hot_pick["pct_from_high"]), 1),
                                "cmf20": round(float(hot_pick["cmf20"]), 3)}})

    # ── ETF 腿：月频调仓 ──
    etf_q = {c: None for c, *_ in ETF_POOL}
    try:
        q = ",".join(pfx(c) + c for c in etf_q)
        import requests
        r = requests.get(f"https://qt.gtimg.cn/q={q}",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        r.encoding = "gbk"
        for line in r.text.split(";"):
            m = re.search(r'v_(s[hz]\d{6})="(.*)"', line)
            if m:
                p = m.group(2).split("~")
                if len(p) > 3 and p[3]:
                    etf_q[m.group(1)[2:]] = float(p[3])
    except Exception:
        pass
    etfs, etf_ok = [], 0
    for c, nm, sec in ETF_POOL:
        px = etf_q.get(c)
        if not px:
            etfs.append({"leg": "ETF腿", "code": c, "name": nm, "sector": sec,
                         "price": None, "shares": 0, "lots": 0, "cost": 0,
                         "error": "行情获取失败"})
            continue
        o = plan_order(px, W_ETF_TOTAL / N_ETF, etf=True)
        etf_ok += 1
        etfs.append({"leg": "ETF腿", "code": c, "name": nm, "sector": sec,
                     "price": round(px, 3), **o})

    # ── 第二轮：剩余现金补位 ──
    # 十年国债 ETF 135.8 元 / 国债 ETF 118.2 元，1 手要 1.36 万 / 1.18 万，
    # 而单只预算只有 9,667 元 → 第一轮必然 0 手。不补位就白白少 9.66% 仓位。
    used = sum(x.get("cost", 0) for x in legs + etfs)
    topped = []
    for x in etfs:
        if x.get("shares", 0) == 0 and x.get("price"):
            o1 = plan_lots(x["price"], 1)
            if o1["cost"] <= CAPITAL - used - CASH_FLOOR * CAPITAL:
                x.update(o1)
                x["note"] = "剩余现金补位 1 手（目标权重买不满最小单位）"
                used += o1["cost"]
                topped.append(x["code"])

    total_cost = sum(x.get("cost", 0) for x in legs + etfs)
    cash = CAPITAL - total_cost
    if topped:
        log(f"  剩余现金补位：{len(topped)} 只（{'、'.join(topped)}）", quiet)

    # ── 调仓日历 / 持仓 / 止损 ──
    cal = trade_days(quiet)
    rb = rebalance_info(sig_date, cal)
    pos_path = os.path.join(API, "positions.json")
    positions = load_positions(pos_path)
    if not positions:
        for code, nm, leg in SEED_POSITIONS:
            positions[code] = {"name": nm, "leg": leg, "lots": 0,
                               "cost": None, "entry_date": bar_date,
                               "source": "09-02 信号 seed（原始 exec_engine.py 不在本沙箱，"
                                         "成本价与手数未记录，首次扫描前需人工补录）"}
        save_positions(pos_path, positions)

    # 持仓行情（用于止损扫描）
    pq = {}
    if positions:
        try:
            sp = get_snapshot([c for c in positions if is_a_share(c)][:200], quiet=True)
            pq = dict(zip(sp["code"], sp["price"])) if len(sp) else {}
        except Exception:
            pq = {}
    stops = stop_loss_scan({c: p for c, p in positions.items() if p.get("cost")}, pq)

    # ── 本期指令 vs 上期持仓：变更清单 ──
    new_codes = {x["code"] for x in legs}
    old_codes = set(positions) if positions else set()
    changes = {
        "is_stock_rebalance_day": rb["stocks_rebalance_day"],
        "is_etf_rebalance_day": rb["etf_rebalance_day"],
        "next_stock_rebalance": rb["next_stock_rebalance"],
        "next_etf_rebalance": rb["next_etf_rebalance"],
        "add": sorted(new_codes - old_codes),
        "remove": sorted(old_codes - new_codes),
        "keep": sorted(new_codes & old_codes),
        "action": ("执行调仓" if rb["stocks_rebalance_day"]
                   else "维持上期持仓，不换股"),
    }

    out = {
        "rebalance": rb,
        "changes": changes,
        "data_freshness": {
            "bar_date": bar_date,
            "expected_bar_date": want_bar,
            "stale": bar_date != want_bar,
            "bars_behind": behind,
        },
        "positions": positions,
        "stop_loss_scan": stops,
        "sig_date": sig_date,
        "bar_date": bar_date,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "capital": CAPITAL,
        "config": {
            "defense": f"{N_DEFENSE} 只 × {W_DEFENSE*100}% = {N_DEFENSE*W_DEFENSE*100}%",
            "hot": f"{N_HOT} 只 × {W_HOT*100}% = {N_HOT*W_HOT*100}%",
            "etf": f"{N_ETF} 只合计 {W_ETF_TOTAL*100}%",
            "max_price": MAX_PRICE, "stop_loss": STOP_LOSS,
            "pool": f"{len(D)} 只（沪深300 + 中证500，已过滤）",
        },
        "picks": legs,
        "etfs": etfs,
        "hot_gates": [{"name": n, "value": (None if v is None or not np.isfinite(v)
                                            else round(float(v), 3)), "pass": p}
                      for n, v, p in gate_log],
        "summary": {
            "n_pick": len(legs), "n_etf": etf_ok,
            "invested": round(total_cost, 2),
            "invested_pct": round(total_cost / CAPITAL * 100, 2),
            "cash": round(cash, 2), "cash_pct": round(cash / CAPITAL * 100, 2),
        },
        "defense_ranking": [
            {"code": r["code"], "name": r["name"], "score": round(float(r["score"]), 3),
             "price": round(float(r["price"]), 3)}
            for _, r in S.head(12).iterrows()],
        "hot_ranking": [
            {"code": r["code"], "name": r["name"],
             "mom": round(float(r["mom_12_1"]), 2),
             "gates_ok": gates(r)[0]}
            for _, r in hot.head(12).iterrows()],
    }

    log("─" * 56, quiet)
    log(f"  个股 {len(legs)} 只 + ETF {etf_ok} 只 | "
        f"投入 {total_cost:,.0f} 元（{total_cost/CAPITAL*100:.1f}%）| "
        f"现金 {cash:,.0f} 元（{cash/CAPITAL*100:.1f}%）", quiet)
    log(f"  耗时 {int((time.time()-t0)*1000)}ms", quiet)
    return out


def to_markdown(d: dict) -> str:
    rb, ch = d["rebalance"], d["changes"]
    L = [f"# {d['sig_date']} 交易信号（v5 定案）", "",
         f"- 信号日期：**{d['sig_date']}**（开盘前执行）",
         f"- 因子基准：**{d['bar_date']} 收盘**（bar_date，铁律：不得用 sig_date）",
         f"- 生成时间：{d['generated_at']}",
         f"- 本金：{d['capital']:,.0f} 元",
         f"- 样本池：{d['config']['pool']}", ""]

    # ── 顶部：今天该不该动 ──
    # 数据新鲜度：bar_date 落后必须显式告警，绝不让旧因子静默生效
    fr = d.get("data_freshness") or {}
    if fr.get("stale"):
        L += [f"> ⚠ **数据源未更新**：因子基准 {fr.get('bar_date')}，"
              f"但此刻应为 {fr.get('expected_bar_date')}。",
              "> 开盘前重跑一次即可（`bash web/run_daily_signal.sh`）。", ""]
    elif (fr.get("bars_behind") or 0) >= 1:
        L += [f"> ⚠ **信号跨度提示**：因子基准 {fr.get('bar_date')} 收盘，"
              f"而本信号是为 {d['sig_date']} 开盘准备的，中间 "
              f"{fr.get('bars_behind')} 个交易日的行情尚未反映在因子里。",
              "> 这是盘中/盘后出信号的必然结果；每天开盘前 8:30 重跑即可消除。", ""]

    L += ["## 今天该不该动", "",
          f"**{ch['action']}**", "",
          "| 项目 | 状态 |",
          "|---|---|",
          f"| 今日是否个股调仓日 | {'**是**' if rb['stocks_rebalance_day'] else '否'} |",
          f"| 今日是否 ETF 调仓日 | {'**是**' if rb['etf_rebalance_day'] else '否'} |",
          f"| 下次个股调仓 | {rb['next_stock_rebalance']} |",
          f"| 下次 ETF 调仓 | {rb['next_etf_rebalance']} |", ""]
    if not rb["stocks_rebalance_day"]:
        L += ["> 个股腿按 v5 定案是**季频调仓**（1/4/7/10 月首个交易日），",
              "> 不是每天都换。天天换手要付佣金 0.025% + 滑点 0.1% + 印花税 0.05%，",
              "> 而 v5 的回测收益正是**按季频换手**验证出来的 —— 日频调仓是另一个",
              "> 没有回测支撑的策略，不在定案范围内。下面的排名**仅供观察**。", ""]
    if ch["add"] or ch["remove"]:
        L += [f"调仓差异：新进 {len(ch['add'])} 只（{'、'.join(ch['add']) or '—'}），"
              f"调出 {len(ch['remove'])} 只（{'、'.join(ch['remove']) or '—'}），"
              f"保留 {len(ch['keep'])} 只。", ""]

    # ── 止损扫描 ──
    if d.get("stop_loss_scan"):
        L += ["## 持仓止损扫描（−25%，仅防黑天鹅）", "",
              "| 代码 | 名称 | 成本 | 现价 | 浮盈亏 | 触发 |",
              "|---|---|---:|---:|---:|---|"]
        for s in d["stop_loss_scan"]:
            L.append(f"| {s['code']} | {s['name']} | {s['cost']} | {s['price']} | "
                     f"{s['pnl_pct']:+.2f}% | {'**触发**' if s['hit_stop'] else '否'} |")
        L += [""]
    else:
        L += ["## 持仓止损扫描", "",
              "> 持仓成本价未记录，无法计算浮盈亏。请在 "
              "`web/api/positions.json` 里补录实际成交价后重跑。", ""]

    L += ["## 个股腿", ""]
    if not rb["stocks_rebalance_day"]:
        L += ["> 今日非个股调仓日。下表是**本期打分结果，仅供观察**，",
              "> **不是今天的买入指令**。实际应执行的仍是上期持仓。", ""]
    L += ["| 腿 | 代码 | 名称 | 现价 | 手数 | 金额 | 佣金 | 合计 | 依据 |",
          "|---|---|---|---|---:|---:|---:|---:|---|"]
    for x in d["picks"]:
        L.append(f"| {x['leg']} | {x['code']} | {x['name']} | {x['price']} | "
                 f"{x['shares']} | {x['amount']:,.2f} | {x['fee']:.2f} | "
                 f"{x['cost']:,.2f} | {x['why']} |")
    L += ["", f"**合计投入 {d['summary']['invested']:,.2f} 元"
              f"（{d['summary']['invested_pct']}%），"
              f"留现金 {d['summary']['cash']:,.2f} 元（{d['summary']['cash_pct']}%）**", ""]

    L += ["## ETF 腿（月频调仓）", ""]
    if not rb["etf_rebalance_day"]:
        L += [f"> 今日非 ETF 调仓日（下次 {rb['next_etf_rebalance']}），"
              "**维持现有 ETF 持仓**。", ""]
    L += ["| 代码 | 名称 | 板块 | 现价 | 手数 | 金额 | 合计 | 备注 |",
          "|---|---|---|---|---:|---:|---:|---|"]
    for x in d["etfs"]:
        if x.get("error"):
            L.append(f"| {x['code']} | {x['name']} | {x['sector']} | — | — | — | — | "
                     f"{x['error']} |")
        elif x.get("shares", 0) == 0:
            L.append(f"| {x['code']} | {x['name']} | {x['sector']} | {x['price']} | 0 | 0 | 0 | "
                     f"1 手需 {x['price']*LOT:,.0f} 元，超出单只预算 {x['budget']:,.0f} 元 |")
        else:
            L.append(f"| {x['code']} | {x['name']} | {x['sector']} | {x['price']} | "
                     f"{x['shares']} | {x['amount']:,.2f} | {x['cost']:,.2f} | "
                     f"{x.get('note','')} |")
    L += [""]

    L += ["## 热点仓三道闸门", "", "| 闸门 | 实测值 | 结果 |", "|---|---|---|"]
    for g in d["hot_gates"]:
        L.append(f"| {g['name']} | {g['value']} | {'✓ 通过' if g['pass'] else '✕ 未过'} |")
    L += [""]

    L += ["## 防御仓六因子排名（Top 12）", "",
          "| # | 代码 | 名称 | 六因子分 | 现价 |", "|---:|---|---|---:|---:|"]
    for i, r in enumerate(d["defense_ranking"], 1):
        L.append(f"| {i} | {r['code']} | {r['name']} | {r['score']:+.3f} | {r['price']} |")
    L += [""]

    L += ["## 热点仓 12-1 动量排名（Top 12）", "",
          "| # | 代码 | 名称 | 12-1动量 | 三闸门 |", "|---:|---|---|---:|---|"]
    for i, r in enumerate(d["hot_ranking"], 1):
        L.append(f"| {i} | {r['code']} | {r['name']} | {r['mom']:+.1f}% | "
                 f"{'✓' if r['gates_ok'] else '✕'} |")
    L += ["", "---", "",
          "> 本信号由规则自动生成，非投资建议。回测区间偏牛，",
          "> B vs A 方案 P(更优)=86.9%，未达 95% 统计显著。"]
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="信号日期 YYYY-MM-DD")
    ap.add_argument("--pool", default="300,905", help="成分池，逗号分隔")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-cache", action="store_true", help="忽略 K 线缓存，强制重拉")
    a = ap.parse_args()

    pools = tuple(("000300" if p.strip() == "300" else
                   "000905" if p.strip() == "905" else p.strip())
                  for p in a.pool.split(","))
    with _Lock() as got:
        if not got:
            print("已有信号生成任务在运行（进程锁），本次退出，避免并发写坏产物",
                  file=sys.stderr)
            raise SystemExit(3)

        d = generate(sig_date=a.date, pools=pools, workers=a.workers,
                     quiet=a.quiet, cache=not a.no_cache)

        os.makedirs(API, exist_ok=True)
        os.makedirs(DAILY, exist_ok=True)
        p1 = os.path.join(API, "daily_signal.json")
        p2 = os.path.join(DAILY, f"{d['sig_date']}_信号.md")
        atomic_write_json(p1, d)
        open(p2, "w", encoding="utf-8").write(to_markdown(d))
        print(f"\n已写入：\n  {p1}\n  {p2}")
