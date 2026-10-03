#!/usr/bin/env bash
# 锐思投研 · 一键启动
#   ./run.sh          仅启动服务（读本地库）
#   ./run.sh --sync   先同步数据再启动
set -euo pipefail
cd "$(dirname "$0")"

PY="${RUISI_PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "找不到 python3，请先安装 Python 3.7+ 或设置 RUISI_PYTHON 环境变量" >&2
  exit 1
fi

if [[ "${1:-}" == "--sync" ]]; then
  echo "== 同步数据 =="
  "$PY" sync.py
  echo
fi

echo "== 启动本地服务 =="
exec "$PY" server.py
