#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
  elif [[ -x "${ROOT_DIR}/.venv/Scripts/python.exe" ]]; then
    PYTHON_BIN="${ROOT_DIR}/.venv/Scripts/python.exe"
  else
    PYTHON_BIN="python"
  fi
fi
HOST="${GEO_HOST:-127.0.0.1}"
INDEXING_PORT="${GEO_INDEXING_PORT:-8010}"
MATERIAL_PORT="${MATERIAL_PARSER_PORT:-8020}"
ARTICLE_PORT="${ARTICLE_GENERATOR_PORT:-8030}"

pids=()

start_service() {
  local name="$1"
  local workdir="$2"
  local app="$3"
  local port="$4"

  echo "Starting ${name}: http://${HOST}:${port}"
  (
    cd "${workdir}"
    "${PYTHON_BIN}" -m uvicorn "${app}" --host "${HOST}" --port "${port}"
  ) &
  pids+=("$!")
}

cleanup() {
  echo "Stopping services..."
  for pid in "${pids[@]}"; do
    kill "${pid}" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

start_service "geo-indexing" "${ROOT_DIR}/indexing_test" "api:app" "${INDEXING_PORT}"
start_service "material-parser" "${ROOT_DIR}" "material_parser.api:app" "${MATERIAL_PORT}"
start_service "article-generator" "${ROOT_DIR}" "article_generator.api:app" "${ARTICLE_PORT}"

cat <<EOF

All services are starting. Press Ctrl+C to stop.
Health checks:
  收录测试:   http://${HOST}:${INDEXING_PORT}/health
  资料解析:   http://${HOST}:${MATERIAL_PORT}/health
  文章生成:   http://${HOST}:${ARTICLE_PORT}/health

EOF

wait
