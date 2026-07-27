#!/usr/bin/env python3
"""Claude Code / Codex の生ログを解析し「無駄なトークン使用」を検出する。

`report.py` が読む ccusage の集計済み JSON では「いくら使ったか」しか分からない。
本モジュールはローカルのセッションログ（`~/.claude/projects/**/*.jsonl` と
`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`）を直接走査し、
1 tool 呼び出し / 1 圧縮イベント単位まで降りて無駄を推定する。

公開 API は 2 つだけ:

- `build_rate_table(rep)` — ccusage の実コストからモデル別単価を逆算する
- `collect_log_stats(rates=..., since=..., until=..., provider=...)` — 解析結果を返す

実装済みのシグナルは S1（巨大な tool 出力）、S6（圧縮による廃棄）、
S3（サブエージェント委譲のオーバーヘッド）、S3R（agentType 別内訳の参考表）、
S2（セッション単位のキャッシュ非効率）、S7（失敗・中断で捨てたトークン）、
S8（reasoning トークン比率 vs effort 設定）、S4（モデル選択の妥当性・参考値）、
S5（重複 tool 呼び出し・参考値）。
外部送信は一切行わず、ローカルファイルを読むだけである。
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

# --- 調整可能な定数 -------------------------------------------------------
# 実測（2026-07 のログ）で 20,000 文字を超える tool 結果は 209 件に絞られ、
# 上位は明確に見直し対象（巨大ファイルの全文 Read 等）だった。
# これより小さくすると通常の Read/Grep が大量に混ざりノイズになる。
S1_MIN_CHARS = 20_000

# 巨大出力はセッション序盤に入ると以降の全ターンで cache_read として再課金される。
# ただしキャッシュの TTL 失効はログから判別できないため、放置されたセッションを
# 過大評価しないよう増幅倍率に上限を設ける。15 は「1 つの作業単位で現実的に
# 続くターン数」として選定（10 では過小、20 超では TTL 失効の疑いが強い）。
S1_MAX_TURNS = 15

# 文字数からトークン数への近似換算。英日混在のログでは 1 トークン ≒ 4 文字。
# Codex の `Original token count: N` が取れる場合はそちらを優先する。
CHARS_PER_TOKEN = 4

# 廃棄済みコンテキストは cache_read 経由で課金されていた分として換算する。
# `RATE_CACHE_READ_MULT` と同値だが、用途が異なるため定数を分けている。
WASTE_CACHE_READ_MULT = 0.1

# ccusage の実コストから基準単価（$/M input）を逆算するための倍率。
# 実測: この重みで割ると Opus 5.000 / Sonnet 2.000 / Haiku 1.000 に収束する。
RATE_OUTPUT_MULT = 5.0
RATE_CACHE_READ_MULT = 0.1
RATE_CACHE_CREATE_MULT = 1.25

# HTML の表に出す行数。全件出すとレポートが読めなくなるため上位のみ。
TOP_N_ROWS = 15

# S6 の表に参考として載せる manual 圧縮の行数。auto 行を押し出さないよう少数に絞る。
S6_MANUAL_REF_ROWS = 3

# S3: 軽量な用途向けの agentType。ここに高価なモデルが割り当たっていれば見直し候補。
# `.claude/agents/*.md` の frontmatter でモデルを固定できるため、設定ミスが起きうる。
# 名前は前方一致（小文字化後）で判定する。プラグイン名前空間付き
# （例 `myplugin:outputsummarizer`）でも当たるよう、コロン以降も見る。
S3_LIGHTWEIGHT_AGENTS = ("outputsummarizer", "explore")

# S3: 「高価」と判定するモデル名の部分文字列。Opus のみを対象にする。
# Sonnet は軽量用途でも妥当な選択肢なので、警告すると誤検知が支配的になる。
S3_EXPENSIVE_MODELS = ("opus",)

# S3: この深さ以上のネスト委譲を「深すぎる」として報告する。
# 実測で spawnDepth は 1〜5 まで存在し、1〜2 は通常の委譲。3 以上は
# 親から見て 1 回のタスクが何層にも重なり、総消費が読めなくなる。
S3_DEEP_SPAWN_DEPTH = 3

# S2: セッションの cache_create / cache_read 比率が期間中央値のこの倍数を
# 超えたら外れ値。3 倍は「中央的な使い方の 3 倍キャッシュを作り直している」
# = 明確な逸脱の目安。
S2_OUTLIER_MULT = 3.0

# S2: 相対評価に必要な最小サンプル数。これ未満ではシグナル自体を出さない
# （2〜3 件の中央値は「分布」ではなく、外れ値の概念が成立しない）。
S2_MIN_SAMPLES = 5

# S2: 母集団に入れる最小 assistant ターン数。
# 1 ターン目は cache_create しか発生しないのが構造的に当然であり、
# 短いセッションは比率が必ず跳ねる。実測では外れ値 27 件のうち 26 件が
# 3 ターン以下で、金額の 93% を占めていた（= 検出の実質すべてがノイズ）。
# 5 は「キャッシュが効いているかを比率で語れる最小の対話長」として選定
# （3 では上記のノイズが残り、10 では通常の短い作業まで母集団から落ちる）。
S2_MIN_TURNS = 5

# S7: エラー / 中断で捨てたトークン。
# Claude の `is_error` な tool 結果は本文が短い（実測で 1 件平均 280 文字程度）ため、
# 「エラー結果そのもののサイズ」だけを見ると金額がほぼゼロになり示唆にならない。
# 実コストは「エラーを受けてモデルがもう一度やり直すために再送されたコンテキスト」
# である。そこで代理指標として、エラー直後の assistant レコードの入力側 usage
# （cache_read + cache_create + input）を「やり直しに要した再送量」として計上する。
# 紐付けは「エラーからこの件数以内のレコードに現れた最初の assistant」に限る。
# 実測（Claude 400 セッション）でエラーから再送 assistant までのレコード距離は
# 1 が 45%、2 以内で 79%、3 以内で 91% を占め、それより遠いものはエラーと無関係な
# 後続ターンに紐付いている疑いが強い。3 を上限として因果の推定を近距離に留める。
# 上限を超えた場合とセッション末尾でエラーが起きた場合は、
# 再送量が観測できないためエラー本文のサイズからの近似に落とす。
S7_MAX_LOOKAHEAD = 3

# S8: reasoning トークン比率がコホート中央値のこの倍数を超えたら外れ値。
# 実測（2026-05〜07 の Codex 394 セッション）では xhigh コホートの中央値 0.403 に対し
# 最大 0.820 で、分布の幅は約 2 倍に収まる。3 倍にすると検出が 0 件になり、
# 1.3 倍では 37 件（= 分布の上側 1/4）が出てノイズになる。1.8 倍で 6 件に絞られ、
# 「同じ model/effort の標準的な使い方から明確に逸脱している」ものだけが残る。
S8_OUTLIER_MULT = 1.8

# S8: コホート（model, effort）ごとの最小セッション数。これ未満のコホートは
# 中央値が「分布」を表さないため評価しない（S2_MIN_SAMPLES と同じ考え方）。
S8_MIN_COHORT_SESSIONS = 5

# S8: 母集団に入れる最小 output トークン数。出力が少ないセッションは
# reasoning 比率が数トークンの差で跳ねる（実測で 0.038〜0.384 とばらついた）。
S8_MIN_OUTPUT_TOKENS = 10_000

# S8: effort が解決できなかったコホートのラベル。
# ラベル無しのデフォルト設定であって設定ミスではないため、
# このコホートには「effort を下げよ」という推奨をしない。
S8_UNKNOWN_EFFORT = "unknown"

# S4: 「短い作業」と見なす assistant ターン数の上限。
# 実測（2026-07 の Opus トップレベルセッション 168 件）では assistant ターン数の
# 中央値が 5、p75 が 10 だった。5 は「分布の下半分＝相対的に短い作業」の境界であり、
# ここを 3 にすると対話の立ち上がりだけで終わった中断セッションが支配的になり、
# 10 にすると通常の作業長のセッションまで「短い」と呼ぶことになる。
S4_MAX_TURNS = 5

# S4: 「大規模なコンテキスト収集ではない」と見なすトークン総量の上限。
# 大量のトークンを読んで一発で答えを出した作業は Opus の使用が正当化されやすいため
# 母集団から外す。実測の同母集団で総量の中央値は 194,469 で、10 万は分布の下側
# 1/4 に当たる（この閾値で 168 件中 47 件に絞られる）。20 万に緩めると 85 件と
# 半数を「無駄の候補」と呼ぶことになり、確度の低い仮説としては広すぎる。
S4_MAX_TOTAL_TOKENS = 100_000

# S4: 代替モデルの単価が現行モデルの何分の 1 未満なら「もっと安い選択肢があった」と
# 見なすか。1.5 倍は Opus($5.00) → Sonnet($2.00) の 2.5 倍や Haiku($1.00) の 5 倍を
# 拾い、同一系列内の僅差（単価の逆算誤差の範囲）を拾わない水準。
S4_MIN_RATE_GAP = 1.5

# S5: 同一の (tool 名, 引数) がこの回数以上繰り返されたら重複として報告する。
# 実測の同一引数繰り返しは 133 パターンあり、大半は 2〜3 回で「1 回失敗して
# もう一度」という正当な再試行を含む。4 回以上に絞ると `Bash {"command":
# "sleep 60"}` を 16 回のような明らかな無駄が残る。
S5_MIN_REPEATS = 4

# S5: 2 回目以降の呼び出し 1 回あたりの結果サイズの近似トークン数。
# 結果本文を保持しない設計（メモリを一定に保つため）なので実サイズは分からない。
# 実測の tool 結果の平均は 2,666 文字、中央値 396 文字で、平均を採ると巨大出力
# （S1 が別に計上する領域）に引っ張られる。控えめに p75 相当の 1,400 文字
# ≒ 350 トークンを 1 回分として置き、金額は参考値であると明示する。
S5_APPROX_RESULT_TOKENS = 350

# S5: 表に出す引数要約の最大文字数。ログ由来の任意文字列なので長さを必ず抑える。
S5_ARGS_PREVIEW_CHARS = 60

# S5: 同一パスの再読み込みを「正当な再確認」として除外する対象 tool。
# 直前に `Edit`/`Write` が同じパスへ走っていれば、その後の `Read` は
# 変更結果の確認であって重複ではない。
S5_REREAD_TOOLS = ("read",)
S5_MUTATING_TOOLS = ("edit", "write", "notebookedit", "multiedit")

# Claude のログはファイル名が UUID で日付を含まないため mtime で粗く枝刈りする。
# resume で古いセッションが後から追記されるケースを取り落とさないための安全マージン。
MTIME_MARGIN_DAYS = 30

# HTML に例示するスキップパスの最大件数。
MAX_SKIPPED_PATHS = 10

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# `timestamp` も `message` も持たないレコード型。`.get("timestamp")` する前に弾く。
_METADATA_TYPES = frozenset(
    {
        "mode",
        "permission-mode",
        "last-prompt",
        "custom-title",
        "ai-title",
        "agent-name",
        "agent-setting",
        "relocated",
        "worktree-state",
        "file-history-snapshot",
        "queue-operation",
        "pr-link",
        "file-history-delta",
    }
)

# ccusage は `claude-haiku-4-5-20251001` のように日付付きで返すが、
# 生ログ側は `claude-haiku-4-5`。突き合わせるため日付サフィックスを落とす。
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")

# Bedrock 経由の `resolvedModel` は `global.anthropic.claude-opus-4-8` や
# `us.anthropic.claude-haiku-4-5-20251001-v1:0` のようにリージョン接頭辞と
# バージョン接尾辞で修飾される。`normalize_model` の日付サフィックス除去だけでは
# 剥がせないため、専用に落としてから正規化する。
_BEDROCK_PREFIX_RE = re.compile(r"^[a-z0-9-]+\.anthropic\.")
_BEDROCK_VERSION_SUFFIX_RE = re.compile(r"-v\d+:\d+$")

# `.meta.json` の `model` は "opus" / "sonnet" / "haiku" のエイリアス形式でしか
# 入らない。単価表のキー（`claude-opus-5` 等）とは形が違うので、
# `RateTable.base()` に渡す前に代表モデル名へ寄せるのではなく、
# エイリアスは「モデル系列」の判定にのみ使う。

# Codex の現行 function_call_output 形式に含まれる実トークン数。
_ORIGINAL_TOKENS_RE = re.compile(r"Original token count: (\d+)")


# --- 値の正規化 -----------------------------------------------------------
def _as_int(value: Any) -> int:
    """JSON 由来の値を int にする。数値化できない値は 0 として扱う。

    JSON として妥当でも数値フィールドに文字列や list が入っているレコードが
    存在する。ここで例外を投げるとファイル単位の try/except まで伝播し、
    同じファイル内の正常なレコードの集計まで丸ごと失われるため、
    壊れた値はレコード単位で 0 に隔離する。
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# --- データクラス ---------------------------------------------------------
@dataclass
class TokenUsage:
    """1 レスポンス（あるいはその合計）のトークン使用量。"""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_create: int = 0

    def merge_max(self, other: TokenUsage) -> None:
        """フィールドごとに max を取って統合する。

        Claude の 1 レスポンスは 2〜6 レコードに分割記録され、同じ `message.id` を
        共有したまま `output_tokens` だけが増えていく。単純合計すると最大 3.3 倍に
        膨張するため、`message.id` グループ内は max で畳む。
        """
        self.input = max(self.input, other.input)
        self.output = max(self.output, other.output)
        self.cache_read = max(self.cache_read, other.cache_read)
        self.cache_create = max(self.cache_create, other.cache_create)

    def add(self, other: TokenUsage) -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_create += other.cache_create

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_create


@dataclass
class BigToolOutput:
    """S1 の候補となる巨大な tool 結果 1 件。"""

    tool: str
    chars: int
    tokens: int
    turns_after: int
    day: str


@dataclass
class CompactionEvent:
    """S6 が使うコンテキスト圧縮イベント 1 件。"""

    trigger: str
    pre_tokens: int
    post_tokens: int
    dropped: int
    day: str
    estimated: bool = False


@dataclass
class ErrorEvent:
    """S7 が使う失敗・中断イベント 1 件。

    Claude は `tool_result.is_error is True` な結果、Codex は `turn_aborted`
    イベントに対応する。`tokens` は「その失敗を受けてやり直すために再送された
    コンテキスト」の推定量で、`estimated` が True の場合はエラー本文サイズからの
    近似（= 直後の assistant レコードが見つからなかった）であることを示す。
    """

    kind: str
    detail: str
    tokens: int
    day: str
    estimated: bool = False


@dataclass
class DuplicateCall:
    """S5 が使う「同一引数で繰り返された tool 呼び出し」1 パターン。

    `args` は正規化済み JSON 文字列。本文は保持せず、引数と回数だけを残す
    （メモリを一定に保つため）。
    """

    tool: str
    args: str
    count: int
    day: str


@dataclass
class SubagentCall:
    """S3 が使うサブエージェント委譲 1 件。

    親セッションの `Agent` tool 呼び出しと、`subagents/agent-<id>.jsonl` +
    `agent-<id>.meta.json` を突き合わせた結果を持つ。`agent_id` を持たない
    （= 親側の記録から解決できなかった）委譲は集計対象にしない。
    """

    agent_id: str
    agent_type: str
    model: str
    spawn_depth: int
    #: フィールド別の内訳。合計トークンだけでは USD 換算できない
    #: （output は基準単価の 5 倍、cache_create は 1.25 倍で課金されるため）。
    usage: TokenUsage = field(default_factory=TokenUsage)
    parent_agent_id: str = ""
    day: str = ""
    #: 内訳が親の `totalTokens` からの推定（= ログを集計できなかった）か。
    estimated: bool = False

    @property
    def tokens(self) -> int:
        return self.usage.total

    @property
    def is_lightweight(self) -> bool:
        """軽量用途向けの agentType か（プラグイン名前空間を除いて前方一致）。"""
        name = self.agent_type.strip().lower()
        bare = name.rsplit(":", 1)[-1]
        return bare.startswith(S3_LIGHTWEIGHT_AGENTS)

    @property
    def is_expensive_model(self) -> bool:
        """高価なモデルが割り当てられているか。"""
        return any(m in self.model for m in S3_EXPENSIVE_MODELS)


@dataclass
class SessionStat:
    """セッション 1 件の集計。後続段階のシグナルはここに field を足して拡張する。"""

    path: Path
    project: str
    provider: str
    model: str = "unknown"
    started_at: str = ""
    ended_at: str = ""
    #: 期間フィルタを無視したファイル内の最初 / 最後のレコードのタイムスタンプ。
    #: `started_at`/`ended_at` は期間内のレコードだけから作るため、期間境界を
    #: またぐセッションでは「大きなセッションの一部」を全体と誤認しうる。
    #: S4 のように「セッション全体の規模」を条件にする判定はこちらを見る。
    first_record_at: str = ""
    last_record_at: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    assistant_turns: int = 0
    tool_calls: Counter[str] = field(default_factory=Counter)
    big_outputs: list[BigToolOutput] = field(default_factory=list)
    compactions: list[CompactionEvent] = field(default_factory=list)
    subagent_calls: list[SubagentCall] = field(default_factory=list)
    errors: list[ErrorEvent] = field(default_factory=list)
    #: S5: 同一引数で `S5_MIN_REPEATS` 以上繰り返された呼び出しパターン。
    #: `tool_calls` は tool 名だけの件数なので引数の重複は見えない。
    duplicate_calls: list[DuplicateCall] = field(default_factory=list)
    #: Codex の `turn_context.effort`（"low"/"medium"/"high"/"xhigh"）。
    #: null や欠落は `S8_UNKNOWN_EFFORT` のまま残す。Claude では常に unknown。
    effort: str = S8_UNKNOWN_EFFORT
    #: Codex の `reasoning_output_tokens` 累積値。`usage.output` に内包される
    #: 概念なので `TokenUsage` は拡張せず、独立した int として持つ。
    reasoning_output: int = 0

    @property
    def day(self) -> str:
        """セッションの代表日（最初のレコードの日付、YYYY-MM-DD）。"""
        return self.started_at[:10]


@dataclass
class WasteItem:
    """シグナル 1 件の検出結果。`row` は HTML 表示用に整形済みの 1 行。"""

    description: str
    tokens: int
    usd: float
    evidence: str
    session_ref: str
    row: list[str] = field(default_factory=list)
    counted: bool = True


@dataclass
class SignalResult:
    """シグナル単位のまとまり。`report.py` はこれを汎用テーブルとして描画する。"""

    key: str
    title: str
    confidence: str
    count: int
    tokens: int
    usd: float
    description: str = ""
    columns: list[tuple[str, bool]] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    #: 参考用の内訳表か（無駄と断定していないため合計・示唆から除く）。
    reference: bool = False


@dataclass
class LogStats:
    """生ログ解析の全体結果。"""

    signals: list[SignalResult] = field(default_factory=list)
    scanned_files: int = 0
    skipped_files: int = 0
    skipped_paths: list[str] = field(default_factory=list)
    broken_lines: int = 0

    @property
    def total_usd(self) -> float:
        return sum(s.usd for s in self.signals if not s.reference)


@dataclass
class RateTable:
    """モデル名（正規化後）→ 逆算した $/M input 基準単価。"""

    rates: dict[str, float] = field(default_factory=dict)
    fallback: float = 0.0

    def base(self, model: str) -> float:
        """モデルの基準単価を返す。未知モデルは期間全体の加重平均へフォールバック。"""
        return self.rates.get(normalize_model(model), self.fallback)


def normalize_model(model: str) -> str:
    """モデル名を突き合わせ可能な形に正規化する。

    ccusage の日付サフィックス（`-20251001`）と、Bedrock 経由の
    `resolvedModel` が持つリージョン接頭辞（`global.anthropic.`）および
    バージョン接尾辞（`-v1:0`）を落とす。

    Bedrock 修飾は段階 2 の S3 で初めて現れる形式だが、修飾のない名前には
    どのパターンも一致しないため、既存の呼び出し元の結果は変わらない。
    """
    name = model.strip().lower()
    name = _BEDROCK_PREFIX_RE.sub("", name)
    name = _BEDROCK_VERSION_SUFFIX_RE.sub("", name)
    return _DATE_SUFFIX_RE.sub("", name)


# --- 単価の逆算 -----------------------------------------------------------
def _weighted_denominator(
    input_: int, output: int, cache_read: int, cache_create: int
) -> float:
    return (
        input_
        + RATE_OUTPUT_MULT * output
        + RATE_CACHE_READ_MULT * cache_read
        + RATE_CACHE_CREATE_MULT * cache_create
    )


class _ModelAggLike(Protocol):
    """`report.ModelAgg` の構造的部分型（単価逆算に必要なフィールドのみ）。"""

    input: int
    output: int
    cache_read: int
    cache_create: int
    cost: float


class _ReportLike(Protocol):
    """`report.Report` の構造的部分型。

    `report.py` が `logstats` を import するため、逆向きの import は循環になる。
    型検査だけのために Protocol を置き、`Any` による検査の素通りを避ける。
    `Mapping` にしているのは `dict` の値型が非変で `dict[str, ModelAgg]` を
    受け取れないため（`Mapping` の値型は共変）。
    """

    @property
    def by_model(self) -> Mapping[str, _ModelAggLike]: ...


def build_rate_table(rep: _ReportLike) -> RateTable:
    """ccusage の実コストからモデル別の基準単価を逆算する。

    価格表をハードコードせず、`cost / 重み付きトークン数 * 1e6` で求める。
    コストゼロ（または分母ゼロ）のモデルはゼロ除算を避けて登録しない。
    """
    rates: dict[str, float] = {}
    total_cost = 0.0
    total_denom = 0.0
    for model, agg in rep.by_model.items():
        denom = _weighted_denominator(
            agg.input, agg.output, agg.cache_read, agg.cache_create
        )
        cost = float(agg.cost or 0.0)
        total_cost += cost
        total_denom += denom
        if denom <= 0 or cost <= 0:
            continue
        rates[normalize_model(model)] = cost / denom * 1_000_000
    fallback = total_cost / total_denom * 1_000_000 if total_denom > 0 else 0.0
    return RateTable(rates=rates, fallback=fallback)


# --- 期間フィルタ ---------------------------------------------------------
def parse_day_arg(value: str | None) -> date | None:
    """`report.py` と同じ YYYYMMDD 文字列を date に変換する（None は無制限）。"""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def in_period(day: date, since: date | None, until: date | None) -> bool:
    """日付が [since, until] に含まれるか（境界は含む）。"""
    if since is not None and day < since:
        return False
    return not (until is not None and day > until)


def parse_timestamp(value: Any) -> datetime | None:
    """ISO8601（ミリ秒 + Z 終端）をパースする。失敗は None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _epoch_to_datetime(value: Any) -> datetime | None:
    """UNIX 秒（Codex の `completed_at` 等）を UTC の datetime にする。

    fork はレコードの `timestamp` を複製時刻に書き換えてしまうため、
    イベント本来の発生時刻はこちらから復元する。値が数値でない場合や
    範囲外の場合は None を返し、呼び出し側で `timestamp` に落とす。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def mtime_in_range(path: Path, since: date | None, until: date | None) -> bool:
    """mtime による粗い枝刈り。安全マージンを取り、境界付近は必ず中身を見る。"""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date()
    except OSError:
        return True
    margin = timedelta(days=MTIME_MARGIN_DAYS)
    if since is not None and mtime < since - margin:
        return False
    return not (until is not None and mtime > until + margin)


def humanize_project(dir_name: str) -> str:
    """sanitized cwd（例 `-home-ken-programs-ligaius`）を読みやすい名前にする。"""
    return dir_name.lstrip("-").replace("-", "/") or dir_name


def find_claude_files(
    root: Path,
    since: date | None,
    until: date | None,
    *,
    include_subagents: bool = False,
) -> list[Path]:
    """Claude のセッションログを列挙する（mtime で粗く枝刈り）。

    既定では `subagents/` 配下を除く。S3 もこの探索は使わず、親セッションの
    パスから `collect_subagent_calls` が配下を辿る（`agentId` と `.meta.json`
    の対応付けに親のレコードが必要なため）。`include_subagents=True` は
    サブエージェントログを独立に列挙したい場合のためのエスケープハッチ。
    """
    if not root.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(root.rglob("*.jsonl")):
        if not include_subagents and "subagents" in path.parts:
            continue
        if not mtime_in_range(path, since, until):
            continue
        files.append(path)
    return files


def find_codex_files(root: Path, since: date | None, until: date | None) -> list[Path]:
    """Codex のロールアウトを列挙する（パスの YYYY/MM/DD で枝刈り）。"""
    if not root.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(root.glob("*/*/*/rollout-*.jsonl")):
        year, month, day = path.parts[-4:-1]
        try:
            file_day = date(int(year), int(month), int(day))
        except ValueError:
            files.append(path)
            continue
        if in_period(file_day, since, until):
            files.append(path)
    return files


# --- 行単位のパース -------------------------------------------------------
@dataclass
class ScanCounters:
    """走査中の統計（レポートに出す健全性指標）。"""

    scanned_files: int = 0
    skipped_files: int = 0
    broken_lines: int = 0
    skipped_paths: list[str] = field(default_factory=list)

    def note_skip(self, path: Path) -> None:
        self.skipped_files += 1
        if len(self.skipped_paths) < MAX_SKIPPED_PATHS:
            self.skipped_paths.append(str(path))


def iter_records(path: Path, counters: ScanCounters) -> Iterator[dict[str, Any]]:
    """JSONL を 1 行ずつ dict として流す。破損行は数えて無視する。

    実測で 90,517 行中 3 行の破損を確認しているため、`json.loads` は必ず
    try/except で囲む。ファイル自体が読めない場合はスキップとして記録する。
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    counters.broken_lines += 1
                    continue
                if isinstance(record, dict):
                    yield record
                else:
                    counters.broken_lines += 1
    except OSError:
        counters.note_skip(path)


# --- サイズ測定 -----------------------------------------------------------
def measure_content_size(content: Any) -> int:
    """`tool_result.content` の文字数を測る（str / list 両対応）。

    `Agent`/`SendMessage`/`ToolSearch` や画像を返す `Read` では list になる。
    要素の `type` に応じて text は本文長、image は base64 データ長、
    それ以外は JSON 文字列化した長さを足す。
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                total += len(str(block))
                continue
            kind = block.get("type")
            if kind == "text":
                total += len(str(block.get("text") or ""))
            elif kind == "image":
                source = block.get("source")
                data = source.get("data") if isinstance(source, dict) else None
                total += len(data) if isinstance(data, str) else _json_len(block)
            else:
                total += _json_len(block)
        return total
    if content is None:
        return 0
    return _json_len(content)


def _json_len(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(value))


def measure_codex_output(output: Any) -> tuple[int, int | None]:
    """Codex の `function_call_output.output` から (文字数, 実トークン数) を返す。

    4 種の文字列形式 + list 変種があり、`len()` も `json.loads()` も成功を
    仮定できない。現行形式が持つ `Original token count: N` が取れればそれを
    トークン数として優先し、無ければ文字数からの近似に委ねる（None を返す）。

    ただし `Original token count` は**切り詰め前**のサイズである。実際に課金
    されたのは切り詰め後の本文なので、文字数からの近似を上限として抑える
    （抑えないと truncate された巨大出力を過大計上する）。
    """
    if isinstance(output, str):
        tokens: int | None = None
        if match := _ORIGINAL_TOKENS_RE.search(output):
            try:
                tokens = min(int(match.group(1)), len(output) // CHARS_PER_TOKEN)
            except ValueError:
                tokens = None
        if tokens is None:
            # 3 番目の形式: JSON 文字列 {"output": "...", "metadata": {...}}
            try:
                parsed = json.loads(output)
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("output"), str):
                return len(parsed["output"]), None
        return len(output), tokens
    if isinstance(output, list):
        return measure_content_size(output), None
    if output is None:
        return 0, None
    return _json_len(output), None


# --- Claude セッションの解析 ---------------------------------------------
def _usage_from(usage: Any) -> TokenUsage:
    """`message.usage` を TokenUsage にする。欠落キーは 0 として読む。

    `stop_reason: null` の中間レコードでは一部キーが落ちるため、
    `_as_int` で None や壊れた値も 0 に丸める。
    """
    if not isinstance(usage, dict):
        return TokenUsage()
    return TokenUsage(
        input=_as_int(usage.get("input_tokens")),
        output=_as_int(usage.get("output_tokens")),
        cache_read=_as_int(usage.get("cache_read_input_tokens")),
        cache_create=_as_int(usage.get("cache_creation_input_tokens")),
    )


def _tool_uses_from_assistant(content: Any) -> dict[str, tuple[str, Any]]:
    """assistant の content から tool_use id → (tool 名, 引数) を取り出す。"""
    if not isinstance(content, list):
        return {}
    uses: dict[str, tuple[str, Any]] = {}
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_id = block.get("id")
            if isinstance(tool_id, str):
                uses[tool_id] = (
                    str(block.get("name") or "unknown"),
                    block.get("input"),
                )
    return uses


def _tool_names_from_assistant(content: Any) -> dict[str, str]:
    """assistant の content から tool_use id → tool 名の対応を取り出す。"""
    return {k: name for k, (name, _args) in _tool_uses_from_assistant(content).items()}


def _normalize_args(args: Any) -> str:
    """tool の引数を比較可能な正規化 JSON 文字列にする。

    キー順を固定しないと同じ呼び出しが別パターンとして数えられる。
    JSON にできない値（実測では現れないが型の保証がない）は `str()` に落とす。
    """
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(args)


def _tool_arg_path(args: Any) -> str:
    """引数から対象ファイルパスを取り出す（S5 の再読み込み除外に使う）。"""
    if isinstance(args, dict):
        value = args.get("file_path") or args.get("notebook_path")
        if isinstance(value, str):
            return value
    return ""


def _codex_call_args(payload: Mapping[str, Any]) -> Any:
    """Codex の tool 呼び出しの引数を比較可能な値にする。

    `function_call` は `arguments` が JSON 文字列、`custom_tool_call`
    （`apply_patch`）は `input` が生の文字列。JSON なら dict に開いてから
    `_normalize_args` に渡すことで、キー順の違いだけの呼び出しを同一と扱う。
    """
    raw = payload.get("arguments")
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            return raw
    if raw is not None:
        return raw
    return payload.get("input")


class _DuplicateTracker:
    """S5: 同一 (tool 名, 引数) の呼び出し回数を時系列順に数える。

    `Edit`/`Write` が同じパスに介在した後の `Read` は変更結果の正当な再確認
    なので数え直す（それまでの回数を捨てる）。tool 呼び出しの順序を素直に
    追い、直前の変更の有無だけを見る単純な規則に留める。
    """

    def __init__(self) -> None:
        self._counts: Counter[tuple[str, str]] = Counter()
        self._days: dict[tuple[str, str], str] = {}
        #: パス → そのパスに変更が入った後に再読み込みをリセットすべきか。
        self._mutated: set[str] = set()

    def add(self, tool: str, args: Any, day: str) -> None:
        # 引数が空のレコードは「同一引数」の根拠にならない。実測（2026-07）で
        # `input` が記録されない `tool_use` が 6,633 件中 34 件あり、これを
        # 数えると中身の違う 10 回の `Bash` が 1 パターンの重複として誤検出された
        # （引数を持たない `EnterPlanMode` 等も同じ形になる）。
        if args in (None, {}, "", []):
            return
        bare = tool.strip().lower()
        path = _tool_arg_path(args)
        if path and bare.startswith(S5_MUTATING_TOOLS):
            self._mutated.add(path)
        key = (tool, _normalize_args(args))
        if path and path in self._mutated and bare.startswith(S5_REREAD_TOOLS):
            # 変更後の 1 回目の再読み込みは正当な再確認。ここまでの回数を捨て、
            # 「変更後に何度も読み直している」場合だけを次から数える。
            self._mutated.discard(path)
            self._counts[key] = 0
        self._counts[key] += 1
        self._days.setdefault(key, day)

    def results(self) -> list[DuplicateCall]:
        return [
            DuplicateCall(
                tool=tool, args=args, count=count, day=self._days[(tool, args)]
            )
            for (tool, args), count in self._counts.items()
            if count >= S5_MIN_REPEATS
        ]


def _iter_tool_results(content: Any) -> Iterator[dict[str, Any]]:
    """user の content から tool_result ブロックを取り出す。"""
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            yield block


@dataclass
class _AgentResult:
    """親セッション側に記録された `Agent` tool の結果（S3 用の補助情報）。"""

    resolved_model: str = ""
    total_tokens: int = 0
    is_async: bool = False


def _agent_result_from(tool_use_result: Any) -> _AgentResult | None:
    """`toolUseResult` が `Agent` の結果なら補助情報を取り出す。

    段階 1 は `toolUseResult` を一切使わない方針だが（`Edit`/`Write` で本文が
    19〜34 倍に膨らみ、モデルが見た量と乖離するため）、`Agent` の結果に限っては
    本文の重複ではなく専用の集計フィールドなので例外的に読む。

    `toolUseResult` は稀に dict ではなく文字列になるため、必ず型を確認する。
    """
    if not isinstance(tool_use_result, dict):
        return None
    # `agentId` を持つのが Agent の結果の目印。同期・非同期の双方に存在する。
    if not isinstance(tool_use_result.get("agentId"), str):
        return None
    model = tool_use_result.get("resolvedModel")
    return _AgentResult(
        resolved_model=normalize_model(model) if isinstance(model, str) else "",
        total_tokens=_as_int(tool_use_result.get("totalTokens")),
        is_async=tool_use_result.get("isAsync") is True,
    )


def parse_claude_session(
    path: Path,
    counters: ScanCounters,
    seen_uuids: set[str],
    since: date | None = None,
    until: date | None = None,
) -> SessionStat | None:
    """Claude のセッションログ 1 ファイルを解析して SessionStat を返す。

    2 段階 dedup を行う:

    1. 全ファイル横断の `uuid` 集合で既出レコードをスキップする
       （fork/resume が履歴を別ファイルへ丸ごと複製するため）
    2. `message.id` でグループ化し、フィールドごとに max を取る

    `<synthetic>` モデルのレコードはクライアント側エラーで usage が全ゼロなので
    集計対象から除く。期間外のレコードも同様に無視する。
    """
    stat = SessionStat(
        path=path,
        project=humanize_project(path.parent.name),
        provider="claude",
    )
    by_message_id: dict[str, TokenUsage] = {}
    tool_names: dict[str, str] = {}
    # (tool 名, 文字数, 検出時点の assistant ターン数) を貯め、
    # 走査後に「以降のターン数」へ変換する。
    pending_big: list[tuple[str, int, int, str]] = []
    model_counts: Counter[str] = Counter()
    # agentId → 親側に記録された `Agent` の結果。走査後に subagents/ と突き合わせる。
    agent_results: dict[str, _AgentResult] = {}
    # S7: 直後の assistant の入力量で「やり直しの再送量」を解決するまで待つエラー。
    # (tool 名, エラー本文の文字数, 日付, 検出時点のレコード番号)
    pending_errors: list[tuple[str, int, str, int]] = []
    # 集計対象として処理したレコードの通し番号。S7 の紐付け距離の判定に使う。
    record_index = 0
    found = False
    # S5: 同一引数の呼び出し回数。dedup 済みの新規 tool_use のみを数える。
    duplicates = _DuplicateTracker()

    def error_from_size(tool: str, chars: int, day: str, reason: str) -> ErrorEvent:
        """再送量が観測できなかったエラーを本文サイズから近似する。"""
        return ErrorEvent(
            kind=f"{tool} エラー",
            detail=f"エラー本文 {chars:,} 文字（{reason}）",
            tokens=chars // CHARS_PER_TOKEN,
            day=day,
            estimated=True,
        )

    for record in iter_records(path, counters):
        rtype = record.get("type")
        if rtype in _METADATA_TYPES:
            continue

        uuid = record.get("uuid")
        duplicate = isinstance(uuid, str) and uuid in seen_uuids
        if isinstance(uuid, str):
            seen_uuids.add(uuid)
        if duplicate:
            # 複製レコードは課金対象として数えないが、tool_use id → tool 名の
            # 対応だけは拾っておく。これを飛ばすと、複製された assistant の
            # 直後に来る新規 tool_result の tool 名が unknown になる。
            if rtype == "assistant":
                message = record.get("message")
                if isinstance(message, dict):
                    tool_names.update(
                        _tool_names_from_assistant(message.get("content"))
                    )
            continue

        ts = record.get("timestamp")
        # 期間フィルタを掛ける前に、このセッションが本来持つ時間範囲を記録する。
        # `started_at`/`ended_at` は期間内のレコードだけから作るため、期間境界を
        # またぐセッションでは「大きなセッションの一部」を全体と誤認する。
        if isinstance(ts, str) and ts:
            if not stat.first_record_at:
                stat.first_record_at = ts
            stat.last_record_at = max(stat.last_record_at, ts)
        stamp = parse_timestamp(ts)
        if stamp is not None and not in_period(stamp.date(), since, until):
            continue
        if isinstance(ts, str) and ts:
            if not stat.started_at:
                stat.started_at = ts
            stat.ended_at = max(stat.ended_at, ts)
            found = True
        record_index += 1
        # S7: 紐付けの猶予（`S7_MAX_LOOKAHEAD` レコード）を過ぎたエラーは、
        # ここより後の assistant に紐付けると因果と無関係な再送量を拾うため、
        # 本文サイズからの近似で確定させて待ち行列から外す。
        if pending_errors:
            expired = [
                e for e in pending_errors if record_index - e[3] > S7_MAX_LOOKAHEAD
            ]
            if expired:
                pending_errors = [e for e in pending_errors if e not in expired]
                for tool, chars, day, _ in expired:
                    stat.errors.append(
                        error_from_size(tool, chars, day, "再送を特定せず")
                    )

        if rtype == "assistant":
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            model = str(message.get("model") or "unknown")
            if model == "<synthetic>":
                continue
            model_counts[normalize_model(model)] += 1
            # tool_use ブロックは分割記録の最後のレコードにだけ現れることがある。
            # usage の dedup で早期 continue する前に必ず名前を回収する。
            called = _tool_uses_from_assistant(message.get("content"))
            new_calls = {k: v for k, v in called.items() if k not in tool_names}
            tool_names.update({k: name for k, (name, _args) in called.items()})
            for name, args in new_calls.values():
                stat.tool_calls[name] += 1
                # S5: 引数まで含めた呼び出しの記録。tool 名だけのカウント
                # （`tool_calls`）とは別に、時系列順で重複を追う。
                duplicates.add(
                    name, args, str(ts)[:10] if isinstance(ts, str) else stat.day
                )

            message_id = message.get("id")
            usage = _usage_from(message.get("usage"))
            # S7: 直前のエラーを受けた最初の assistant レコードの入力量を
            # 「やり直しに要した再送量」として引き当てる。`<synthetic>` は
            # ここへ到達する前に continue しているので課金対象にならない。
            if pending_errors:
                total_resent = usage.input + usage.cache_read + usage.cache_create
                # 並列 tool 呼び出しが同時に失敗した場合、再送は 1 回で済んでいる。
                # 件数で割らないと 1 回の再送を件数分だけ二重計上する。
                share = total_resent // len(pending_errors)
                for tool, chars, day, _ in pending_errors:
                    stat.errors.append(
                        ErrorEvent(
                            kind=f"{tool} エラー",
                            detail=f"エラー本文 {chars:,} 文字",
                            tokens=share or chars // CHARS_PER_TOKEN,
                            day=day,
                            estimated=share <= 0,
                        )
                    )
                pending_errors.clear()
            if isinstance(message_id, str) and message_id:
                if message_id in by_message_id:
                    by_message_id[message_id].merge_max(usage)
                    continue
                by_message_id[message_id] = usage
            else:
                stat.usage.add(usage)
            stat.assistant_turns += 1

        elif rtype == "user":
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            # `toolUseResult` は使わない（Edit で 19 倍、Write で 34 倍に膨らみ、
            # モデルが実際に見た量と乖離する）。稀に文字列そのものになる点も含め、
            # ここでは参照しないことで型の揺れを踏まない。
            # `Agent` の結果だけは例外的に `toolUseResult` を読む（S3 用）。
            # `resolvedModel` はここにしか無く、`.meta.json` の `model` は
            # "opus" 等のエイリアスなので単価の解決に使えない。
            if (agent := _agent_result_from(record.get("toolUseResult"))) is not None:
                agent_id = str(record["toolUseResult"]["agentId"])
                agent_results[agent_id] = agent

            for block in _iter_tool_results(message.get("content")):
                size = measure_content_size(block.get("content"))
                tool_use_id = block.get("tool_use_id")
                tool = tool_names.get(
                    tool_use_id if isinstance(tool_use_id, str) else "", "unknown"
                )
                day = str(ts)[:10] if isinstance(ts, str) else stat.day
                # `is_error` はキー自体が約 59% のレコードで欠落するため
                # `is True` で判定する（`== False` と混同してはならない）。
                if block.get("is_error") is True:
                    pending_errors.append((tool, size, day, record_index))
                if size < S1_MIN_CHARS:
                    continue
                pending_big.append((tool, size, stat.assistant_turns, day))

        elif rtype == "system" and record.get("subtype") == "compact_boundary":
            meta = record.get("compactMetadata")
            if not isinstance(meta, dict):
                continue
            pre = _as_int(meta.get("preTokens"))
            post = _as_int(meta.get("postTokens"))
            # `cumulativeDroppedTokens` は累積値であり、イベント列で合算すると
            # 二次関数的に過大計上する。必ず preTokens - postTokens を使う。
            stat.compactions.append(
                CompactionEvent(
                    trigger=str(meta.get("trigger") or "unknown"),
                    pre_tokens=pre,
                    post_tokens=post,
                    dropped=max(pre - post, 0),
                    day=str(ts)[:10] if isinstance(ts, str) else stat.day,
                )
            )

    # セッション末尾でエラーが起きた（= 続く assistant が無い）場合は
    # 再送量が観測できないため、エラー本文サイズからの近似に落とす。
    for tool, chars, day, _ in pending_errors:
        stat.errors.append(error_from_size(tool, chars, day, "再送なし"))

    for usage in by_message_id.values():
        stat.usage.add(usage)
    if model_counts:
        stat.model = model_counts.most_common(1)[0][0]

    for tool, size, turns_at, day in pending_big:
        turns_after = max(stat.assistant_turns - turns_at, 1)
        stat.big_outputs.append(
            BigToolOutput(
                tool=tool,
                chars=size,
                tokens=size // CHARS_PER_TOKEN,
                turns_after=min(turns_after, S1_MAX_TURNS),
                day=day,
            )
        )

    stat.duplicate_calls = duplicates.results()

    stat.subagent_calls, subagent_duplicates = collect_subagent_calls(
        path, agent_results, counters, seen_uuids, since, until
    )
    # サブエージェント側の重複も同じ作業単位の無駄として親に合流させる。
    # 実装を委譲する運用では tool 呼び出しの大半がサブエージェント側で発生し、
    # 親のログだけを見ると S5 が原理的に検出できない。
    stat.duplicate_calls.extend(subagent_duplicates)
    for dup in stat.duplicate_calls:
        if not dup.day:
            dup.day = stat.day

    for call in stat.subagent_calls:
        if not call.day:
            call.day = stat.day

    if (
        not found
        and not stat.big_outputs
        and not stat.compactions
        and not stat.subagent_calls
        and not stat.errors
        and not stat.duplicate_calls
    ):
        return None
    return stat


# --- サブエージェントログの解析（S3） ------------------------------------
def subagents_dir(session_path: Path) -> Path:
    """親セッションのログパスから `subagents/` ディレクトリを求める。

    `<project>/<session-uuid>.jsonl` に対して
    `<project>/<session-uuid>/subagents/` が対応する。
    """
    return session_path.parent / session_path.stem / "subagents"


def find_subagent_files(session_path: Path) -> list[Path]:
    """親セッションに属するサブエージェントログを列挙する。

    ネストした委譲（`spawnDepth` 2 以上）も全て同じディレクトリに平置きされる。
    深度は `.meta.json` の `spawnDepth` をそのまま使う。`parentAgentId` は
    将来の木構造復元用に `SubagentCall` へ保持するだけで、現時点では
    親子関係の解決処理は行っていない。

    ここで mtime による枝刈りはしない。親セッションが期間内と判定された時点で
    配下のサブエージェントも同じ作業単位に属するため、レコードの `timestamp`
    による判定に委ねる。
    """
    directory = subagents_dir(session_path)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("agent-*.jsonl"))


def parse_subagent_meta(path: Path) -> dict[str, Any]:
    """`agent-<id>.meta.json` を読む。読めなければ空 dict を返す。

    `.meta.json` が無い / 壊れている場合でもサブエージェントログ自体の
    トークン集計は有効なので、ハードエラーにはしない。
    """
    meta_path = path.with_suffix("").with_suffix(".meta.json")
    try:
        raw = meta_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    try:
        meta = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return meta if isinstance(meta, dict) else {}


@dataclass
class _SubagentLogStat:
    """サブエージェントログ 1 件の集計結果。"""

    usage: TokenUsage = field(default_factory=TokenUsage)
    day: str = ""
    #: ログ内で最も頻出した `message.model`（正規化済み）。
    #: 親の `resolvedModel` が取れない深いネスト委譲でのモデル解決に使う。
    model: str = ""
    #: S5: このサブエージェントログ内で検出された重複呼び出しパターン。
    #: 呼び出し元の親 `SessionStat.duplicate_calls` に合流させる。
    duplicate_calls: list[DuplicateCall] = field(default_factory=list)


def parse_subagent_log(
    path: Path,
    counters: ScanCounters,
    seen_uuids: set[str],
    since: date | None = None,
    until: date | None = None,
) -> _SubagentLogStat:
    """サブエージェントログ 1 件を集計する。

    サブエージェントの `agent-*.jsonl` は assistant / user / attachment のみで
    `system` も `turn_duration` も持たないが、assistant の `message.usage` は
    通常のセッションと同じ形式なので、2 段階 dedup をそのまま適用できる。

    `seen_uuids` は親セッションと共有する。resume で履歴が複製された場合に
    同じレコードを二重計上しないためである。

    `message.model` も併せて数え、最頻値を返す。assistant レコードは完全形の
    モデル名（`claude-opus-4-8` 等）を必ず持つため、親側に `resolvedModel` が
    無いネストした委譲（`spawnDepth` 2 以上は親の `toolUseResult` が親の
    サブエージェントログ側に記録されるため親セッションから解決できない）でも、
    ここからモデルを解決できる。

    S5 の重複 tool 呼び出しも親セッションと同じロジックで数える。実データでは
    重複呼び出しの実例（`Bash {"command": "sleep 60"}` の繰り返し等）が
    ほぼ全てサブエージェント側にあり（実装を委譲する運用では tool 呼び出しの
    大半が構造的にこちらで発生する）、トップレベルのセッションだけを見ていると
    原理的に検出できない。結果は呼び出し元の親 `SessionStat.duplicate_calls`
    に合流させる（`seen_uuids` を親と共有しているので二重計上にはならない）。
    """
    by_message_id: dict[str, TokenUsage] = {}
    extra = TokenUsage()
    model_counts: Counter[str] = Counter()
    day = ""
    duplicates = _DuplicateTracker()
    # tool_use id → tool 名。分割記録された同一 tool_use を二重に数えないための集合。
    seen_tool_ids: set[str] = set()

    for record in iter_records(path, counters):
        if record.get("type") != "assistant":
            continue
        uuid = record.get("uuid")
        if isinstance(uuid, str):
            if uuid in seen_uuids:
                continue
            seen_uuids.add(uuid)

        ts = record.get("timestamp")
        stamp = parse_timestamp(ts)
        if stamp is not None and not in_period(stamp.date(), since, until):
            continue
        if not day and isinstance(ts, str):
            day = ts[:10]

        message = record.get("message")
        if not isinstance(message, dict):
            continue
        raw_model = message.get("model")
        if raw_model == "<synthetic>":
            continue
        if isinstance(raw_model, str) and raw_model:
            model_counts[normalize_model(raw_model)] += 1

        # S5: 親セッションと同じ扱い。tool_use ブロックは分割記録の最後の
        # レコードにだけ現れることがあるため、usage の dedup で早期に
        # continue する前に回収する。
        called = _tool_uses_from_assistant(message.get("content"))
        for tool_id, (name, args) in called.items():
            if tool_id in seen_tool_ids:
                continue
            seen_tool_ids.add(tool_id)
            duplicates.add(name, args, str(ts)[:10] if isinstance(ts, str) else day)

        usage = _usage_from(message.get("usage"))
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            if message_id in by_message_id:
                by_message_id[message_id].merge_max(usage)
            else:
                by_message_id[message_id] = usage
        else:
            extra.add(usage)

    total = TokenUsage()
    total.add(extra)
    for usage in by_message_id.values():
        total.add(usage)
    dup_calls = duplicates.results()
    for dup in dup_calls:
        if not dup.day:
            dup.day = day
    return _SubagentLogStat(
        usage=total,
        day=day,
        model=model_counts.most_common(1)[0][0] if model_counts else "",
        duplicate_calls=dup_calls,
    )


def collect_subagent_calls(
    session_path: Path,
    agent_results: Mapping[str, _AgentResult],
    counters: ScanCounters,
    seen_uuids: set[str],
    since: date | None = None,
    until: date | None = None,
) -> tuple[list[SubagentCall], list[DuplicateCall]]:
    """親セッション配下のサブエージェント委譲を SubagentCall のリストにする。

    トークン数は**サブエージェントログの自前集計を第一候補にする**。
    計画では同期呼び出しは親の `toolUseResult.totalTokens` を使う想定だったが、
    実データで照合した結果 `totalTokens` は最終ターン 1 回分の `usage` の
    単純和でしかなく（例: 7 + 8,667 + 0 + 275 = 8,949 と完全一致）、
    実際の消費に対して 2〜80 倍の過小評価になる。委譲 1 件の総消費として
    使えないため、S1/S6 と同じ 2 段階 dedup による自前集計を採る。

    ログ内の集計が 0 トークンだった場合（期間外・resume で uuid 既出・空
    ファイル）に限り `totalTokens` へフォールバックし、その行を「推定」として
    印を付ける。内訳が分からないため cache_read 相当として置く（サブエージェント
    の消費は実測で 94.8% が cache_read であり、これが最も実態に近い）。

    モデル解決の優先順位:

    1. 親の `resolvedModel`（Bedrock 修飾を正規化済み）
    2. サブエージェントログ内で最頻の `message.model`。`spawnDepth` 2 以上は
       親側の `toolUseResult` が親セッションに無いため 1 が取れないが、
       ログの assistant レコードは完全形のモデル名を必ず持つ
    3. `.meta.json` の `model`（"opus" 等のエイリアス）
    4. `unknown`

    S5 の重複 tool 呼び出しは各サブエージェントログで検出したものをまとめて
    第 2 の戻り値として返す。呼び出し元が親の `SessionStat.duplicate_calls` に
    合流させる（サブエージェント側の無駄も親セッションの作業単位の一部として
    数えるため）。
    """
    calls: list[SubagentCall] = []
    duplicates: list[DuplicateCall] = []
    for log_path in find_subagent_files(session_path):
        agent_id = log_path.stem.removeprefix("agent-")
        meta = parse_subagent_meta(log_path)
        result = agent_results.get(agent_id)

        log = parse_subagent_log(log_path, counters, seen_uuids, since, until)
        duplicates.extend(log.duplicate_calls)
        usage = log.usage
        estimated = False
        if usage.total <= 0:
            fallback = result.total_tokens if result is not None else 0
            if fallback <= 0:
                continue
            usage = TokenUsage(cache_read=fallback)
            estimated = True

        meta_model = meta.get("model")
        model = ""
        if result is not None and result.resolved_model:
            model = result.resolved_model
        elif log.model:
            model = log.model
        elif isinstance(meta_model, str):
            model = normalize_model(meta_model)

        calls.append(
            SubagentCall(
                agent_id=agent_id,
                agent_type=str(meta.get("agentType") or "unknown"),
                model=model or "unknown",
                spawn_depth=_as_int(meta.get("spawnDepth")),
                usage=usage,
                parent_agent_id=str(meta.get("parentAgentId") or ""),
                day=log.day,
                estimated=estimated,
            )
        )
    return calls, duplicates


# --- Codex ロールアウトの解析 --------------------------------------------
def parse_codex_session(
    path: Path,
    counters: ScanCounters,
    seen_turn_ids: set[str] | None = None,
    since: date | None = None,
    until: date | None = None,
) -> SessionStat | None:
    """Codex のロールアウト 1 ファイルを解析して SessionStat を返す。

    `token_count.info` は null になりうる（セッション最初の ping）。
    `context_compacted` はメトリクスを持たないため、直前に見た
    `last_token_usage.input_tokens` を廃棄量の推定値として使う。

    S7 用に `turn_aborted` を、S8 用に `turn_context.effort` と
    `total_token_usage.reasoning_output_tokens` を拾う。中断ターンの消費量は
    `total_token_usage`（累積値）の、同じ `turn_id` の `task_started` 時点の
    スナップショットとの差分で求める。実データで `task_started` がターン境界で
    あることを確認済み。基準となるスナップショットが取れなかったターン
    （`task_started` がファイルに無い等）は、累積値との差分がセッション全体の
    消費量まで膨らむため、直近の `last_token_usage` による控えめな近似に落とす。

    期間フィルタ（`since`/`until`）は「そのレコードを集計に含めるか」だけに使い、
    累積値のスナップショット等の内部状態は期間の内外を問わず更新する。
    期間外のレコードで状態機械を止めると、期間をまたいだ差分計算が壊れる。

    `seen_turn_ids` は全 Codex ファイル横断で共有する `turn_id` の集合。
    Codex の fork（`session_meta.payload.forked_from_id`）は親の履歴を丸ごと
    複製するため、共有しないと同じ `turn_aborted` を複数ファイルから重複計上する
    （実測で 6 月の S7 検出トークンの 35% が重複だった）。Claude の `uuid` とは
    形式が異なるので、集合は必ず別に持つ。

    S5 の重複 tool 呼び出しは `call_id` によるファイル内 dedup で数える。実測
    （2026 年の 1,228 ファイル / 69,109 件）で `call_id` は `function_call` /
    `custom_tool_call` に 100% 存在し、複数ファイルに同じ `call_id` が現れる
    ケースは 0 件だった。Codex の fork は会話履歴を複製せず（`turn_aborted` の
    ような累積状態と違い）呼び出し履歴が別ファイルへ丸ごと複製されないため、
    Claude のような横断 dedup は不要である。
    """
    if seen_turn_ids is None:
        seen_turn_ids = set()
    stat = SessionStat(path=path, project="codex", provider="codex")
    last_input = 0
    found = False
    # turn_id → その `task_started` 時点の累積入力量。`turn_aborted` の時点との
    # 差分が「中断されたターンで実際に消費した入力トークン」になる。
    turn_start_input: dict[str, int] = {}
    total_input = 0
    call_names: dict[str, str] = {}
    # (tool 名, 文字数, トークン数, 検出時点のターン数, 日付)
    pending_big: list[tuple[str, int, int, int, str]] = []
    # S5: 同一引数の呼び出し回数。`call_id` でファイル内の重複記録を弾く。
    duplicates = _DuplicateTracker()
    seen_call_ids: set[str] = set()

    for record in iter_records(path, counters):
        ts = record.get("timestamp")
        stamp = parse_timestamp(ts)
        in_range = stamp is None or in_period(stamp.date(), since, until)
        if in_range and isinstance(ts, str) and ts:
            if not stat.started_at:
                stat.started_at = ts
            stat.ended_at = max(stat.ended_at, ts)
            found = True

        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        rtype = record.get("type")

        if rtype == "session_meta":
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd:
                stat.project = cwd
        elif rtype == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model:
                stat.model = normalize_model(model)
            # `effort` は null になりうる。最後に観測した値を採る
            # （セッション途中で変更された場合、後半の設定が実態に近い）。
            effort = payload.get("effort")
            if isinstance(effort, str) and effort:
                stat.effort = effort.strip().lower()
        elif rtype == "event_msg":
            ptype = payload.get("type")
            if ptype == "token_count":
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                # Codex の input_tokens は cached_input_tokens を含む。
                # Claude の会計モデル（両者が排他）と混ぜてはならないため、
                # ここでは S6 の代理指標としてのみ使う。
                last = info.get("last_token_usage")
                if isinstance(last, dict):
                    last_input = _as_int(last.get("input_tokens"))
                total = info.get("total_token_usage")
                if isinstance(total, dict):
                    # 累積値の追跡は期間の内外を問わず行う（差分計算の基準になる）。
                    total_input = _as_int(total.get("input_tokens"))
                    if in_range:
                        stat.usage = TokenUsage(
                            input=total_input,
                            output=_as_int(total.get("output_tokens")),
                        )
                        # 累積値なので合算せず、最後に観測した値で上書きする。
                        stat.reasoning_output = _as_int(
                            total.get("reasoning_output_tokens")
                        )
            elif ptype == "task_started":
                # 期間外でも必ず記録する。ここを飛ばすと期間をまたいだ
                # `turn_aborted` の差分がセッション累積そのものに膨らむ。
                turn_start_input[str(payload.get("turn_id") or "")] = total_input
            elif ptype == "turn_aborted":
                turn_id = str(payload.get("turn_id") or "")
                # fork は複製した履歴のレコード側 `timestamp` を複製時刻に
                # 書き換える（実測で本来 06-12 の中断が 06-15 として現れる）が、
                # `completed_at` は元の中断時刻を保つ。期間判定とイベントの
                # 日付にはこちらを優先し、fork の有無で日付が動かないようにする。
                aborted_at = _epoch_to_datetime(payload.get("completed_at"))
                event_day = str(ts)[:10] if isinstance(ts, str) else stat.day
                event_in_range = in_range
                if aborted_at is not None:
                    event_day = aborted_at.date().isoformat()
                    event_in_range = in_period(aborted_at.date(), since, until)
                # fork で複製された同じ中断イベントは 1 回だけ数える。
                # 期間外のレコードでは登録しない。登録すると「期間外の複製が
                # 期間内の 1 件目を打ち消す」形になり、期間の指定を変えるだけで
                # 確定済みの過去の検出量が動いてしまう。
                duplicate = bool(turn_id) and turn_id in seen_turn_ids
                if event_in_range and not duplicate:
                    if turn_id:
                        seen_turn_ids.add(turn_id)
                    reason = payload.get("reason")
                    base = turn_start_input.get(turn_id)
                    # 基準が不明なターンは累積差分を信用できない。
                    # 直近 1 リクエストの入力量に留めて過大計上を避ける。
                    if base is None:
                        spent = last_input
                        detail = f"{reason or 'unknown'}（ターン基準不明）"
                    else:
                        spent = max(total_input - base, 0)
                        detail = str(reason or "unknown")
                    stat.errors.append(
                        ErrorEvent(
                            kind="turn_aborted",
                            detail=detail,
                            tokens=spent,
                            day=event_day,
                            estimated=True,
                        )
                    )
            elif ptype == "context_compacted" and in_range:
                stat.compactions.append(
                    CompactionEvent(
                        trigger="auto",
                        pre_tokens=last_input,
                        post_tokens=0,
                        dropped=last_input,
                        day=str(ts)[:10] if isinstance(ts, str) else stat.day,
                        estimated=True,
                    )
                )
        elif rtype == "response_item" and in_range:
            ptype = payload.get("type")
            if ptype in ("function_call", "custom_tool_call"):
                name = str(payload.get("name") or "unknown")
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    call_names[call_id] = name
                if ptype == "function_call":
                    stat.tool_calls[name] += 1
                    stat.assistant_turns += 1
                # S5: 同じ `call_id` の再記録は同一呼び出しなので 1 回だけ数える。
                # `apply_patch` は `custom_tool_call` として現れるため、ファイル
                # 変更の直後の再読み込みを除外できるようここでも記録する（ただし
                # Codex の引数は `file_path` を持たず、変更対象は `exec_command`
                # のコマンド文字列や patch 本文の中にあるため、Claude 側の
                # パス単位の再読み込み除外は実質的に働かない）。
                if isinstance(call_id, str) and call_id:
                    if call_id in seen_call_ids:
                        continue
                    seen_call_ids.add(call_id)
                duplicates.add(
                    name,
                    _codex_call_args(payload),
                    str(ts)[:10] if isinstance(ts, str) else stat.day,
                )
            elif ptype == "function_call_output":
                chars, real_tokens = measure_codex_output(payload.get("output"))
                if chars < S1_MIN_CHARS:
                    continue
                call_id = payload.get("call_id")
                tool = call_names.get(
                    call_id if isinstance(call_id, str) else "", "unknown"
                )
                pending_big.append(
                    (
                        tool,
                        chars,
                        real_tokens
                        if real_tokens is not None
                        else chars // CHARS_PER_TOKEN,
                        stat.assistant_turns,
                        str(ts)[:10] if isinstance(ts, str) else stat.day,
                    )
                )

    if not found:
        return None
    stat.duplicate_calls = duplicates.results()
    for dup in stat.duplicate_calls:
        if not dup.day:
            dup.day = stat.day
    for tool, chars, tokens, turns_at, day in pending_big:
        stat.big_outputs.append(
            BigToolOutput(
                tool=tool,
                chars=chars,
                tokens=tokens,
                turns_after=min(max(stat.assistant_turns - turns_at, 1), S1_MAX_TURNS),
                day=day,
            )
        )
    return stat


# --- シグナル解析 ---------------------------------------------------------
def _fmt_usd(value: float) -> str:
    return f"${value:,.2f}"


def analyze_s1(sessions: list[SessionStat], rates: RateTable) -> SignalResult:
    """S1: 巨大な tool 出力を検出する（以降のターン数による増幅込み）。

    巨大出力はセッション序盤に入ると以降の全ターンで cache_read として
    再課金される。よって無駄コストは `サイズ × 以降のターン数`（上限
    `S1_MAX_TURNS`）で見積もり、cache_read 相当の単価で USD 化する。
    """
    items: list[WasteItem] = []
    for stat in sessions:
        base = rates.base(stat.model)
        for big in stat.big_outputs:
            tokens = big.tokens * max(big.turns_after, 1)
            usd = tokens * base * WASTE_CACHE_READ_MULT / 1_000_000
            items.append(
                WasteItem(
                    description=f"{big.tool} が {big.chars:,} 文字を返却",
                    tokens=tokens,
                    usd=usd,
                    evidence=(
                        f"{big.chars:,} 文字 ≒ {big.tokens:,} トークンが"
                        f"以降 {big.turns_after} ターン再送された推定"
                    ),
                    session_ref=str(stat.path),
                    row=[
                        big.day,
                        stat.project,
                        big.tool,
                        f"{big.chars:,}",
                        f"{big.turns_after}",
                        f"{tokens:,}",
                        _fmt_usd(usd),
                    ],
                )
            )
    items.sort(key=lambda i: -i.usd)
    return SignalResult(
        key="S1",
        title="巨大な tool 出力（再送による増幅込み）",
        confidence="high",
        count=len(items),
        tokens=sum(i.tokens for i in items),
        usd=sum(i.usd for i in items),
        description=(
            f"1 回で {S1_MIN_CHARS:,} 文字を超えた tool 結果。"
            f"以降のターンで cache_read として再課金されるため、"
            f"最大 {S1_MAX_TURNS} ターン分まで増幅して見積もる。"
        ),
        columns=[
            ("日付", False),
            ("プロジェクト", False),
            ("Tool", False),
            ("文字数", True),
            ("再送ターン", True),
            ("推定トークン", True),
            ("推定 USD", True),
        ],
        rows=[i.row for i in items[:TOP_N_ROWS]],
    )


def analyze_s6(sessions: list[SessionStat], rates: RateTable) -> SignalResult:
    """S6: コンテキスト圧縮で廃棄したトークンを集計する。

    `manual` トリガーはユーザーの意思による圧縮なので無駄ではない。
    行としては参考表示するが、合計トークン / 合計 USD には含めない。
    """
    items: list[WasteItem] = []
    for stat in sessions:
        base = rates.base(stat.model)
        for event in stat.compactions:
            usd = event.dropped * base * WASTE_CACHE_READ_MULT / 1_000_000
            counted = event.trigger != "manual"
            note = "推定" if event.estimated else ""
            items.append(
                WasteItem(
                    description=(
                        f"{event.trigger} 圧縮で {event.dropped:,} トークン廃棄"
                    ),
                    tokens=event.dropped,
                    usd=usd,
                    evidence=f"preTokens - postTokens{f'（{note}）' if note else ''}",
                    session_ref=str(stat.path),
                    counted=counted,
                    row=[
                        event.day,
                        stat.project,
                        event.trigger + (f"（{note}）" if note else ""),
                        f"{event.pre_tokens:,}",
                        f"{event.post_tokens:,}",
                        f"{event.dropped:,}",
                        _fmt_usd(usd),
                    ],
                )
            )
    items.sort(key=lambda i: -i.usd)
    counted_items = [i for i in items if i.counted]
    manual_items = [i for i in items if not i.counted]
    # auto を先に埋め、残り枠に manual を少数だけ足す。金額だけでソートすると
    # manual の廃棄量が大きい期間に auto 行が 1 件も表に出なくなる。
    rows = [i.row for i in counted_items[:TOP_N_ROWS]]
    free_slots = max(TOP_N_ROWS - len(rows), 0)
    rows += [i.row for i in manual_items[: min(S6_MANUAL_REF_ROWS, free_slots)]]
    return SignalResult(
        key="S6",
        title="コンテキスト圧縮で廃棄したトークン",
        confidence="high",
        count=len(counted_items),
        tokens=sum(i.tokens for i in counted_items),
        usd=sum(i.usd for i in counted_items),
        description=(
            "自動圧縮（auto）で捨てられたコンテキスト。"
            "`preTokens - postTokens` のみを使い、累積値の "
            "`cumulativeDroppedTokens` は使わない。"
            "manual は利用者の意思による圧縮なので、"
            f"参考として最大 {S6_MANUAL_REF_ROWS} 件だけ表に出し、合計には含めない。"
            "Codex は圧縮イベントがメトリクスを持たないため直前の入力量からの推定値。"
        ),
        columns=[
            ("日付", False),
            ("プロジェクト", False),
            ("トリガー", False),
            ("Pre", True),
            ("Post", True),
            ("廃棄", True),
            ("推定 USD", True),
        ],
        rows=rows,
    )


def _subagent_usd(usage: TokenUsage, model: str, rates: RateTable) -> float:
    """サブエージェントの消費を内訳ごとの単価で USD 換算する。

    合計トークンに一律 `WASTE_CACHE_READ_MULT` を掛けると約 2 倍の過小評価に
    なる。実測内訳は input 0.2% / output 0.9% / cache_read 94.8% /
    cache_create 4.0% だが、output は基準単価の 5 倍、cache_create は 1.25 倍で
    課金されるため、比率が小さくても金額寄与は無視できない。S1/S6 の単価逆算と
    同じ `_weighted_denominator` を通して正確に求める。
    """
    denom = _weighted_denominator(
        usage.input, usage.output, usage.cache_read, usage.cache_create
    )
    return denom * rates.base(model) / 1_000_000


def analyze_s3(sessions: list[SessionStat], rates: RateTable) -> SignalResult:
    """S3: サブエージェント委譲のオーバーヘッドを検出する。

    委譲そのものは無駄ではない（文脈の隔離という明確な価値がある）。
    無駄として数えるのは次の 2 つに絞る:

    1. 軽量用途の `agentType` に高価なモデル（Opus）が割り当たっている
    2. `spawnDepth` が深すぎる（`S3_DEEP_SPAWN_DEPTH` 以上）

    `agentType` 別の全体集計は列の意味が違うため、この表には混ぜず
    `analyze_s3_reference` が別表として出す。
    """
    items: list[WasteItem] = []

    for stat in sessions:
        for call in stat.subagent_calls:
            usd = _subagent_usd(call.usage, call.model, rates)

            reasons: list[str] = []
            if call.is_lightweight and call.is_expensive_model:
                reasons.append(f"軽量用途に {call.model}")
            if call.spawn_depth >= S3_DEEP_SPAWN_DEPTH:
                reasons.append(f"ネスト深度 {call.spawn_depth}")
            if not reasons:
                continue
            note = "推定" if call.estimated else ""
            items.append(
                WasteItem(
                    description=f"{call.agent_type}: {' / '.join(reasons)}",
                    tokens=call.tokens,
                    usd=usd,
                    evidence=" / ".join(reasons) + (f"（{note}）" if note else ""),
                    session_ref=str(stat.path),
                    row=[
                        call.day,
                        stat.project,
                        call.agent_type + (f"（{note}）" if note else ""),
                        call.model,
                        f"{call.spawn_depth}",
                        f"{call.tokens:,}",
                        _fmt_usd(usd),
                    ],
                )
            )

    items.sort(key=lambda i: -i.usd)
    rows = [i.row for i in items[:TOP_N_ROWS]]

    return SignalResult(
        key="S3",
        title="サブエージェント委譲のオーバーヘッド",
        confidence="medium",
        count=len(items),
        tokens=sum(i.tokens for i in items),
        usd=sum(i.usd for i in items),
        description=(
            "委譲そのものは無駄ではないため、"
            f"「軽量用途（{'/'.join(S3_LIGHTWEIGHT_AGENTS)}）に Opus を割り当てた」"
            f"「ネスト深度が {S3_DEEP_SPAWN_DEPTH} 以上」の 2 点だけを合計に数える。"
            "トークン数は `subagents/` 配下のログを 2 段階 dedup して自前集計した値。"
            "親の `toolUseResult.totalTokens` は最終ターン 1 回分しか含まず"
            "実消費の 2〜80 分の 1 になるため使わない（ログから 1 件も"
            "集計できなかった場合のみフォールバックし「推定」と表示する）。"
            "agentType 別の全体集計は S3R の別表に出す。"
        ),
        columns=[
            ("日付", False),
            ("プロジェクト", False),
            ("agentType", False),
            ("モデル", False),
            ("深度", True),
            ("トークン", True),
            ("推定 USD", True),
        ],
        rows=rows,
    )


def analyze_s3_reference(sessions: list[SessionStat], rates: RateTable) -> SignalResult:
    """S3R: agentType 別のサブエージェント消費を参考表として集計する。

    S3 本体の表とは列の意味が異なるため（1 行が 1 委譲ではなく agentType 全体）、
    同じ表に混ぜると列ヘッダと値が食い違う。独立した `SignalResult` として返し、
    `reference=True` で「無駄と断定していない参考値」であることを明示する。
    """
    # agentType → [件数, 合計トークン, 合計 USD]
    by_type: dict[str, list[float]] = {}
    for stat in sessions:
        for call in stat.subagent_calls:
            agg = by_type.setdefault(call.agent_type, [0.0, 0.0, 0.0])
            agg[0] += 1
            agg[1] += call.tokens
            agg[2] += _subagent_usd(call.usage, call.model, rates)

    ranked = sorted(by_type.items(), key=lambda kv: -kv[1][1])
    rows = [
        [
            agent_type,
            f"{int(count):,}",
            f"{int(tokens):,}",
            f"{int(tokens / count) if count else 0:,}",
            _fmt_usd(usd),
        ]
        for agent_type, (count, tokens, usd) in ranked[:TOP_N_ROWS]
    ]
    return SignalResult(
        key="S3R",
        title="サブエージェント委譲の agentType 別内訳（参考）",
        confidence="low",
        count=len(by_type),
        tokens=sum(int(v[1]) for v in by_type.values()),
        usd=sum(v[2] for v in by_type.values()),
        description=(
            "S3 で無駄と判定しなかった委譲も含む、agentType 別の全体集計。"
            "件数と 1 件あたりの平均トークンが一覧できること自体が"
            "委譲設計の見直しの示唆になるため参考として出す。"
            "無駄と断定した値ではないため、無駄の合計金額には含めない。"
        ),
        columns=[
            ("agentType", False),
            ("件数", True),
            ("合計トークン", True),
            ("平均トークン/件", True),
            ("推定 USD", True),
        ],
        rows=rows,
        reference=True,
    )


def analyze_s2(sessions: list[SessionStat], rates: RateTable) -> SignalResult:
    """S2: セッション単位のキャッシュ非効率（外れ値）を検出する。

    既存の示唆（`report.py` の `build_insights`）はモデル単位でしか
    Cache Create > Cache Read を見ておらず、どのセッションが原因か分からない。
    ここではセッションごとに `cache_create / cache_read` を出し、期間内の
    **分布に対する相対評価**で外れ値を挙げる。絶対閾値を使わないのは、
    比率の妥当な水準がモデルと作業内容で大きく変わるためである。

    母集団は「比率を語る意味があるセッション」に絞る。`cache_create > 0` に
    加えて `cache_read > 0`（ゼロだと分母のゼロ除算回避で比率が桁違いに跳ね、
    実測で 79,014.0 のような値が表に出た）と `assistant_turns >=
    S2_MIN_TURNS`（1〜3 ターンでは cache_create 主体になるのが構造的に当然）を
    要求する。

    超過分の USD は「中央値相当の比率であれば発生しなかったはずの
    cache_create」に `RATE_CACHE_CREATE_MULT` を掛けて求める。
    """
    empty = SignalResult(
        key="S2",
        title="セッション単位のキャッシュ非効率",
        confidence="medium",
        count=0,
        tokens=0,
        usd=0.0,
    )

    candidates = [
        s
        for s in sessions
        if s.usage.cache_create > 0
        and s.usage.cache_read > 0
        and s.assistant_turns >= S2_MIN_TURNS
    ]
    if len(candidates) < S2_MIN_SAMPLES:
        # サンプルが少なすぎると中央値が「分布」を表さず、外れ値の概念が
        # 成立しない。シグナル自体を出さない。
        return empty

    def ratio(stat: SessionStat) -> float:
        # 母集団で cache_read > 0 を保証済みなのでゼロ除算は起きない。
        return stat.usage.cache_create / stat.usage.cache_read

    median = statistics.median(ratio(s) for s in candidates)
    if median <= 0:
        return empty
    threshold = median * S2_OUTLIER_MULT

    items: list[WasteItem] = []
    for stat in candidates:
        value = ratio(stat)
        if value <= threshold:
            continue
        # 中央値どおりなら作られていたはずの量を引き、超過分だけを数える。
        expected = median * stat.usage.cache_read
        excess = max(int(stat.usage.cache_create - expected), 0)
        if excess <= 0:
            continue
        usd = excess * rates.base(stat.model) * RATE_CACHE_CREATE_MULT / 1_000_000
        items.append(
            WasteItem(
                description=(
                    f"{stat.project}: cache_create/cache_read = {value:,.1f}"
                    f"（期間中央値 {median:,.1f}）"
                ),
                tokens=excess,
                usd=usd,
                evidence=f"中央値の {value / median:,.1f} 倍",
                session_ref=str(stat.path),
                row=[
                    stat.day,
                    stat.project,
                    stat.model,
                    f"{stat.usage.cache_create:,}",
                    f"{stat.usage.cache_read:,}",
                    f"{value:,.1f}",
                    f"{excess:,}",
                    _fmt_usd(usd),
                ],
            )
        )

    items.sort(key=lambda i: -i.usd)
    return SignalResult(
        key="S2",
        title="セッション単位のキャッシュ非効率",
        confidence="medium",
        count=len(items),
        tokens=sum(i.tokens for i in items),
        usd=sum(i.usd for i in items),
        description=(
            f"セッションごとの cache_create / cache_read が、対象期間の中央値"
            f"（{median:,.1f}）の {S2_OUTLIER_MULT:g} 倍を超えたもの。"
            "妥当な比率はモデルと作業内容で変わるため絶対閾値を使わず、"
            f"期間内の分布に対する相対評価で判定する（サンプルが "
            f"{S2_MIN_SAMPLES} 件未満の期間は評価しない）。"
            f"母集団は cache_read が 0 でなく、assistant のターン数が "
            f"{S2_MIN_TURNS} 以上のセッションに限る。1〜数ターンで終わった"
            "セッションは cache_create 主体になるのが構造的に当然で、"
            "「同じ文脈を作り直している」無駄の兆候ではないため。"
            "トークン数と USD は「中央値どおりの比率なら作られなかったはずの"
            "cache_create 超過分」で、キャッシュ生成の単価（基準単価の "
            f"{RATE_CACHE_CREATE_MULT:g} 倍）で換算している。"
            "同じ文脈を何度も作り直している（= セッションを細切れにしている）"
            "セッションが上位に来る。"
        ),
        columns=[
            ("日付", False),
            ("プロジェクト", False),
            ("モデル", False),
            ("Cache Create", True),
            ("Cache Read", True),
            ("比率", True),
            ("超過分", True),
            ("推定 USD", True),
        ],
        rows=[i.row for i in items[:TOP_N_ROWS]],
    )


def analyze_s7(sessions: list[SessionStat], rates: RateTable) -> SignalResult:
    """S7: 失敗・中断で捨てたトークンを集計する。

    数えるのは「エラーや中断そのもの」ではなく「それを受けてやり直すために
    再送されたコンテキスト」である。エラー結果の本文は実測で 1 件平均 280 文字
    程度しかなく、本文サイズだけを見ると金額がほぼゼロになり示唆にならない。

    - Claude: `tool_result.is_error is True` から `S7_MAX_LOOKAHEAD` レコード
      以内に現れた最初の assistant レコードの入力側 usage
      （input + cache_read + cache_create）を再送量とする。それより遠い
      assistant はエラーと無関係な後続ターンの疑いが強いので紐付けず、
      エラー本文サイズからの近似に落とす。`<synthetic>`
      （`isApiErrorMessage: true`）は usage が全ゼロで課金されていないため、
      `parse_claude_session` の時点で除外済みであり、この経路にも入らない。
    - Codex: `turn_aborted` と、同じ `turn_id` の `task_started` の時点の
      `total_token_usage.input_tokens`（累積値）の差分を、中断されたターンで
      消費した入力量とする。基準が取れないターンは直近 1 リクエスト分の
      入力量に留める（累積値そのものを計上して桁を誤らないため）。
      fork で複製された同じ `turn_id` の中断は 1 回だけ数える。

    USD は S1/S6 と同じく cache_read 相当（基準単価の
    `WASTE_CACHE_READ_MULT` 倍）で換算する。再送されたコンテキストの大半が
    キャッシュ経由の読み出しだからである。
    """
    items: list[WasteItem] = []
    for stat in sessions:
        base = rates.base(stat.model)
        for event in stat.errors:
            usd = event.tokens * base * WASTE_CACHE_READ_MULT / 1_000_000
            note = "推定" if event.estimated else ""
            items.append(
                WasteItem(
                    description=f"{event.kind}: {event.detail}",
                    tokens=event.tokens,
                    usd=usd,
                    evidence=event.detail + (f"（{note}）" if note else ""),
                    session_ref=str(stat.path),
                    row=[
                        event.day,
                        stat.project,
                        event.kind + (f"（{note}）" if note else ""),
                        event.detail,
                        f"{event.tokens:,}",
                        _fmt_usd(usd),
                    ],
                )
            )

    items.sort(key=lambda i: -i.usd)
    return SignalResult(
        key="S7",
        title="失敗・中断で捨てたトークン",
        confidence="medium",
        count=len(items),
        tokens=sum(i.tokens for i in items),
        usd=sum(i.usd for i in items),
        description=(
            "tool 呼び出しの失敗（`is_error`）とターンの中断（`turn_aborted`）。"
            "計上しているのはエラー本文そのものではなく、"
            "「その失敗を受けてやり直すために再送されたコンテキスト」である"
            "（エラー本文は実測で 1 件平均 280 文字程度しかなく、"
            "本文サイズだけでは実コストを表さない）。"
            "Claude はエラー直後の assistant レコードの入力側 usage、"
            "Codex は中断ターンの累積入力量の差分を代理指標に使う。"
            "クライアント側エラーの `<synthetic>` レコードは usage が全ゼロ"
            "（課金されていない）ため、そもそも集計対象に入れていない。"
        ),
        columns=[
            ("日付", False),
            ("プロジェクト", False),
            ("種別", False),
            ("内容", False),
            ("推定トークン", True),
            ("推定 USD", True),
        ],
        rows=[i.row for i in items[:TOP_N_ROWS]],
    )


def analyze_s8(sessions: list[SessionStat], rates: RateTable) -> SignalResult:
    """S8: reasoning トークン比率が同じ設定の標準から逸脱したセッションを検出する。

    比率の妥当な水準は `(model, effort)` で大きく変わる（実測で xhigh の中央値
    0.403 に対し high は 0.246 と 1.6 倍の差がある）ため、単一の絶対閾値では
    機能しない。コホートごとの中央値に対する相対評価にする。

    母集団は「比率を語る意味があるセッション」に絞る:

    - `usage.output >= S8_MIN_OUTPUT_TOKENS`（出力が少ないと数トークンの差で
      比率が跳ねる。実測で 0.038〜0.384 とばらついた）
    - コホートのセッション数が `S8_MIN_COHORT_SESSIONS` 以上
      （それ未満の中央値は「分布」を表さず、外れ値の概念が成立しない）

    超過分 `reasoning - 中央値比率 × output` は output と同じ課金
    （基準単価の `RATE_OUTPUT_MULT` 倍）として換算する。Codex の
    `output_tokens` は `reasoning_output_tokens` を含むためである。

    `effort` が `unknown` のコホートには「effort を下げよ」と推奨しない。
    ラベルの無いデフォルト設定であって設定ミスではないからである。
    """
    empty = SignalResult(
        key="S8",
        title="reasoning トークン比率の外れ値（Codex）",
        confidence="medium",
        count=0,
        tokens=0,
        usd=0.0,
    )

    candidates = [
        s
        for s in sessions
        if s.provider == "codex"
        and s.usage.output >= S8_MIN_OUTPUT_TOKENS
        and s.reasoning_output > 0
    ]
    if not candidates:
        return empty

    cohorts: dict[tuple[str, str], list[SessionStat]] = {}
    for stat in candidates:
        cohorts.setdefault((stat.model, stat.effort), []).append(stat)

    def ratio(stat: SessionStat) -> float:
        # 母集団で output >= S8_MIN_OUTPUT_TOKENS を保証済み。
        return stat.reasoning_output / stat.usage.output

    items: list[WasteItem] = []
    evaluated = 0
    for (model, effort), members in cohorts.items():
        # 「無駄と数えるか」と「表に出すか」を同じ基準にするため、
        # サンプル不足のコホートはここで丸ごと落とす。
        if len(members) < S8_MIN_COHORT_SESSIONS:
            continue
        evaluated += len(members)
        median = statistics.median(ratio(s) for s in members)
        if median <= 0:
            continue
        threshold = median * S8_OUTLIER_MULT
        for stat in members:
            value = ratio(stat)
            if value <= threshold:
                continue
            excess = max(int(stat.reasoning_output - median * stat.usage.output), 0)
            if excess <= 0:
                continue
            usd = excess * rates.base(stat.model) * RATE_OUTPUT_MULT / 1_000_000
            advice = (
                "effort の見直し候補"
                if effort != S8_UNKNOWN_EFFORT
                else "effort 未設定のため推奨なし"
            )
            items.append(
                WasteItem(
                    description=(
                        f"{model}/{effort}: reasoning 比率 {value:.0%}"
                        f"（コホート中央値 {median:.0%}）"
                    ),
                    tokens=excess,
                    usd=usd,
                    evidence=f"中央値の {value / median:,.1f} 倍 / {advice}",
                    session_ref=str(stat.path),
                    row=[
                        stat.day,
                        stat.project,
                        model,
                        effort,
                        f"{value:.0%}",
                        f"{median:.0%}",
                        f"{excess:,}",
                        _fmt_usd(usd),
                        advice,
                    ],
                )
            )

    if evaluated == 0:
        return empty

    items.sort(key=lambda i: -i.usd)
    return SignalResult(
        key="S8",
        title="reasoning トークン比率の外れ値（Codex）",
        confidence="medium",
        count=len(items),
        tokens=sum(i.tokens for i in items),
        usd=sum(i.usd for i in items),
        description=(
            "Codex セッションの `reasoning_output_tokens / output_tokens` が、"
            f"同じ (モデル, effort) のコホート中央値の {S8_OUTLIER_MULT:g} 倍を"
            "超えたもの。妥当な比率は effort 設定で大きく変わる"
            "（実測で xhigh の中央値 40% に対し high は 25%）ため"
            "絶対閾値は使わない。母集団は output が "
            f"{S8_MIN_OUTPUT_TOKENS:,} トークン以上のセッションに限り、"
            f"セッション数が {S8_MIN_COHORT_SESSIONS} 件未満のコホートは"
            "中央値が分布を表さないため評価しない。"
            "超過分は output と同じ単価（基準単価の "
            f"{RATE_OUTPUT_MULT:g} 倍）で換算している。"
            f"effort が {S8_UNKNOWN_EFFORT} のコホートはラベル無しの"
            "デフォルト設定であって設定ミスではないため、"
            "「effort を下げよ」という推奨はしない。"
        ),
        columns=[
            ("日付", False),
            ("プロジェクト", False),
            ("モデル", False),
            ("effort", False),
            ("reasoning 比率", True),
            ("コホート中央値", True),
            ("超過分", True),
            ("推定 USD", True),
            ("示唆", False),
        ],
        rows=[i.row for i in items[:TOP_N_ROWS]],
    )


def _cheapest_rate(rates: RateTable) -> float:
    """期間内に実際に使われたモデルのうち最も安い基準単価を返す。

    「もっと安い選択肢があった」の代替単価に使う。価格表をハードコードせず
    実績から採るのは `build_rate_table` と同じ方針である。単価が 1 つも
    逆算できなかった場合はフォールバックに落とす。
    """
    return min(rates.rates.values(), default=rates.fallback)


def _starts_in_period(
    stat: SessionStat, since: date | None, until: date | None
) -> bool:
    """セッションの開始時刻が指定期間内に収まっているか。

    `first_record_at`（期間フィルタ前の最初のレコード）を使う。これが期間外の
    セッションは「期間境界で切り詰められた一部だけが見えている」状態であり、
    セッション全体の規模を条件にする判定（S4）の母集団に入れてはならない。
    タイムスタンプが解決できない場合は判定材料が無いので通す（既存の他条件で
    絞られる）。
    """
    stamp = parse_timestamp(stat.first_record_at or stat.started_at)
    if stamp is None:
        return True
    return in_period(stamp.date(), since, until)


def analyze_s4(
    sessions: list[SessionStat],
    rates: RateTable,
    since: date | None = None,
    until: date | None = None,
) -> SignalResult:
    """S4: 短く小規模な作業に高価なモデルを使ったセッションを挙げる（参考値）。

    これは**仮説**である。文脈収集・設計レビュー・一発で答えを出す難問は
    意図的に高価なモデルを使う。設定で固定されたモデルによる自動化ワークフロー
    （セキュリティレビューの自動実行等）も同じ形で現れる。よって
    `reference=True` とし、合計・示唆から除外した参考値として提示する。
    金額も「特定された無駄」ではなく「理論上の削減余地の上限値」である。

    母集団はトップレベルのセッションに限る。サブエージェントとして実行された
    ログ（`subagents/` 配下）は `find_claude_files(include_subagents=False)` の
    既定で列挙されないため `SessionStat` になっておらず、ここにも入らない
    （親から見た軽量 agentType への高価モデル割り当ては S3 が別に検出している）。

    判定は 4 条件の論理積に留める:

    1. セッションの開始が指定期間内（`since`/`until`）に収まっている
    2. `assistant_turns <= S4_MAX_TURNS`（短い作業）
    3. `usage.total <= S4_MAX_TOTAL_TOKENS`（大規模な文脈収集ではない）
    4. 単価が期間内の最安モデルの `S4_MIN_RATE_GAP` 倍を超える（高価）
       あるいはモデル名に "opus" を含む

    1 は期間境界をまたぐセッションを外すための条件である。`assistant_turns` と
    `usage.total` は期間外レコードを捨てた後の値なので、開始が期間外の
    セッションでは「大きなセッションの期間内に見えている一部」を「短く小規模な
    作業」と誤認する（実測で 1 ターン / 39,703 トークンに見えるセッションが
    全期間では 6 ターン / 217,843 トークンだった）。
    """
    cheapest = _cheapest_rate(rates)
    items: list[WasteItem] = []
    for stat in sessions:
        if stat.provider != "claude":
            continue
        if not _starts_in_period(stat, since, until):
            continue
        if stat.assistant_turns <= 0 or stat.assistant_turns > S4_MAX_TURNS:
            continue
        if stat.usage.total <= 0 or stat.usage.total > S4_MAX_TOTAL_TOKENS:
            continue
        base = rates.base(stat.model)
        expensive = any(m in stat.model for m in S3_EXPENSIVE_MODELS) or (
            cheapest > 0 and base > cheapest * S4_MIN_RATE_GAP
        )
        if not expensive or base <= cheapest:
            continue
        # 削減余地の上限 = 単価差 × 実効トークン量。実効トークン量は課金の
        # 重み付き換算値を使い、内訳の違い（output は 5 倍、cache_read は
        # 0.1 倍）を無視しないようにする。
        effective = _weighted_denominator(
            stat.usage.input,
            stat.usage.output,
            stat.usage.cache_read,
            stat.usage.cache_create,
        )
        usd = effective * (base - cheapest) / 1_000_000
        if usd <= 0:
            continue
        items.append(
            WasteItem(
                description=(
                    f"{stat.assistant_turns} ターン / "
                    f"{stat.usage.total:,} トークンの作業に {stat.model}"
                ),
                tokens=stat.usage.total,
                usd=usd,
                evidence=f"最安モデルとの単価差 ${base - cheapest:,.2f}/M",
                session_ref=str(stat.path),
                row=[
                    stat.day,
                    stat.project,
                    stat.model,
                    f"{stat.assistant_turns}",
                    f"{stat.usage.total:,}",
                    _fmt_usd(usd),
                ],
            )
        )

    items.sort(key=lambda i: -i.usd)
    return SignalResult(
        key="S4",
        title="モデル選択の妥当性（参考）",
        confidence="low",
        count=len(items),
        tokens=sum(i.tokens for i in items),
        usd=sum(i.usd for i in items),
        description=(
            f"assistant のターン数が {S4_MAX_TURNS} 以下、トークン総量が "
            f"{S4_MAX_TOTAL_TOKENS:,} 以下の短く小規模な作業に、"
            "期間内の最安モデルより明確に高価なモデルを使ったセッション。"
            "これは**仮説**であり特定された無駄ではない。"
            "文脈収集・設計レビュー・一発で答えを出す難問は意図的に高価な"
            "モデルを使うため、大量のトークンを扱ったセッションは母集団から"
            "除外している。金額は「同じ作業を最安モデルで行えたと仮定した場合の"
            "理論上の削減余地の上限値」であり、無駄の合計金額には含めない。"
            "対象はトップレベルのセッションのみで、サブエージェントへの委譲は"
            "含まない（S3 が別に評価する）。ただしトップレベルであっても、"
            "設定で固定されたモデルによる自動化ワークフロー"
            "（セキュリティレビューの自動実行等）は同じ形で現れるため、"
            "「手動でモデルを選び間違えた」ことの証拠ではない。"
            "期間境界で切り詰められたセッションを「短く小規模な作業」と"
            "誤認しないため、開始日時が指定期間内に収まるセッションに限る。"
        ),
        columns=[
            ("日付", False),
            ("プロジェクト", False),
            ("モデル", False),
            ("ターン数", True),
            ("トークン総量", True),
            ("削減余地上限 USD", True),
        ],
        rows=[i.row for i in items[:TOP_N_ROWS]],
        reference=True,
    )


def analyze_s5(sessions: list[SessionStat], rates: RateTable) -> SignalResult:
    """S5: 同一引数で繰り返された tool 呼び出しを挙げる（参考値）。

    誤検知は構造的に残る（ポーリング、意図的な再試行、同じファイルの再確認）。
    よって `reference=True` とし件数を主役にする。金額は結果本文を保持しない
    設計のため 1 回あたり `S5_APPROX_RESULT_TOKENS` の固定近似で、厳密な計測
    ではない。

    介在する `Edit`/`Write` の後の再読み込みは正当な再確認として
    `_DuplicateTracker` が除外している。
    """
    items: list[WasteItem] = []
    for stat in sessions:
        base = rates.base(stat.model)
        for dup in stat.duplicate_calls:
            # 1 回目は必要な呼び出し。2 回目以降だけを無駄の候補として数える。
            repeats = dup.count - 1
            tokens = repeats * S5_APPROX_RESULT_TOKENS
            usd = tokens * base * WASTE_CACHE_READ_MULT / 1_000_000
            preview = dup.args
            if len(preview) > S5_ARGS_PREVIEW_CHARS:
                preview = preview[:S5_ARGS_PREVIEW_CHARS] + "…"
            items.append(
                WasteItem(
                    description=f"{dup.tool} を同一引数で {dup.count} 回呼び出し",
                    tokens=tokens,
                    usd=usd,
                    evidence=f"2 回目以降 {repeats} 回分",
                    session_ref=str(stat.path),
                    row=[
                        stat.project,
                        dup.tool,
                        preview,
                        f"{dup.count}",
                        _fmt_usd(usd),
                    ],
                )
            )

    items.sort(key=lambda i: (-i.tokens, -i.usd))
    return SignalResult(
        key="S5",
        title="重複 tool 呼び出し（参考）",
        confidence="low",
        count=len(items),
        tokens=sum(i.tokens for i in items),
        usd=sum(i.usd for i in items),
        description=(
            f"同一セッション内で (tool 名, 引数) が完全一致する呼び出しが "
            f"{S5_MIN_REPEATS} 回以上繰り返されたパターン。"
            "同じパスに `Edit`/`Write` が介在した後の再読み込みは変更結果の"
            "正当な再確認なので除外している。それでもポーリングや意図的な"
            "再試行との区別は付かないため、金額ではなく**件数**として見る値である。"
            "金額は結果本文を保持しない設計のため 2 回目以降 1 回を "
            f"{S5_APPROX_RESULT_TOKENS:,} トークンと固定近似した参考値であり、"
            "厳密な計測ではない。無駄の合計金額には含めない。"
            "対象は Claude と Codex の双方で、Claude はサブエージェント"
            "（`subagents/` 配下）で発生した重複も呼び出し元の親セッションに"
            "合流させて数える（実装を委譲する運用では tool 呼び出しの大半が"
            "サブエージェント側で発生するため、親のログだけでは検出できない）。"
            "Codex は `call_id` でファイル内の重複記録を弾いて数える。"
        ),
        columns=[
            ("プロジェクト", False),
            ("Tool", False),
            ("引数", False),
            ("回数", True),
            ("参考 USD", True),
        ],
        rows=[i.row for i in items[:TOP_N_ROWS]],
        reference=True,
    )


# --- 公開 API -------------------------------------------------------------
def collect_sessions(
    *,
    since: date | None,
    until: date | None,
    provider: str,
    counters: ScanCounters,
    claude_root: Path = CLAUDE_PROJECTS_DIR,
    codex_root: Path = CODEX_SESSIONS_DIR,
) -> list[SessionStat]:
    """対象プロバイダのログを走査して SessionStat のリストを返す。

    ファイル単位で例外を隔離し、壊れたファイルはスキップとして記録する
    （レポート生成を止めないため）。
    """
    sessions: list[SessionStat] = []
    # Claude の `uuid` と Codex の `turn_id` は形式が異なるため集合を分ける
    # （混ぜると偶然の衝突で正常なレコードを落としうる）。
    seen_uuids: set[str] = set()
    seen_turn_ids: set[str] = set()

    if provider in ("all", "claude"):
        for path in find_claude_files(claude_root, since, until):
            counters.scanned_files += 1
            try:
                stat = parse_claude_session(path, counters, seen_uuids, since, until)
            except Exception:
                counters.note_skip(path)
                continue
            if stat is not None:
                sessions.append(stat)

    if provider in ("all", "codex"):
        for path in find_codex_files(codex_root, since, until):
            counters.scanned_files += 1
            try:
                stat = parse_codex_session(path, counters, seen_turn_ids, since, until)
            except Exception:
                counters.note_skip(path)
                continue
            if stat is not None:
                sessions.append(stat)

    return sessions


def collect_log_stats(
    *,
    rates: RateTable,
    since: str | None,
    until: str | None,
    provider: str,
    claude_root: Path = CLAUDE_PROJECTS_DIR,
    codex_root: Path = CODEX_SESSIONS_DIR,
) -> LogStats:
    """生ログを走査して無駄トークンのシグナルを集計する（公開 API）。

    `since`/`until` は `report.py` と同じ YYYYMMDD 文字列（None なら無制限）。
    """
    since_day = parse_day_arg(since)
    until_day = parse_day_arg(until)
    counters = ScanCounters()
    sessions = collect_sessions(
        since=since_day,
        until=until_day,
        provider=provider,
        counters=counters,
        claude_root=claude_root,
        codex_root=codex_root,
    )
    signals = [
        signal
        for signal in (
            analyze_s1(sessions, rates),
            analyze_s6(sessions, rates),
            analyze_s3(sessions, rates),
            analyze_s3_reference(sessions, rates),
            analyze_s2(sessions, rates),
            analyze_s7(sessions, rates),
            analyze_s8(sessions, rates),
            analyze_s4(sessions, rates, since_day, until_day),
            analyze_s5(sessions, rates),
        )
        # count が 0 でも参考表示の行（S6 の manual のみの期間など）は残す。
        if signal.count > 0 or signal.rows
    ]
    return LogStats(
        signals=signals,
        scanned_files=counters.scanned_files,
        skipped_files=counters.skipped_files,
        skipped_paths=list(counters.skipped_paths),
        broken_lines=counters.broken_lines,
    )
