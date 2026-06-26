#!/usr/bin/env bash

#MISE description="lint"
#MISE quiet=true

# 途中でエラーが出ても他のファイルのリントは続行するため、set -e は使用しない
set -uo pipefail

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/common.sh"

print_blue "linting Markdown, TOML, JSON with dprint"$'\n'
dprint check

print_blue "linting Python scripts with ruff"$'\n'
uv run ruff check report.py tests/

print_blue "Lint shell scripts with shfmt & shellcheck"$'\n'
shellcheck_files=()
while IFS= read -r -d '' file; do
  shellcheck_files+=("$file")
done < <(find . -type f \( -name "*.sh" -o -name "*.bash" \) -not -path "./.venv/*" -not -path "./mypy_cache/*" -not -path "./.git/*" -not -path "./.pytest_cache/*" -not -path "./ruff_cache/*" -print0)
if [ "${shellcheck_files[0]+_}" ]; then
  shfmt -d "${shellcheck_files[@]}"
  shellcheck -x -P SCRIPTDIR "${shellcheck_files[@]}"
else
  print_red "No shell scripts found; skipping shellcheck."$'\n'
fi

print_blue "linting YAML files with yamllint"$'\n'
yamllint .
