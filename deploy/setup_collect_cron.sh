#!/usr/bin/env bash
# ============================================================
# 在部署服务器（裸机 Linux：Nginx + Gunicorn + systemd）上注册
# 「每个交易日收盘后自动采集」等定时任务。
#
# 用法（在部署服务器上，root 或具有 crontab 权限的用户执行）：
#   sudo ./deploy/setup_collect_cron.sh
#
# 行为：
#   1. 自动定位项目根目录（脚本上一级）与 venv python（缺省 venv/bin/python）。
#   2. 把本脚本管理的任务（带 "# [kzz]" 标记）写入当前用户的 crontab，
#      并先清掉旧的同标记行 —— 故可重复执行、幂等不重复。
#   3. collect_daily 每周一至周五 16:35 运行；脚本内部还会再跳过周末/法定节假日，
#      并在 collect_runs / collect_steps 表记录结构化日志，管理员后台
#      /admin/collect-logs 可查每步成败与错误原文。
#
# 注意：宝塔面板部署请用「计划任务」图形界面（见 BAOTA.md），不要跑本脚本，
#       否则会与宝塔的计划任务重复。
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
VENV_PY="$APP_DIR/venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  VENV_PY="$(command -v python3 || echo python3)"
  echo "[warn] 未找到 $APP_DIR/venv/bin/python，改用 $VENV_PY"
fi

MARK="# [kzz]"
TMP="$(mktemp)"

# 保留现有 crontab 中「非本脚本管理」的行
{ crontab -l 2>/dev/null || true; } | grep -vF "$MARK" > "$TMP" || true

# 追加本脚本管理的四行（占位符稍后替换；date 部分保持字面量供 cron 自己展开）
cat >> "$TMP" <<'EOF'
# [kzz] 公告采集 每日08:30（强赎/不强赎/下修/即将发行）
30 8 * * * cd __APP_DIR__ && __VENV_PY__ fetch_announcements.py --clear >> __APP_DIR__/ann_cron.log 2>&1

# [kzz] 每日收盘总采集 16:35（周一至周五；内部跳过非交易日；结构化日志见后台 /admin/collect-logs）
35 16 * * 1-5 cd __APP_DIR__ && __VENV_PY__ collect_daily.py >> __APP_DIR__/collect_cron.log 2>&1

# [kzz] 双低策略每周轮动 每周一16:40
40 16 * * 1 cd __APP_DIR__ && __VENV_PY__ rotate_double_low.py >> __APP_DIR__/_double_low.log 2>&1

# [kzz] 数据库每日备份 04:00（保留14天）
0 4 * * * mkdir -p __APP_DIR__/backups && cp __APP_DIR__/cb_holders.db __APP_DIR__/backups/cb_holders.db.$(date +\%F) && find __APP_DIR__/backups -name 'cb_holders.db.*' -mtime +14 -delete
EOF

sed -i "s#__APP_DIR__#$APP_DIR#g; s#__VENV_PY__#$VENV_PY#g" "$TMP"
crontab "$TMP"
rm -f "$TMP"

echo "已安装/更新定时任务（collect_daily 每个交易日 16:35 自动采集）："
echo "  - 项目目录 : $APP_DIR"
echo "  - Python   : $VENV_PY"
echo "  - 查看     : crontab -l"
echo "  - 日志     : 后台 /admin/collect-logs（结构化）+ $APP_DIR/collect_cron.log（原始输出）"
