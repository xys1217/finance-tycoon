#!/usr/bin/env python3.11
"""信号产物体检：能不能拿去部署。

为什么单独立一个文件
------------------------------------------------------------------
这道检查原先是内联在 workflow 里的 heredoc，没法单独跑、
没法做负向测试 —— 而「检查工具本身没验过」正是前几轮反复漏 bug 的根因。
抽成脚本后，可以拿造假的坏信号喂它，确认它真的会红。

它防的是哪类事故
------------------------------------------------------------------
「任务绿了，但信号是昨天的」—— 本地定时任务踩过的坑：
状态更新成已执行、last_run_at 往前走，就是没有产物。
所以这里不查「跑没跑」，只查「产物对不对」。

检查项
------------------------------------------------------------------
1. 文件读得到、是合法 JSON
2. data_freshness.stale == False（bar_date 没落后于期望交易日）
3. picks / etfs 非空
4. ETF 行情成功率：拿不到行情的 ETF 仍会进列表（shares=0 带 error），
   只看「列表非空」会漏掉整条腿全挂 —— 必须看 summary.n_etf 成功数

用法：  python3.11 tools/check_signal_fresh.py [信号文件路径]
退出码：0 通过 / 1 不通过
"""
import json
import os
import sys

MAX_ETF_LOST = 2          # 容忍 2 只偶发失败；超过判定接口不通或代码坏了


def check(path: str) -> int:
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print(f"::error::读不到或解析不了 {path}：{e}")
        return 1

    f = d.get("data_freshness", {})
    bar, sig = d.get("bar_date"), d.get("sig_date")
    print(f"sig_date={sig}  bar_date={bar}  "
          f"stale={f.get('stale')}  bars_behind={f.get('bars_behind')}")

    if f.get("stale"):
        print("::error::信号数据不新鲜（stale=true），"
              f"bar_date={bar}，期望={f.get('expected_bar_date')}，"
              f"落后 {f.get('bars_behind')} 个交易日。")
        return 1

    if not d.get("picks") or not d.get("etfs"):
        print("::error::信号内容为空（picks 或 etfs 缺失），不能部署。")
        return 1

    n_all = len(d.get("etfs", []))
    n_ok = d.get("summary", {}).get("n_etf", 0)
    if n_all and n_ok < n_all:
        lost = [e.get("code", "?") for e in d["etfs"] if e.get("error")]
        print(f"::error::ETF 行情只成功 {n_ok}/{n_all} 只"
              f"（失败：{', '.join(lost) or '未知'}）")
        if n_all - n_ok > MAX_ETF_LOST:
            print(f"::error::缺失超过 {MAX_ETF_LOST} 只，判定为失败。")
            return 1

    print(f"::notice::信号体检通过（ETF 行情 {n_ok}/{n_all}）")
    return 0


def selftest(base: dict) -> int:
    """负向自测：喂造假的坏信号，确认本工具真的会红。

    检查工具如果不自检，就会出现「一直报绿、其实什么也没查」的情况
    —— arities_check.py 第一版就是这个毛病（只认 return (a,b,c)，
    漏了 append/yield 模式，表是空的却报「干净 ✓」）。
    """
    import copy
    import tempfile

    cases = [
        ("正常信号 → 应通过", lambda d: d, 0),
        ("stale=true → 应拒绝",
         lambda d: d.update(data_freshness={**d["data_freshness"],
                                            "stale": True, "bars_behind": 3}) or d, 1),
        ("picks 为空 → 应拒绝", lambda d: d.update(picks=[]) or d, 1),
        ("etfs 为空 → 应拒绝", lambda d: d.update(etfs=[]) or d, 1),
        ("ETF 行情全挂 → 应拒绝",
         lambda d: d.update(etfs=[{**e, "error": "行情获取失败", "shares": 0}
                                  for e in d["etfs"]],
                            summary={**d["summary"], "n_etf": 0}) or d, 1),
        ("ETF 只挂 1 只 → 应容忍",
         lambda d: d.update(etfs=[{**e, "error": "x"} if i == 0 else e
                                  for i, e in enumerate(d["etfs"])],
                            summary={**d["summary"],
                                     "n_etf": d["summary"]["n_etf"] - 1}) or d, 0),
    ]
    bad = 0
    for name, mut, want in cases:
        d = copy.deepcopy(base)
        mut(d)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
            p = f.name
        got = check(p)
        os.unlink(p)
        ok = (got == want)
        bad += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {name}（实际退出码 {got}）")

    # 文件级故障
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write("{ 这不是 json")
        p = f.name
    got = check(p)
    os.unlink(p)
    ok = got == 1
    bad += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 非法 JSON → 应拒绝（实际退出码 {got}）")

    got = check("/tmp/肯定不存在的文件_xyz.json")
    ok = got == 1
    bad += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 文件不存在 → 应拒绝（实际退出码 {got}）")

    print(f"\n自测：{'全部通过 ✓' if bad == 0 else f'{bad} 项不通过 ✗'}")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        src = "web/api/daily_signal.json"
        if not os.path.exists(src):
            print(f"自测需要一份真实信号：{src} 不存在")
            sys.exit(1)
        sys.exit(selftest(json.load(open(src, encoding="utf-8"))))
    sys.exit(check(sys.argv[1] if len(sys.argv) > 1 else "web/api/daily_signal.json"))
