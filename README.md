# ccusage-report

<div align="center">
  <a href="https://github.com/Seika139/ccusage-report/actions/workflows/uv-qualify.yml">
    <img alt="Qualify Code" src="https://github.com/Seika139/ccusage-report/actions/workflows/uv-qualify.yml/badge.svg">
  </a>
  <a href="https://github.com/Seika139/ccusage-report/actions/workflows/lint-markdown.yml">
    <img alt="Lint Markdown" src="https://github.com/Seika139/ccusage-report/actions/workflows/lint-markdown.yml/badge.svg">
  </a>
  <a href="https://github.com/Seika139/ccusage-report/actions/workflows/lint-yaml.yml">
    <img alt="Lint YAML" src="https://github.com/Seika139/ccusage-report/actions/workflows/lint-yaml.yml/badge.svg">
  </a>
  <a href="https://github.com/Seika139/ccusage-report/actions/workflows/shellcheck.yml">
    <img alt="ShellCheck" src="https://github.com/Seika139/ccusage-report/actions/workflows/shellcheck.yml/badge.svg">
  </a>
</div>

[ccusage](https://github.com/ryoppippi/ccusage) の JSON 出力を **モデル別 × 日次** に集計し、グラフとコスト削減示唆を含む自己完結 HTML レポートを生成する個人ツール。

サーバ不要。生成された 1 枚の HTML をブラウザで開くだけ（グラフは Chart.js を CDN から SRI 検証付きで読み込む）。

## 必要なもの

- `ccusage` が PATH にあること（`ccusage --version` で確認）
- `uv`（Python 標準ライブラリのみ使用、追加依存なし）

## 使い方

mise タスク経由が基本（`mise run report --help` で flag 一覧を表示）:

```bash
mise run init                          # 初回: uv sync で環境構築
mise run report                        # 直近30日・全プロバイダで集計しブラウザを開く
mise run report --days 7               # 直近7日
mise run report --all                  # 全期間
mise run report --since 20260601       # 開始日を明示指定
mise run report --provider claude      # claude のみ
mise run report --no-open              # 開かずファイル出力のみ
```

uv で直接呼ぶ場合:

```bash
uv run report.py --provider codex      # codex のみ
```

### 主なオプション

| フラグ                | 説明                                              | 既定                                               |
| --------------------- | ------------------------------------------------- | -------------------------------------------------- |
| `--days N`            | 直近 N 日を集計（`--since` 未指定時）             | 30                                                 |
| `--all`               | 全期間を集計                                      | off                                                |
| `--since` / `--until` | 期間 `YYYYMMDD`（`--since` は `--days` より優先） | 直近30日                                           |
| `--provider`          | `claude` / `codex` / `all`                        | `all`                                              |
| `-o, --output`        | 出力 HTML パス                                    | `out/ccusage-report_<provider>_<開始>_<終了>.html` |
| `--no-open`           | 生成後ブラウザを開かない                          | 開く                                               |

期間指定の優先順位は `--all` > `--since` > `--days` > 既定30日。全期間だと棒グラフが
細くなりすぎるため、既定を直近30日に絞っている。

出力先は既定でリポ内の `out/`（git 追跡外）。ファイル名には**実際に集計された期間**
（ccusage が返したデータの最初と最後の日）と provider が入るため、`--since` を省略しても
正確な期間が反映される。`out/` が無ければ実行時に自動作成される。
`-o` で明示指定した場合のみカレントディレクトリ基準で解決される。

## レポートの内容

1. **コスト削減の示唆**（ルールベース・LLM 不使用）
   - 特定 Opus モデルが総コストの 70% 超 → 軽量モデル移行余地の警告
   - Cache Create > Cache Read のモデル → キャッシュ崩壊（セッション細切れ）の疑い
   - 直近平均日次コストからの 30 日換算予測
2. **モデル別 × 日次の推移グラフ**（全プロバイダ横断）
   - Claude（opus / sonnet / haiku）と Codex（gpt-5.x）等を一画面に表示
   - セレクタで指標を切替: Cost / Input / Output / Cache Create / Cache Read / Total Tokens（既定 Total Tokens）
   - 積み上げ表示の ON/OFF も切替可能
3. **日次明細テーブル**（ccusage daily 風）
   - 各日について All 行（全モデル合算）＋モデル別行を表示
   - 列: Input / Output / Cache Create / Cache Read / Total Tokens / Cost
4. **モデル別サマリ表**（Input / Output / Cache Read / Cache Create / Cost / Share）

## 注意点

- ccusage は実際の請求額ではなく、ログ上のトークン数 × 公開単価による**推定コスト**を返す。
- 月末予測の分母は対象期間の全日数（未使用日も含む）。burn rate として見るなら `--since` で直近に絞ると現実的。
- Chart.js のバージョンは SRI ハッシュと対で固定している。更新時は `report.py` の `src` / `integrity` を両方差し替えること。

## 開発

### push ルール（ブランチ保護）

個人ツールのため `main` への直接 push を許可している。PR は必須ではない。

- **`main` へ直接 push 可**（PR レビュー・required status checks によるゲートはなし）。
- **force-push は禁止**（`non_fast_forward`）。履歴の破壊的な書き換えはできない。
- **`main` ブランチの削除は禁止**（`deletion`）。
- CI（`uv-qualify` / `lint-markdown` / `lint-yaml` / `shellcheck`）は push 時に実行されるが、push やマージをブロックしない。バッジで結果を確認する。

ブランチ保護ルールは GitHub UI ではなく [`Seika139/.github`](https://github.com/Seika139/.github) の Terraform（`terraform/github/locals.tf` の `ccusage-report` エントリ）で管理している。変更する場合は同リポジトリで `mise run terra-plan` / `terra-apply` を実行すること。
