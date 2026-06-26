#!/usr/bin/env bash

#MISE description="ccusage 使用状況の HTML レポートを生成する"
#USAGE flag "--provider <provider>" help="集計対象プロバイダ" {
#USAGE   choices "all" "claude" "codex"
#USAGE }
#USAGE flag "--days <days>" help="直近 N 日を集計（--since 未指定時。既定 30）"
#USAGE flag "--all" help="全期間を集計する"
#USAGE flag "--since <since>" help="開始日 YYYYMMDD（--days より優先）"
#USAGE flag "--until <until>" help="終了日 YYYYMMDD"
#USAGE flag "-o --output <output>" help="出力 HTML パス（既定は out/ 配下に自動命名）"
#USAGE flag "--no-open" help="生成後にブラウザを開かない"

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# shellcheck disable=SC1091
source "${ROOT_DIR}/mise/common.sh"

# mise usage の値を report.py の引数へ組み立てる。デフォルト値の正本は report.py 側に
# 一本化するため、ここでは未指定の flag は引数として渡さない（値が空なら付与しない）。
args=()
[ -n "${usage_provider:-}" ] && args+=(--provider "${usage_provider}")
[ -n "${usage_days:-}" ] && args+=(--days "${usage_days}")
[ "${usage_all:-false}" = "true" ] && args+=(--all)
[ -n "${usage_since:-}" ] && args+=(--since "${usage_since}")
[ -n "${usage_until:-}" ] && args+=(--until "${usage_until}")
[ -n "${usage_output:-}" ] && args+=(--output "${usage_output}")
[ "${usage_no_open:-false}" = "true" ] && args+=(--no-open)

print_blue "generating ccusage report"$'\n'
uv run report.py "${args[@]}"
