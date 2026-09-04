#!/usr/bin/env python3.11
"""可复现性检查：同一 bar_date 连跑两次信号，结果必须一致。

为什么需要它（2026-09-04 的教训）
------------------------------------------------------------------
前五轮检查全绿，但漏了一个维度：**同一天跑两次，结果不一样**。

根因：ETF 腿直接取腾讯快照的现价 p[3]，而个股腿严格执行
`D["price"] = D["close_bar"]`（bar_date 收盘）。盘前两者相等，
盘中就分叉 —— 09:08 跑出 159934 买 1000 份，09:16 跑出 900 份
（现价涨 1.58%，整手从 1000 跳档到 900），单只差 814 元、
总投入差 721 元。同一天、同一 bar_date，两份互相矛盾的清单。

之前的检查为什么没发现：全部是「确认式」检查 ——
查有没有内容、有没有报错、字段对不对。从来没有人问过
「再跑一次还是不是这个结果」。可复现性是信号系统的底线：
不可复现 = 无法归因 = 出问题查不出来。

排除的实时字段
------------------------------------------------------------------
- generated_at：时间戳，本来就该变
- snap_price：现价，仅展示参考，不参与计算
- stop_loss_scan：止损扫描**应该**用实时价，不该被冻结

用法：python3.11 test_reproducibility.py [--date YYYY-MM-DD]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SIG = os.path.join(HERE, "modules", "signal", "daily_signal.py")
API = os.path.join(HERE, "api", "daily_signal.json")

# 整体跳过的顶层字段：
#   generated_at   —— 时间戳，本来就该变
#   stop_loss_scan —— 止损扫描**必须**用实时价（判断此刻该不该割肉），
#                     冻结成 bar_date 收盘价反而错了。两次跑之间股价在动，
#                     这里的 price / pnl_pct / hit_stop 自然不同，不算不可复现。
#   defense_ranking / hot_ranking
#                  —— 观察用排名榜。score 抖动 1e-3 就能让名次互换，
#                     榜里排第几不影响「买什么、买多少」，所以跳过细节比较；
#                     但下面会单独核对 Top 集合，若真换了人会软提示。
#                     真要影响买入，picks 的 code/shares 严判那关会拦住。
VOLATILE_TOP = {"generated_at", "stop_loss_scan",
                "defense_ranking", "hot_ranking"}
VOLATILE_INNER = {"snap_price"}

# 必须逐位一致的字段：这些决定「买什么、买多少、花多少钱」，
# 漂移一分钱都是真 bug（ETF 腿用现价那次就是 shares 1000→900）。
# 其余数值字段（score / z_* / cmf20 …）允许极小的相对误差，
# 因为快照的 pb、mktcap 有取整精度，按 close_bar/snap_price 折回
# bar_date 后会残留 1e-3 级别的抖动，盘前跑（ratio=1）则完全为零。
STRICT_KEYS = {"code", "name", "leg", "price", "snap_price", "shares", "lots",
               "amount", "fee", "cost", "budget", "px", "weight_actual",
               "sig_date", "bar_date", "stale", "bars_behind", "hit_stop",
               "action", "error"}
TOL = 0.005          # 0.5% 相对误差


def run_once(date: str | None) -> dict:
    cmd = [sys.executable, SIG, "--quiet", "--force"]
    if date:
        cmd += ["--date", date]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    if r.returncode != 0:
        print(f"  信号生成失败（退出码 {r.returncode}）")
        print("  " + (r.stderr or r.stdout).strip()[-800:])
        sys.exit(1)
    return json.load(open(API, encoding="utf-8"))


def strip(d: dict) -> dict:
    out = {k: v for k, v in d.items() if k not in VOLATILE_TOP}

    def clean(x):
        if isinstance(x, dict):
            return {k: v for k, v in x.items() if k not in VOLATILE_INNER}
        return x
    for key in ("picks", "etfs"):
        if key in out:
            out[key] = [clean(i) for i in out[key]]
    return out


def index_by_code(lst) -> dict:
    """列表按 code 建索引，让差异路径可读。"""
    out = {}
    for i, it in enumerate(lst or []):
        out[it.get("code") or f"#{i}"] = it if isinstance(it, dict) else it
    return out


def walk(path, x, y, hard, soft, critical=False):
    """递归比较。关键字段严判，其余数值容差内放行。"""
    crit = critical or path.rsplit(".", 1)[-1] in STRICT_KEYS
    if isinstance(x, dict) and isinstance(y, dict):
        for k in sorted(set(x) | set(y)):
            walk(f"{path}.{k}", x.get(k), y.get(k), hard, soft, crit)
        return
    if isinstance(x, list) and isinstance(y, list):
        if len(x) != len(y):
            hard.append((path, f"长度 {len(x)}", f"长度 {len(y)}"))
            return
        for i, (xi, yi) in enumerate(zip(x, y)):
            walk(f"{path}[{i}]", xi, yi, hard, soft, crit)
        return
    if x == y:
        return
    if not crit and isinstance(x, (int, float)) and isinstance(y, (int, float)):
        if abs(x - y) / max(abs(x), abs(y), 1e-9) <= TOL:
            soft.append((path, x, y))
            return
    hard.append((path, x, y))


def main() -> int:
    date = None
    if "--date" in sys.argv:
        date = sys.argv[sys.argv.index("--date") + 1]
    elif os.path.exists(API):
        # 盘中跑时 resolve_sig_date 会返回「下一交易日」，会被覆盖保护拦下
        # （保护本身是对的：不该让明天的信号冲掉今天的）。
        # 但本脚本要验的是「重跑当天信号是否一致」，不是「明天能不能生成」，
        # 所以默认复用现有产物的 sig_date。
        try:
            date = json.load(open(API, encoding="utf-8")).get("sig_date")
        except Exception:
            date = None

    print("=== 可复现性检查：同一 bar_date 连跑两次 ===")
    had = os.path.exists(API)
    bak = None
    if had:
        bak = tempfile.mktemp(suffix=".json")
        shutil.copy2(API, bak)

    try:
        print("  第一次…", flush=True)
        a = run_once(date)
        print("  第二次…", flush=True)
        b = run_once(date)
    finally:
        if bak and os.path.exists(bak):
            shutil.copy2(bak, API)
            os.unlink(bak)

    date_used = a["sig_date"]
    print(f"  sig_date={date_used}  bar_date={a['bar_date']}")

    # 排名榜细节已跳过，这里单独核对 Top 集合有没有换人
    for key, n in (("defense_ranking", 12), ("hot_ranking", 5)):
        ta = [x.get("code") for x in (a.get(key) or [])[:n]]
        tb = [x.get("code") for x in (b.get(key) or [])[:n]]
        if ta and tb and set(ta) != set(tb):
            print(f"  ⓘ {key} Top{n} 成员有出入（观察用，不影响买入清单）：")
            print(f"      第一次 {ta}")
            print(f"      第二次 {tb}")

    # 列表按 code 建索引，让差异路径可读（picks[601899].shares 而不是 picks[0].shares）
    ca, cb = strip(a), strip(b)
    for key in ("picks", "etfs", "defense_ranking", "hot_ranking"):
        if key in ca:
            ca[key] = index_by_code(ca[key])
            cb[key] = index_by_code(cb.get(key, []))

    hard, soft = [], []
    for k in sorted(set(ca) | set(cb)):
        walk(k, ca.get(k), cb.get(k), hard, soft, critical=(k in VOLATILE_TOP))

    if soft:
        print(f"\n  ⓘ {len(soft)} 处微小抖动（容差 {TOL:.1%} 内，不改变买卖清单）：")
        for k, x, y in soft[:6]:
            print(f"    {k}: {x!r} -> {y!r}")
        print("    来源：快照 pb/mktcap 取整精度 × close_bar/snap_price 折回的残差。")
        print("    盘前跑时 ratio 恒为 1，此抖动为零（CI 的三次触发都在盘前）。")

    if hard:
        print(f"\n  ✗ 两次结果不一致，{len(hard)} 处关键差异：")
        for k, x, y in hard[:15]:
            print(f"    {k}:  {x!r}  ->  {y!r}")
        print("\n  信号不可复现 —— 这些字段决定买什么、买多少，漂移就是真 bug。")
        return 1

    print(f"  ✓ 关键字段完全一致（投入 {a['summary']['invested']:,.2f} 元 / "
          f"个股 {len(a['picks'])} 只 / ETF {len(a['etfs'])} 只）")
    return 0

    print(f"  ✓ 完全一致（投入 {a['summary']['invested']:,.2f} 元 / "
          f"个股 {len(a['picks'])} 只 / ETF {len(a['etfs'])} 只）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
