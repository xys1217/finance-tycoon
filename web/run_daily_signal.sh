#!/usr/bin/env bash
# 每交易日开盘前生成当日信号，并刷新静态离线版。
#
# 时间：建议 cron 设在 08:30（留 1 小时缓冲，K 线拉取约 90 秒，
#       失败还会自动重试一次）。信号必须在 09:30 开盘前就绪。
# 例：  30 8 * * 1-5 /workspace/web/run_daily_signal.sh >> /workspace/logs/daily_signal.log 2>&1
#
# 铁律：打分与闸门一律用 bar_date（最后一个完整交易日收盘）。
#       早上 8:30 跑，K 线最后一根必然是昨天 → 口径正确。
set -uo pipefail

# 路径不写死：脚本放在仓库 web/ 下，就能在任何机器上跑
# （沙箱里是 /workspace/web，GitHub Actions 里是 ~/work/xxx/xxx/web，
#  自己的服务器又是另一个路径）。写死 /workspace 换台机器就全废。
WEB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$WEB/.." && pwd)"
LOG="$ROOT/logs"
mkdir -p "$LOG"
TS=$(date '+%Y-%m-%d %H:%M:%S')
STAMP=$(date '+%Y-%m-%d')
echo "  工作目录 $WEB"

echo "────────────────────────────────────────────"
echo "[$TS] 开始生成当日信号"

# 0) 非交易日直接跳过（周末/法定节假日不出信号，避免挂出过期单子）
python3 - "$STAMP" <<'PY'
import sys, datetime
try:
    import akshare as ak
    cal = set(str(x)[:10] for x in ak.tool_trade_date_hist_sina()["trade_date"])
    today = sys.argv[1]
    sys.exit(0 if today in cal else 3)
except SystemExit:
    raise
except Exception:
    sys.exit(0)          # 日历拿不到就照常跑，宁可多跑不可漏跑
PY
RC=$?
if [ "$RC" = "3" ]; then
  echo "[$STAMP] 非交易日，跳过"
  exit 0
fi

# 1) 生成信号（失败自动重试一次）
cd "$WEB/modules/signal" || exit 1
if ! python3 daily_signal.py --quiet > "$LOG/signal_$STAMP.log" 2>&1; then
  echo "[$(date '+%H:%M:%S')] 首次失败，60 秒后重试"
  sleep 60
  if ! python3 daily_signal.py --quiet > "$LOG/signal_$STAMP.log" 2>&1; then
    echo "[$(date '+%H:%M:%S')] 重试仍失败，见 $LOG/signal_$STAMP.log"
    tail -20 "$LOG/signal_$STAMP.log"
    exit 1
  fi
fi

# 2) 刷新静态离线版（让 file:// 打开也能看到当日信号）
cd "$WEB" && python3 build_static.py >> "$LOG/signal_$STAMP.log" 2>&1

# 3) 摘要
python3 - "$WEB/api/daily_signal.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
rb, ch = d["rebalance"], d["changes"]
print(f"  信号日 {d['sig_date']} | bar_date {d['bar_date']} | {ch['action']}")
print(f"  个股 {len(d['picks'])} 只 + ETF {d['summary']['n_etf']} 只 | "
      f"投入 {d['summary']['invested']:,.0f} 元（{d['summary']['invested_pct']}%）| "
      f"现金 {d['summary']['cash']:,.0f} 元（{d['summary']['cash_pct']}%）")
sl = [s for s in d.get("stop_loss_scan", []) if s["hit_stop"]]
if sl:
    print(f"  ⚠ 触发止损 {len(sl)} 只：" + "、".join(f"{s['code']} {s['pnl_pct']}%" for s in sl))
PY
echo "[$(date '+%H:%M:%S')] 完成"
