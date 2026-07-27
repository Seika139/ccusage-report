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
mise run report --no-log-analysis      # 生ログ解析をスキップ（ccusage の集計のみ）
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
| `--no-log-analysis`   | 生ログ解析（無駄トークンの深掘り）をスキップ      | 解析する                                           |

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
4. **無駄なトークンの深掘り（生ログ解析）**
   - ccusage の集計値では見えない「支払ったが価値に結び付きにくかったトークン」を、ローカルのセッションログ（`~/.claude/projects` と `~/.codex/sessions`）から検出する。
   - **S1 巨大な tool 出力**: 1 回で 20,000 文字を超えた tool 結果。セッション序盤に入った巨大出力は以降の全ターンで `cache_read` として再課金されるため、以降のターン数（上限 15）で増幅した推定コストを出す。
   - **S6 コンテキスト圧縮で廃棄したトークン**: 自動圧縮（auto）で捨てられたコンテキスト量（`preTokens - postTokens`）。manual は利用者の意思による操作なので表には出すが合計には含めない。
   - **S3 サブエージェント委譲のオーバーヘッド**: `subagents/` 配下のログから委譲 1 件ずつのトークン消費を集計する。委譲そのものは無駄ではないため、「軽量用途の agentType（OutputSummarizer / Explore）に Opus を割り当てた」「ネスト深度が 3 以上」の 2 点だけを合計に数え、agentType 別の全体集計（件数 / 合計トークン / 平均）は参考行として出す。
   - **S2 セッション単位のキャッシュ非効率**: セッションごとの `cache_create / cache_read` が対象期間の中央値の 3 倍を超えたセッション。妥当な比率はモデルと作業内容で変わるため絶対閾値は使わず、期間内の分布に対する相対評価で判定する（サンプルが 5 件未満の期間は評価しない）。モデル単位でしか見ない「コスト削減の示唆」を、原因セッションまで分解したもの。
   - **S7 失敗・中断で捨てたトークン**: tool 呼び出しの失敗（`tool_result.is_error`）とターンの中断（Codex の `turn_aborted`）。計上するのはエラー本文そのものではなく「その失敗を受けてやり直すために再送されたコンテキスト」で、Claude はエラー直後の assistant レコードの入力側 usage、Codex は中断ターンの累積入力量の差分を代理指標に使う。クライアント側エラーの `<synthetic>` レコードは usage が全ゼロ（課金されていない）ため集計対象に入れていない。
   - **S8 reasoning トークン比率の外れ値（Codex）**: セッションごとの `reasoning_output_tokens / output_tokens` が、同じ (モデル, effort) のコホート中央値の 1.8 倍を超えたもの。妥当な比率は effort で大きく変わる（実測で xhigh の中央値 40% に対し high は 25%）ため絶対閾値は使わない。母集団は output が 10,000 トークン以上のセッションに限り、セッション数が 5 件未満のコホートは評価しない。effort が unknown のコホートはラベル無しのデフォルト設定であって設定ミスではないため、「effort を下げよ」という推奨はしない。
   - **S4 モデル選択の妥当性（確度: 低・参考値）**: assistant のターン数が 5 以下、トークン総量が 100,000 以下の短く小規模な作業に、期間内の最安モデルより明確に高価なモデル（Opus 等）を使ったセッション。これは**仮説**であり特定された無駄ではない。文脈収集・設計レビュー・一発で答えを出す難問は意図的に高価なモデルを使うため、大量のトークンを扱ったセッションは母集団から除外している。金額は「同じ作業を最安モデルで行えたと仮定した場合の理論上の削減余地の**上限値**」であり、無駄の合計金額にも示唆にも含めない。対象はトップレベルのセッションのみで、サブエージェントへの委譲は含まない（S3 が別に評価する）。ただしトップレベルであっても、設定で固定されたモデルによる自動化ワークフロー（セキュリティレビューの自動実行等）は同じ形で現れるため、「手動でモデルを選び間違えた」ことの証拠ではない。期間境界で切り詰められたセッションを「短く小規模な作業」と誤認しないため、開始日時が指定期間内に収まるセッションに限る。
   - **S5 重複 tool 呼び出し（確度: 低・参考値）**: 同一セッション内で (tool 名, 引数) が完全一致する呼び出しが 4 回以上繰り返されたパターン。同じパスに `Edit`/`Write` が介在した後の再読み込みは変更結果の正当な再確認なので除外している。それでもポーリングや意図的な再試行との区別は付かないため、金額ではなく**件数**として見る値である。金額は結果本文を保持しない設計のため 2 回目以降 1 回を 350 トークンと固定近似した参考値であり、厳密な計測ではない。無駄の合計金額にも示唆にも含めない。対象は Claude と Codex の双方で、Claude はサブエージェント（`subagents/` 配下）で発生した重複も呼び出し元の親セッションに合流させて数える（実装を委譲する運用では tool 呼び出しの大半がサブエージェント側で発生するため、親のログだけでは検出できない）。Codex は `call_id` でファイル内の重複記録を弾いて数える。
   - 単価は価格表をハードコードせず、ccusage の実コストからモデルごとに逆算している。
   - `--no-log-analysis` でスキップできる。解析が失敗してもレポート生成は続行する（警告を stderr に出すだけ）。
5. **モデル別サマリ表**（Input / Output / Cache Read / Cache Create / Cost / Share）

## 注意点

- ccusage は実際の請求額ではなく、ログ上のトークン数 × 公開単価による**推定コスト**を返す。
- 「無駄なトークンの深掘り」の金額はすべて**推定値**であり請求額ではない。廃棄・再送されたコンテキストは `cache_read` 相当（基準単価の 0.1 倍）で換算している（S2 の cache_create 超過分のみ 1.25 倍、S8 の reasoning 超過分のみ 5 倍）。
- S3 のトークン数はサブエージェントログを 2 段階 dedup して自前集計した値である。親セッションの `toolUseResult.totalTokens` は最終ターン 1 回分の `usage` の和でしかなく、実消費の 2〜80 分の 1 になるため使っていない（ログが読めない場合のみフォールバックし「推定」と表示する）。
- 生ログ解析は**ローカルのログファイルを読むだけ**で、外部への送信は一切行わない。
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
