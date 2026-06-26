#!/usr/bin/env bash

#MISE description="format"
#MISE quiet=true

# 途中でエラーが出ても他のファイルのフォーマットは続行するため、set -e は使用しない
set -uo pipefail

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/common.sh"

print_blue "formatting Markdown, TOML, JSON with dprint"$'\n'
dprint fmt

print_blue "formatting Python scripts with ruff"$'\n'
uv run ruff format report.py tests/
uv run ruff check --fix report.py tests/

# shfmt でフォーマットするファイル・ディレクトリのリスト
shell_files=(
  mise/common.sh
  mise/tasks/
)

print_blue "formatting shell scripts with shfmt"$'\n'
find "${shell_files[@]}" -type f \
  \( -name "*.sh" -o -name "*.bash" \) -print0 |
  xargs -0 shfmt -w

# yamllint には自動修正機能が無いため、parsable 出力を解析して
# `trailing-spaces` 違反の行だけ sed で行末スペースを除去する。
# line-length / indentation など他の違反は dot-lint 側で検出・手動修正する。
print_blue "formatting YAML files with yamllint + sed"$'\n'
yamllint -f parsable . | while IFS= read -r finding; do
  # 形式: file:line:col: [level] message (rule-name)
  if [[ "$finding" =~ ^([^:]+):([0-9]+):[0-9]+:\ .*\(trailing-spaces\)$ ]]; then
    file="${BASH_REMATCH[1]}"
    line_num="${BASH_REMATCH[2]}"
    # `-i.bak` は BSD/GNU 双方で in-place 編集として動く portable 形式。
    # `-E` (ERE) で正規表現方言差を回避する。
    if sed -i.bak -E "${line_num}s/[[:space:]]+\$//" "${file}"; then
      rm -f "${file}.bak"
      print_green "Fixed trailing spaces: ${file}:${line_num}"$'\n'
    else
      print_red "Failed to fix trailing spaces: ${file}:${line_num}"$'\n'
    fi
  fi
done
