# -*- coding: utf-8 -*-
"""
单页应用后端：静态资源 + 4 个模块的 API
启动:  python3 server.py            # 默认 8899
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading

from flask import Flask, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.path.join(HERE, "api")
app = Flask(__name__, static_folder=HERE, static_url_path="")

sys.path.insert(0, os.path.join(HERE, "modules", "analyze"))
sys.path.insert(0, os.path.join(HERE, "modules", "paper"))


def _banner():
    """启动时打印代码版本。

    踩过的坑：改完 engine.py / daily_signal.py 后没重启 server，
    Flask 非 debug 模式不会热重载，线上跑的仍是旧代码 —— 于是出现
    「直接调用函数是对的，走 API 却是错的」这种极难排查的现象
    （例如 999999 被旧代码误报成 B 股）。打印 commit 让版本一眼可查。
    """
    import subprocess
    from datetime import datetime
    try:
        rev = subprocess.check_output(
            ["git", "-C", HERE, "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=5).decode().strip()
    except Exception:
        rev = "?"
    try:
        dirty = bool(subprocess.check_output(
            ["git", "-C", HERE, "status", "--porcelain"],
            stderr=subprocess.DEVNULL, timeout=5).decode().strip())
    except Exception:
        dirty = False
    print(f"[server] 启动 {datetime.now():%Y-%m-%d %H:%M:%S} | "
          f"代码 commit {rev}{' + 未提交改动' if dirty else ''}")
    if dirty:
        print("[server] ⚠ 工作区有未提交改动，当前进程加载的可能是旧代码；"
              "改完 engine.py / daily_signal.py 必须重启本进程")


def _load(name):
    p = os.path.join(API, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/<path:p>")
def static_files(p):
    return send_from_directory(HERE, p)


@app.route("/api/quant")
def api_quant():
    d = _load("quant_partial.json")
    return jsonify(d or {"css": "", "html": "<div class='empty'>量化选股片段未生成，运行 python3 build_quant.py</div>"})


@app.route("/api/daily_signal")
def api_daily_signal():
    """当日交易信号。缺文件时给出明确指引，而不是让前端静默空白。"""
    d = _load("daily_signal.json")
    if not d:
        return jsonify({"error": "当日信号未生成。运行：python3 web/modules/signal/daily_signal.py"})
    # 实时补一笔：距今多久（前端用来提示「信号已过期」）
    import datetime as _dt
    try:
        g = _dt.datetime.strptime(d["generated_at"], "%Y-%m-%d %H:%M:%S")
        d["age_min"] = round((_dt.datetime.now() - g).total_seconds() / 60, 1)
    except Exception:
        d["age_min"] = None
    return jsonify(d)


@app.route("/api/positions")
def api_positions():
    """个股腿持仓（含成本价，用于止损扫描）。"""
    return jsonify(_load("positions.json") or {})


@app.route("/api/positions", methods=["POST"])
def api_positions_set():
    """补录成本价：{code:{cost:7.85, lots:15, name:'工商银行', leg:'防御仓'}}"""
    d = request.get_json(silent=True)          # 不用 `or {}`，否则分不清 null 和 {}
    if d is None or d == {}:
        return jsonify({"ok": False, "msg": "空请求"}), 400
    # 必须是 {code: {...}} 字典。传数组时 d.items() 会抛 AttributeError
    # → 整个接口 500 且 Flask 只回一个 HTML 错误页，前端完全看不出原因。
    # 任何外部输入都要先校验类型再处理。
    if not isinstance(d, dict):
        return jsonify({
            "ok": False,
            "msg": f"格式不对：需要 {{\"601899\": {{\"cost\": 7.85, ...}}}} 这种字典，"
                   f"收到的是 {type(d).__name__}"
                   + ("（传了数组，请改成字典）" if isinstance(d, list) else "")
        }), 400

    p = os.path.join(API, "positions.json")
    cur = _load("positions.json") or {}
    updated, skipped = [], []
    for code, v in d.items():
        if not isinstance(v, dict):
            skipped.append(str(code))          # 值不是字典，跳过后要如实报告
            continue
        cur.setdefault(code, {}).update(v)
        cur[code].pop("source", None)          # 人工补录后去掉 seed 提示
        updated.append(str(code))
    # 原子写：先落临时文件再 os.replace，避免写一半被别的进程读走。
    # mkstemp 默认权限 0600，显式放宽到 0644 与其它 api/*.json 保持一致。
    fd, tmp = tempfile.mkstemp(dir=API, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)
    msg = f"已更新 {len(updated)} 只持仓"
    if skipped:
        msg += f"；跳过 {len(skipped)} 项（值不是字典）"
    return jsonify({"ok": True, "msg": msg, "updated": updated,
                    "skipped": skipped, "positions": cur})


@app.route("/api/sentiment")
def api_sentiment():
    return jsonify(_load("sentiment_status.json") or {})


@app.route("/api/mainfund")
def api_mainfund():
    return jsonify(_load("mainfund.json") or {})


@app.route("/api/analyze")
def api_analyze():
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify({"error": "缺少 code 参数"})
    try:
        from analyze_stock import analyze
        return jsonify(analyze(code))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


# ---------------------------------------------------------------- 模拟盘
@app.route("/api/paper")
def api_paper():
    """账户全貌：持仓估值 + 浮动盈亏 + 风险提醒 + 净值快照。"""
    try:
        import engine as PE
        acc = PE.load()
        PE.snapshot(acc)                 # 每日记一次净值，用于画曲线
        st = PE.statement(acc)
        sig = PE.load_signal()
        if sig:
            # sig_plan 返回 5 元组 (code, name, leg, budget, shares)。
            # budget 必须取信号原值，不能用 weight_actual 反推 —— 那是个
            # 已扣成本的数，反推当预算会自我收紧到「买不起 1 手」。
            st["plan"] = [
                {"code": c, "name": n, "leg": leg,
                 "budget": round(b, 2), "shares": s,
                 "weight": round(b / (sig.get("capital") or 200000) * 100, 2)}
                for c, n, leg, b, s in PE.sig_plan(sig)]
            st["plan_source"] = f"{sig.get('sig_date')} 当日信号（bar {sig.get('bar_date')}）"
        else:
            st["plan"] = [{"code": c, "name": n, "leg": leg, "weight": round(w * 100, 2)}
                          for c, n, leg, w in PE.V5_PLAN]
            st["plan_source"] = "内置 V5_PLAN（09-02 快照，当日信号不可用）"
        return jsonify(st)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/paper/quote")
def api_paper_quote():
    """下单前查价：实时价 + 可执行性 + 1 手成本。"""
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify({"error": "缺少 code 参数"})
    try:
        import engine as PE
        return jsonify(PE.quote_for(code))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/paper/order", methods=["POST"])
def api_paper_order():
    """下单：{code, side:'buy'|'sell', mode:'shares'|'amount', value}"""
    d = request.get_json(silent=True) or {}
    code = str(d.get("code") or "").strip()
    side = (d.get("side") or "buy").lower()
    mode = (d.get("mode") or "shares").lower()
    try:
        value = float(d.get("value") or 0)
    except Exception:
        value = 0
    if not code or value <= 0:
        return jsonify({"ok": False, "msg": "参数不完整：需要 code 与正数 value"})
    try:
        import engine as PE
        acc = PE.load()
        if side == "buy":
            if mode == "amount":
                ok, msg, t = PE.buy_amount(acc, code, value)
            else:
                ok, msg, t = PE.buy(acc, code, int(value))
        elif side == "sell":
            ok, msg, t = PE.sell(acc, code, int(value))
        else:
            return jsonify({"ok": False, "msg": f"未知方向 {side}"})
        PE.snapshot(acc, force=True)
        return jsonify({"ok": ok, "msg": msg, "trade": t,
                        "state": PE.statement(acc)})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/paper/plan", methods=["POST"])
def api_paper_plan():
    """按 v5 定案组合一键建仓。"""
    try:
        import engine as PE
        acc = PE.load()
        r = PE.follow_plan(acc)
        PE.snapshot(acc, force=True)
        r["state"] = PE.statement(acc)
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/paper/reset", methods=["POST"])
def api_paper_reset():
    """清空账户，恢复 20 万现金。"""
    try:
        import engine as PE
        acc = PE.reset()
        return jsonify({"ok": True, "msg": "账户已重置为 200,000 元现金",
                        "state": PE.statement(acc)})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """后台触发重新采集（不阻塞响应）。"""
    what = request.json.get("what") if request.is_json else None
    jobs = {
        "sentiment": [sys.executable, os.path.join(HERE, "modules", "sentiment", "dump_status.py")],
        "mainfund": [sys.executable, os.path.join(HERE, "modules", "mainfund", "fetch_fund.py")],
        "signal": [sys.executable, os.path.join(HERE, "modules", "signal", "daily_signal.py"), "--quiet"],
    }
    if what not in jobs:
        return jsonify({"error": f"未知任务 {what}"}), 400

    def run():
        try:
            import subprocess
            subprocess.run(jobs[what], cwd=HERE, timeout=600,
                           env={**os.environ, "TQDM_DISABLE": "1"})
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "msg": f"{what} 采集已在后台启动"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    _banner()
    print(f"最强量化大佬 → http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
