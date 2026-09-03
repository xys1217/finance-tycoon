#!/usr/bin/env bash
# 全量检查一键入口。
#
# 为什么需要它：2026-09-03 连查四轮，每轮我都是「手工挑几项验证」，
# 结果每轮都漏掉没想到的路径。教训是——检查必须固化成脚本，靠人肉
# 记忆一定会漏。改完任何代码，跑这个。
#
# 用法：bash run_all_checks.sh [端口]
set -u
PORT="${1:-8899}"
cd "$(dirname "$0")"
BASE="http://127.0.0.1:${PORT}"
RC=0

step() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; RC=1; }

# ---------------------------------------------------------------- 0. 前置
step "0. 前置：静态检查（不需要服务在跑）"

if python3.11 -m pyflakes ./*.py modules/*/*.py 2>/dev/null \
     | grep -v "unable to detect undefined names" | grep -q .; then
  bad "pyflakes 有告警："
  python3.11 -m pyflakes ./*.py modules/*/*.py 2>/dev/null | head -10
else
  ok "pyflakes 干净"
fi

if python3.11 arities_check.py > /tmp/_arity.log 2>&1; then
  ok "返回值/解包一致性（含负向盲区已覆盖）"
else
  bad "返回值/解包不一致："
  grep -E "✗|⚠" /tmp/_arity.log | head -10
fi

# ---------------------------------------------------------------- 1. 服务
step "1. 服务可用性"
if curl -s -o /dev/null --max-time 3 "${BASE}/api/daily_signal"; then
  ok "服务已在 ${BASE}"
else
  echo "  拉起服务…"
  nohup python3.11 server.py "${PORT}" > /tmp/srv_check.log 2>&1 &
  for i in $(seq 1 20); do
    sleep 1
    curl -s -o /dev/null --max-time 2 "${BASE}/api/daily_signal" && break
  done
  if curl -s -o /dev/null --max-time 3 "${BASE}/api/daily_signal"; then
    ok "服务已启动"
  else
    bad "服务起不来，看 /tmp/srv_check.log"
    exit 1
  fi
fi
head -2 /tmp/srv_check.log 2>/dev/null | sed 's/^/  /'

# ---------------------------------------------------------------- 2. 信号
step "2. 当日信号自检"
if python3.11 selfcheck_signal.py > /tmp/_sig.log 2>&1; then
  ok "信号自检 $(grep -oE '通过 [0-9]+ / 失败 [0-9]+' /tmp/_sig.log | tail -1)"
else
  bad "信号自检失败："
  grep -E "✗" /tmp/_sig.log | head -10
fi

# ---------------------------------------------------------------- 3. API
step "3. API 全端点 × 参数矩阵"
if python3.11 test_api_matrix.py "${BASE}" > /tmp/_api.log 2>&1; then
  ok "API 矩阵 $(grep -oE '通过 [0-9]+ / 失败 [0-9]+' /tmp/_api.log | tail -1)"
else
  bad "API 矩阵失败："
  sed -n '/失败明细/,$p' /tmp/_api.log | head -15
fi

# ---------------------------------------------------------------- 4. 前端
step "4. 前端交互（真点按钮）"
if timeout 400 python3.11 test_ui_flow.py "${BASE}" > /tmp/_ui.log 2>&1; then
  ok "前端交互 $(grep -oE '通过 [0-9]+ / [0-9]+' /tmp/_ui.log | tail -1)"
else
  bad "前端交互失败："
  sed -n '/问题明细/,$p' /tmp/_ui.log | head -15
fi

# ---------------------------------------------------------------- 5. 离线版
step "5. 离线版（file:// 模式，只验渲染与 JS 错误）"
if timeout 200 python3.11 test_offline.py > /tmp/_off.log 2>&1; then
  ok "离线版正常"
else
  bad "离线版有问题："
  grep -E "✗|错误" /tmp/_off.log | head -10
fi

# ---------------------------------------------------------------- 汇总
step "汇总"
if [ "$RC" -eq 0 ]; then
  ok "全部检查通过"
else
  bad "存在问题，看上面各段输出"
fi
exit $RC
