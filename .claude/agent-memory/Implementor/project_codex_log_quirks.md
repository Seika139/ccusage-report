---
name: codex-log-quirks
description: Codex ロールアウトログの fork 挙動 — 履歴を丸ごと複製し、複製側のレコード timestamp を複製時刻に書き換える
metadata:
  type: project
---

Codex の fork（`session_meta.payload.forked_from_id` / `parent_thread_id` を持つロールアウト）は、親セッションの履歴を丸ごと複製したうえで、**複製されたレコードの `timestamp` を「複製した時刻」に書き換える**。イベント本来の発生時刻は payload 側のフィールド（`turn_aborted` なら `completed_at`、`task_started` なら `started_at`、いずれも UNIX 秒）にのみ残る。

実測（2026-06 の `~/.codex/sessions`）: 本来 2026-06-12T08:05:38Z の `turn_aborted` が、fork ファイル上では 2026-06-15T04:14:12Z として記録されていた。同一の `turn_aborted`（同じ `turn_id`）が親 + fork 3 つの計 4 ファイルに現れ、dedup しないと S7 の検出トークンが 35% 過大計上された（258,089,539 → 167,737,144）。

**Why:** ログ由来の集計で「期間フィルタの結果が `--since` の指定でぶれない」ことを保証するために必要。レコードの `timestamp` を信じると、fork がいつ行われたかによって過去の確定済み期間の検出量が動いてしまう。`turn_id` も 1 ファイル内で一意ではなく、全ファイル横断の集合で dedup しないと重複する。

**How to apply:** Codex ログから日付を取り出す実装・期間判定を書くときは、payload に本来の時刻を持つフィールドがないか先に確認し、あればそれを優先する。イベント単位の重複排除は `turn_id` を全ファイル横断で共有する（Claude の `uuid` とは形式が違うので集合は必ず別に持つ）。累積値（`total_token_usage`）の差分を取る実装では、期間フィルタで基準値のスナップショット更新をスキップしてはならない（差分が「累積 - 0」に膨らみ、実測で 9000 倍の過大計上になった）。
