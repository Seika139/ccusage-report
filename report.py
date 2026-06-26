#!/usr/bin/env python3
"""ccusage の JSON をモデル別×日次に集計し、自己完結 HTML レポートを生成する。

依存ゼロ（標準ライブラリのみ）。グラフは Chart.js を CDN から読み込み、
データは HTML に直書きするためサーバ不要で `open` するだけで閲覧できる。

グラフは指標（Cost / Input / Output / Cache Create / Cache Read / Total Tokens）を
セレクタで切替でき、全プロバイダのモデルを横断表示する。

使い方:
    uv run report.py                       # 全プロバイダ・全期間
    uv run report.py --since 20260601      # 期間指定
    uv run report.py --provider claude     # claude / codex / all（既定 all）
    uv run report.py -o /tmp/report.html   # 出力先
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import webbrowser
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# --since/--days/--all いずれも未指定のときに集計する既定日数。
# 全期間だと棒グラフが細くなりすぎるため、直近 N 日に絞る。
DEFAULT_SINCE_DAYS = 30

# デフォルト出力ディレクトリ。cwd ではなくスクリプト自身の場所を基準にし、
# どこから実行してもリポ内の out/（.gitignore 済み）に生成物が集まるようにする。
OUTPUT_DIR = Path(__file__).resolve().parent / "out"

# Claude 系モデルのプレフィックス。ccusage は Codex(gpt-5.5) 等も混在で返すため、
# provider フィルタはモデル名のプレフィックスで判定する。
PROVIDER_PREFIXES = {
    "claude": ("claude",),
    "codex": ("gpt-", "o1", "o3", "o4"),
}

# グラフで切替表示する指標の定義。(key, ラベル, ModelAgg 属性) の順。
# "total" は派生指標で属性を持たない（他 4 指標の和として算出する）。
METRICS: list[tuple[str, str, str | None]] = [
    ("cost", "Cost (USD)", "cost"),
    ("input", "Input", "input"),
    ("output", "Output", "output"),
    ("cache_create", "Cache Create", "cache_create"),
    ("cache_read", "Cache Read", "cache_read"),
    ("total", "Total Tokens", None),
]


@dataclass
class ModelAgg:
    """1 モデル分の累積トークンとコスト。"""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_create: int = 0
    cost: float = 0.0

    def add(self, b: dict[str, Any]) -> None:
        self.input += b.get("inputTokens", 0)
        self.output += b.get("outputTokens", 0)
        self.cache_read += b.get("cacheReadTokens", 0)
        self.cache_create += b.get("cacheCreationTokens", 0)
        self.cost += b.get("cost", 0.0) or 0.0

    def add_agg(self, other: ModelAgg) -> None:
        """別の ModelAgg を加算する（日次 All 行などの合算用）。"""
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_create += other.cache_create
        self.cost += other.cost

    @property
    def total(self) -> int:
        """Total Tokens（入出力 + キャッシュの和）。"""
        return self.input + self.output + self.cache_read + self.cache_create

    def metric(self, key: str) -> float:
        """指標キーに対応する値を返す（"total" は派生算出）。"""
        if key == "total":
            return self.total
        return float(getattr(self, key))


@dataclass
class Report:
    days: list[str] = field(default_factory=list)
    # model -> ModelAgg（全期間合計）
    by_model: dict[str, ModelAgg] = field(default_factory=lambda: defaultdict(ModelAgg))
    # day -> model -> ModelAgg（日次×モデルの内訳。指標切替グラフの元データ）
    by_day_model: dict[str, dict[str, ModelAgg]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(ModelAgg))
    )


def resolve_since(
    since: str | None, days: int | None, show_all: bool, today: date
) -> str | None:
    """集計開始日 (--since 相当) を決定する。

    優先順位: --all（全期間=None）> --since 明示指定 > --days からの逆算。
    どれも該当しなければ DEFAULT_SINCE_DAYS 日前を YYYYMMDD で返す。
    """
    if show_all:
        return None
    if since:
        return since
    span = days if days is not None else DEFAULT_SINCE_DAYS
    return (today - timedelta(days=span)).strftime("%Y%m%d")


def run_ccusage(since: str | None, until: str | None) -> dict[str, Any]:
    """ccusage daily -j を実行して JSON を返す。"""
    cmd = ["ccusage", "daily", "-j"]
    if since:
        cmd += ["--since", since]
    if until:
        cmd += ["--until", until]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        sys.exit("error: `ccusage` が見つかりません。PATH を確認してください。")
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: ccusage 実行に失敗しました\n{e.stderr}")
    result: dict[str, Any] = json.loads(proc.stdout)
    return result


def matches_provider(model: str, provider: str) -> bool:
    if provider == "all":
        return True
    return model.startswith(PROVIDER_PREFIXES.get(provider, ()))


def aggregate(data: dict[str, Any], provider: str) -> Report:
    rep = Report()
    for day in data.get("daily", []):
        period = day.get("period", "")
        rep.days.append(period)
        for b in day.get("modelBreakdowns", []):
            model = b.get("modelName", "unknown")
            if not matches_provider(model, provider):
                continue
            rep.by_model[model].add(b)
            rep.by_day_model[period][model].add(b)
    return rep


def default_output_path(rep: Report, provider: str) -> Path:
    """実際に集計された期間からデフォルト出力パスを組み立てる。

    --since/--until を省略しても rep.days（ccusage が返した実データの日付）から
    正確な期間をファイル名に反映する。例: ccusage-report_all_2025-10-09_2026-06-26.html
    """
    start = rep.days[0] if rep.days else "unknown"
    end = rep.days[-1] if rep.days else "unknown"
    return OUTPUT_DIR / f"ccusage-report_{provider}_{start}_{end}.html"


def build_insights(rep: Report) -> list[dict[str, str]]:
    """ルールベースの削減示唆を生成する（LLM 不使用・決定的）。"""
    insights: list[dict[str, str]] = []
    total_cost = sum(m.cost for m in rep.by_model.values())
    if total_cost <= 0:
        return [{"level": "info", "text": "対象期間にコストが検出されませんでした。"}]

    # (1) 高コストモデルの偏り: Opus が総額の 70% 超なら軽量モデル移行余地。
    for model, agg in rep.by_model.items():
        share = agg.cost / total_cost
        if "opus" in model and share >= 0.70:
            insights.append(
                {
                    "level": "warn",
                    "text": f"{model} が総コストの {share:.0%} を占めています。"
                    "要約・lint・整形など軽い作業を Haiku/Sonnet に移すと削減余地があります。",
                }
            )

    # (2) キャッシュ崩壊の検出: Cache Create が Cache Read を上回るモデル。
    #     通常は Read >> Create が理想（再利用が効いている）。逆転は非効率の兆候。
    for model, agg in rep.by_model.items():
        if agg.cache_create > 0 and agg.cache_create > agg.cache_read:
            insights.append(
                {
                    "level": "warn",
                    "text": f"{model} はキャッシュ生成({agg.cache_create:,})が"
                    f"再利用({agg.cache_read:,})を上回っています。"
                    "セッションを細切れにせず長く保つとキャッシュ再利用が効きます。",
                }
            )

    # (3) 月末予測: 直近の平均日次コストから 30 日換算。
    n_days = max(len(rep.days), 1)
    daily_avg = total_cost / n_days
    insights.append(
        {
            "level": "info",
            "text": f"対象 {n_days} 日の平均日次コストは ${daily_avg:,.2f}。"
            f"このペースなら 30 日換算で約 ${daily_avg * 30:,.2f} です。",
        }
    )

    if not any(i["level"] == "warn" for i in insights):
        insights.insert(
            0, {"level": "ok", "text": "顕著なコスト非効率は検出されませんでした。"}
        )
    return insights


def palette(n: int) -> list[str]:
    base = [
        "#6366f1",
        "#ec4899",
        "#f59e0b",
        "#10b981",
        "#3b82f6",
        "#ef4444",
        "#8b5cf6",
        "#14b8a6",
        "#f97316",
        "#84cc16",
    ]
    return [base[i % len(base)] for i in range(n)]


def render_daily_table(rep: Report, color_of: dict[str, str]) -> str:
    """ccusage daily 風の日次テーブルを生成する。

    各日について All 行（その日の全モデル合算）を出し、続けてモデル別行を
    コスト降順で並べる。新しい日付が上に来るよう降順で表示する。
    """
    cols = ("input", "output", "cache_create", "cache_read", "total")

    def cells(a: ModelAgg) -> str:
        nums = "".join(f"<td class='num'>{int(a.metric(c)):,}</td>" for c in cols)
        return f"{nums}<td class='num'>${a.cost:,.2f}</td>"

    blocks: list[str] = []
    for day in reversed(rep.days):
        models = rep.by_day_model.get(day, {})
        if not models:
            continue
        day_all = ModelAgg()
        for agg in models.values():
            day_all.add_agg(agg)
        rows = [f"<tr class='day-all'><td>{day}</td><td>All</td>{cells(day_all)}</tr>"]
        rows += [
            f"<tr><td></td>"
            f"<td><span class='dot' style='background:{color_of.get(m, '#999')}'></span>{m}</td>"
            f"{cells(models[m])}</tr>"
            for m in sorted(models, key=lambda m: -models[m].cost)
        ]
        blocks.append("".join(rows))
    return "".join(blocks)


def render_html(rep: Report, provider: str) -> str:
    models = sorted(rep.by_model, key=lambda m: -rep.by_model[m].cost)
    colors = palette(len(models))
    color_of = dict(zip(models, colors, strict=True))

    # 指標 × モデルごとに日次系列を作り、全指標分を HTML に埋め込む。
    # JS 側でセレクタの指標キーに応じて該当 datasets に差し替える（再生成不要）。
    def series(metric_key: str, model: str) -> list[float]:
        return [
            round(rep.by_day_model[d][model].metric(metric_key), 4)
            if model in rep.by_day_model.get(d, {})
            else 0.0
            for d in rep.days
        ]

    datasets_by_metric = {
        key: [
            {"label": m, "data": series(key, m), "backgroundColor": color_of[m]}
            for m in models
        ]
        for key, _label, _attr in METRICS
    }
    metric_labels = {key: label for key, label, _attr in METRICS}

    total_cost = sum(m.cost for m in rep.by_model.values())
    rows = "".join(
        f"<tr><td><span class='dot' style='background:{color_of[m]}'></span>{m}</td>"
        f"<td class='num'>{a.input:,}</td><td class='num'>{a.output:,}</td>"
        f"<td class='num'>{a.cache_read:,}</td><td class='num'>{a.cache_create:,}</td>"
        f"<td class='num'>${a.cost:,.2f}</td>"
        f"<td class='num'>{(a.cost / total_cost if total_cost else 0):.1%}</td></tr>"
        for m, a in ((m, rep.by_model[m]) for m in models)
    )

    insights = build_insights(rep)
    insight_html = "".join(
        f"<li class='ins-{i['level']}'>{i['text']}</li>" for i in insights
    )

    chart_payload = json.dumps(
        {
            "labels": rep.days,
            "datasetsByMetric": datasets_by_metric,
            "metricLabels": metric_labels,
        },
        ensure_ascii=False,
    )
    # 指標セレクタの option 要素。既定の選択は Total Tokens。
    metric_options = "".join(
        f"<option value='{key}'{' selected' if key == 'total' else ''}>{label}</option>"
        for key, label, _attr in METRICS
    )

    daily_table = render_daily_table(rep, color_of)

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>ccusage レポート ({provider})</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.min.js"
        integrity="sha384-b0GXujLkk9eYYSmcSfoyZbfyElGAQnDyY0skCHSG6w3JgTMFnz11ggrTAr7seu9f"
        crossorigin="anonymous"></script>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 980px; color: #1f2937; }}
  h1 {{ font-size: 1.4rem; }}
  .sub {{ color: #6b7280; font-size: .85rem; }}
  .ctrl {{ font-size: .9rem; }}
  .ctrl select {{ margin: 0 1rem 0 .3rem; padding: .2rem .4rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; font-size: .9rem; }}
  th, td {{ border-bottom: 1px solid #e5e7eb; padding: .5rem .6rem; text-align: left; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .dot {{ display: inline-block; width: .7rem; height: .7rem; border-radius: 2px; margin-right: .4rem; }}
  ul.insights {{ list-style: none; padding: 0; }}
  ul.insights li {{ padding: .6rem .8rem; border-radius: 6px; margin: .4rem 0; }}
  .ins-warn {{ background: #fef3c7; border-left: 4px solid #f59e0b; }}
  .ins-ok   {{ background: #d1fae5; border-left: 4px solid #10b981; }}
  .ins-info {{ background: #e0e7ff; border-left: 4px solid #6366f1; }}
  .total {{ font-weight: 600; }}
  table.daily {{ font-size: .8rem; }}
  table.daily td, table.daily th {{ padding: .35rem .5rem; }}
  table.daily tr.day-all {{ background: #f3f4f6; font-weight: 600; border-top: 2px solid #d1d5db; }}
</style>
</head>
<body>
  <h1>ccusage 使用状況レポート</h1>
  <p class="sub">provider: <b>{provider}</b> / 期間: {rep.days[0] if rep.days else "-"} 〜 {rep.days[-1] if rep.days else "-"} / 総コスト: <b>${total_cost:,.2f}</b></p>

  <h2>コスト削減の示唆</h2>
  <ul class="insights">{insight_html}</ul>

  <h2>モデル別 × 日次の推移</h2>
  <p class="ctrl">指標:
    <select id="metric">{metric_options}</select>
    <label><input type="checkbox" id="stacked" checked> 積み上げ</label>
  </p>
  <canvas id="chart" height="120"></canvas>

  <h2>日次明細（All / モデル別）</h2>
  <table class="daily">
    <thead><tr><th>Date</th><th>Model</th><th class="num">Input</th><th class="num">Output</th>
      <th class="num">Cache Create</th><th class="num">Cache Read</th>
      <th class="num">Total Tokens</th><th class="num">Cost</th></tr></thead>
    <tbody>{daily_table}</tbody>
  </table>

  <h2>モデル別サマリ</h2>
  <table>
    <thead><tr><th>Model</th><th class="num">Input</th><th class="num">Output</th>
      <th class="num">Cache Read</th><th class="num">Cache Create</th>
      <th class="num">Cost</th><th class="num">Share</th></tr></thead>
    <tbody>{rows}
      <tr class="total"><td>合計</td><td class="num"></td><td class="num"></td>
        <td class="num"></td><td class="num"></td><td class="num">${total_cost:,.2f}</td><td class="num">100%</td></tr>
    </tbody>
  </table>

<script>
const payload = {chart_payload};
const fmt = (key, v) => key === 'cost' ? '$' + v.toFixed(2) : v.toLocaleString();

const sel = document.getElementById('metric');
const stackedBox = document.getElementById('stacked');

const chart = new Chart(document.getElementById('chart'), {{
  type: 'bar',
  data: {{ labels: payload.labels, datasets: payload.datasetsByMetric[sel.value] }},
  options: {{ responsive: true }}
}});

function applyMetric() {{
  const key = sel.value;
  const stacked = stackedBox.checked;
  chart.data.datasets = payload.datasetsByMetric[key];
  chart.options.scales = {{
    x: {{ stacked }},
    y: {{ stacked, title: {{ display: true, text: payload.metricLabels[key] }} }}
  }};
  chart.options.plugins = {{
    tooltip: {{ callbacks: {{ label: c => c.dataset.label + ': ' + fmt(key, c.parsed.y) }} }}
  }};
  chart.update();
}}

sel.addEventListener('change', applyMetric);
stackedBox.addEventListener('change', applyMetric);
applyMetric();
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="ccusage JSON から HTML レポートを生成")
    ap.add_argument("--since", help="開始日 YYYYMMDD（--days より優先）")
    ap.add_argument("--until", help="終了日 YYYYMMDD")
    ap.add_argument(
        "--days",
        type=int,
        help=f"直近 N 日を集計（--since 未指定時の既定: {DEFAULT_SINCE_DAYS}）",
    )
    ap.add_argument(
        "--all", action="store_true", dest="show_all", help="全期間を集計する"
    )
    ap.add_argument(
        "--provider",
        default="all",
        choices=["claude", "codex", "all"],
        help="集計対象プロバイダ (default: all)",
    )
    ap.add_argument(
        "-o",
        "--output",
        help=f"出力 HTML パス (default: {OUTPUT_DIR}/ccusage-report_<provider>_<開始>_<終了>.html)",
    )
    ap.add_argument("--no-open", action="store_true", help="生成後にブラウザを開かない")
    args = ap.parse_args()

    since = resolve_since(args.since, args.days, args.show_all, date.today())
    data = run_ccusage(since, args.until)
    rep = aggregate(data, args.provider)
    if not rep.by_model:
        sys.exit(f"対象 provider='{args.provider}' のデータがありませんでした。")

    html = render_html(rep, args.provider)
    # 未指定なら実集計期間からスクリプト基準の out/ に命名、指定時は cwd 基準で解決する。
    out = (
        default_output_path(rep, args.provider)
        if args.output is None
        else Path(args.output).resolve()
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"生成しました: {out}")

    if not args.no_open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
