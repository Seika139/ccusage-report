#!/usr/bin/env bash

#MISE hide=true

# 色付けヘルパー関数
# 基本色（ANSI 16色）
print_red() { printf '\e[31m%s\e[0m' "$*"; }
print_green() { printf '\e[32m%s\e[0m' "$*"; }
print_yellow() { printf '\e[33m%s\e[0m' "$*"; }
print_blue() { printf '\e[34m%s\e[0m' "$*"; }
print_magenta() { printf '\e[35m%s\e[0m' "$*"; }
print_cyan() { printf '\e[36m%s\e[0m' "$*"; }
# スタイル
print_dim() { printf '\e[2m%s\e[0m' "$*"; }
print_bold() { printf '\e[1m%s\e[0m' "$*"; }
# RGB カスタムカラー（引数: R G B テキスト）
print_rgb() {
  local r=$1 g=$2 b=$3
  shift 3
  printf '\e[38;2;%d;%d;%dm%s\e[0m' "$r" "$g" "$b" "$*"
}
# よく使うカスタムカラー
print_orange() { print_rgb 250 180 100 "$*"; }
print_soft_green() { print_rgb 150 255 200 "$*"; }
print_soft_blue() { print_rgb 160 190 255 "$*"; }
print_pink() { print_rgb 255 150 200 "$*"; }
