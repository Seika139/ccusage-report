"""各シグナル（S1 / S6 / S3 / S2 / S7 / S8 / S4 / S5）のテスト。

S6 は `cumulativeDroppedTokens` を絶対に使わない。累積値なのでイベント列で合算すると
二次関数的に過大計上する（実測で同一セッション内が 159,591 → 1,968,142 と成長する）。

S3 は親の `toolUseResult.totalTokens` を第一候補にしない。実データで照合した結果、
これは最終ターン 1 回分の `usage` の和でしかなく実消費の 2〜80 分の 1 になる。
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

from logstats import (
    RATE_CACHE_READ_MULT,
    S1_MAX_TURNS,
    S1_MIN_CHARS,
    S2_MIN_SAMPLES,
    S2_MIN_TURNS,
    S2_OUTLIER_MULT,
    S3_DEEP_SPAWN_DEPTH,
    S4_MAX_TOTAL_TOKENS,
    S4_MAX_TURNS,
    S5_APPROX_RESULT_TOKENS,
    S5_ARGS_PREVIEW_CHARS,
    S5_MIN_REPEATS,
    S6_MANUAL_REF_ROWS,
    S7_MAX_LOOKAHEAD,
    S8_MIN_COHORT_SESSIONS,
    S8_MIN_OUTPUT_TOKENS,
    S8_OUTLIER_MULT,
    S8_UNKNOWN_EFFORT,
    TOP_N_ROWS,
    RateTable,
    ScanCounters,
    SessionStat,
    TokenUsage,
    _fmt_usd,
    analyze_s1,
    analyze_s2,
    analyze_s3,
    analyze_s3_reference,
    analyze_s4,
    analyze_s5,
    analyze_s6,
    analyze_s7,
    analyze_s8,
    collect_log_stats,
    parse_claude_session,
    parse_codex_session,
)

# $5.00/M input（Opus 相当）で固定し、金額の検算を決定的にする。
RATES = RateTable(rates={"claude-opus-5": 5.0}, fallback=5.0)
MODEL = "claude-opus-5"


def assistant(uuid: str, ts: str, *, tool_id: str | None = None) -> dict[str, Any]:
    """assistant レコードを組み立てる（tool_id 指定で tool_use を含める）。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": "ok"}]
    if tool_id is not None:
        content.append({"type": "tool_use", "id": tool_id, "name": "Read", "input": {}})
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "message": {
            "id": f"msg_{uuid}",
            "model": MODEL,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": content,
        },
    }


def tool_result(uuid: str, ts: str, tool_id: str, size: int) -> dict[str, Any]:
    """指定サイズの tool_result を持つ user レコードを組み立てる。"""
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": "x" * size,
                }
            ],
        },
    }


def compact(
    uuid: str, ts: str, trigger: str, pre: int, post: int, cumulative: int
) -> dict[str, Any]:
    """compact_boundary レコードを組み立てる（累積フィールドも意図的に入れる）。"""
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": uuid,
        "timestamp": ts,
        "compactMetadata": {
            "trigger": trigger,
            "preTokens": pre,
            "postTokens": post,
            "cumulativeDroppedTokens": cumulative,
        },
    }


def parse(tmp_path: Path, records: list[dict[str, Any]], name: str = "s") -> Any:
    """レコード列を JSONL に書いて SessionStat を得る。"""
    path = tmp_path / f"{name}.jsonl"
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )
    stat = parse_claude_session(path, ScanCounters(), set())
    assert stat is not None
    return stat


def test_s1_ignores_output_below_threshold(tmp_path: Path) -> None:
    """閾値未満の tool 結果は S1 の候補にならない。"""
    stat = parse(
        tmp_path,
        [
            assistant("a1", "2026-07-21T01:00:00.000Z", tool_id="t1"),
            tool_result("r1", "2026-07-21T01:00:01.000Z", "t1", S1_MIN_CHARS - 1),
        ],
    )
    sig = analyze_s1([stat], RATES)
    assert sig.count == 0
    assert sig.usd == 0.0


def test_s1_detects_output_above_threshold(tmp_path: Path) -> None:
    """閾値超の tool 結果は tool 名付きで検出される。"""
    stat = parse(
        tmp_path,
        [
            assistant("a1", "2026-07-21T01:00:00.000Z", tool_id="t1"),
            tool_result("r1", "2026-07-21T01:00:01.000Z", "t1", S1_MIN_CHARS * 2),
        ],
    )
    sig = analyze_s1([stat], RATES)
    assert sig.count == 1
    assert stat.big_outputs[0].tool == "Read"


def test_s1_cost_grows_with_following_turns(tmp_path: Path) -> None:
    """以降のターン数が多いほど推定コストが増える（再送による増幅）。"""
    prefix = [
        assistant("a1", "2026-07-21T01:00:00.000Z", tool_id="t1"),
        tool_result("r1", "2026-07-21T01:00:01.000Z", "t1", S1_MIN_CHARS * 2),
    ]
    short = analyze_s1([parse(tmp_path, prefix, name="short")], RATES)

    long_records = [
        *prefix,
        *[
            assistant(f"a{i}", f"2026-07-21T01:0{i}:00.000Z")
            for i in range(2, 2 + S1_MAX_TURNS)
        ],
    ]
    long = analyze_s1([parse(tmp_path, long_records, name="long")], RATES)

    assert long.usd > short.usd
    assert long.tokens > short.tokens


def test_s1_amplification_is_capped(tmp_path: Path) -> None:
    """増幅倍率は S1_MAX_TURNS で上限が効く（放置セッションを過大評価しない）。"""
    records = [
        assistant("a1", "2026-07-21T01:00:00.000Z", tool_id="t1"),
        tool_result("r1", "2026-07-21T01:00:01.000Z", "t1", S1_MIN_CHARS),
        *[
            assistant(f"a{i}", "2026-07-21T02:00:00.000Z")
            for i in range(2, 2 + S1_MAX_TURNS * 3)
        ],
    ]
    stat = parse(tmp_path, records)
    assert stat.big_outputs[0].turns_after == S1_MAX_TURNS


def test_s6_uses_pre_minus_post_not_cumulative(tmp_path: Path) -> None:
    """S6 は preTokens - postTokens のみを使い、累積フィールドを無視する。"""
    stat = parse(
        tmp_path,
        [
            compact("c1", "2026-07-21T01:00:00.000Z", "auto", 200_000, 20_000, 159_591),
            compact(
                "c2", "2026-07-21T02:00:00.000Z", "auto", 300_000, 30_000, 1_968_142
            ),
        ],
    )
    sig = analyze_s6([stat], RATES)
    assert sig.tokens == (200_000 - 20_000) + (300_000 - 30_000)
    # 累積値を使っていれば 2,127,733 になるはずで、そうなっていないことを確認する。
    assert sig.tokens < 159_591 + 1_968_142


def test_s6_manual_trigger_excluded_from_totals(tmp_path: Path) -> None:
    """manual 圧縮は利用者の意思なので合計トークン / 合計 USD に含めない。"""
    stat = parse(
        tmp_path,
        [
            compact(
                "c1", "2026-07-21T01:00:00.000Z", "manual", 500_000, 10_000, 490_000
            ),
            compact("c2", "2026-07-21T02:00:00.000Z", "auto", 100_000, 10_000, 580_000),
        ],
    )
    sig = analyze_s6([stat], RATES)
    assert sig.count == 1
    assert sig.tokens == 90_000
    # 表には参考として manual も出す。
    assert len(sig.rows) == 2
    assert any(row[2].startswith("manual") for row in sig.rows)


def test_s6_auto_rows_are_not_pushed_out_by_manual(tmp_path: Path) -> None:
    """manual が多数かつ廃棄量も大きくても、auto 行は必ず表に残る。

    金額だけでソートして上位 N 件を取ると、表が manual で埋まり警告対象の auto が
    1 件も見えなくなる。auto を優先で埋め、manual は参考として少数だけ足す。
    """
    records = [
        *[
            compact(
                f"m{i}",
                f"2026-07-21T{i:02d}:00:00.000Z",
                "manual",
                900_000,
                10_000,
                0,
            )
            for i in range(TOP_N_ROWS + 5)
        ],
        compact("a1", "2026-07-22T01:00:00.000Z", "auto", 100_000, 10_000, 0),
        compact("a2", "2026-07-22T02:00:00.000Z", "auto", 80_000, 10_000, 0),
    ]
    sig = analyze_s6([parse(tmp_path, records)], RATES)

    assert sig.count == 2
    assert sig.tokens == 90_000 + 70_000
    auto_rows = [row for row in sig.rows if row[2].startswith("auto")]
    manual_rows = [row for row in sig.rows if row[2].startswith("manual")]
    assert len(auto_rows) == 2
    assert len(manual_rows) == S6_MANUAL_REF_ROWS
    assert len(sig.rows) <= TOP_N_ROWS


def test_s6_manual_only_period_keeps_reference_rows(tmp_path: Path) -> None:
    """圧縮が manual のみの期間でも、参考行を持つシグナルは丸ごと破棄されない。"""
    project = tmp_path / "claude" / "-home-ken-proj"
    project.mkdir(parents=True)
    (project / "s.jsonl").write_text(
        json.dumps(
            compact("m1", "2026-07-21T01:00:00.000Z", "manual", 500_000, 1_000, 0)
        )
        + "\n",
        encoding="utf-8",
    )
    stats = collect_log_stats(
        rates=RATES,
        since=None,
        until=None,
        provider="claude",
        claude_root=tmp_path / "claude",
        codex_root=tmp_path / "codex",
    )
    s6 = [s for s in stats.signals if s.key == "S6"]
    assert len(s6) == 1
    assert s6[0].count == 0
    assert s6[0].rows


def test_s6_usd_uses_cache_read_multiplier(tmp_path: Path) -> None:
    """USD は 廃棄トークン × 単価 × 0.1（cache_read 相当）で計算する。"""
    stat = parse(
        tmp_path,
        [compact("c1", "2026-07-21T01:00:00.000Z", "auto", 1_000_000, 0, 0)],
    )
    sig = analyze_s6([stat], RATES)
    assert abs(sig.usd - 1_000_000 * 5.0 * 0.1 / 1_000_000) < 1e-9


def test_s6_returns_zero_without_compaction(tmp_path: Path) -> None:
    """圧縮イベントが無ければ S6 は発火しない。"""
    stat = parse(tmp_path, [assistant("a1", "2026-07-21T01:00:00.000Z")])
    sig = analyze_s6([stat], RATES)
    assert sig.count == 0
    assert sig.rows == []


# --- S3: サブエージェント委譲のオーバーヘッド ----------------------------
def agent_call(
    uuid: str,
    ts: str,
    agent_id: str,
    *,
    resolved_model: str = "global.anthropic.claude-opus-4-8",
    total_tokens: int | None = None,
    is_async: bool = False,
) -> dict[str, Any]:
    """`Agent` tool の結果を持つ user レコードを組み立てる。

    非同期呼び出し（`isAsync: true`）は `totalTokens` を持たないため、
    `total_tokens=None` でその形を再現する。
    """
    result: dict[str, Any] = {
        "agentId": agent_id,
        "resolvedModel": resolved_model,
        "status": "async_launched" if is_async else "completed",
    }
    if is_async:
        result["isAsync"] = True
    if total_tokens is not None:
        result["totalTokens"] = total_tokens
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "message": {"role": "user", "content": []},
        "toolUseResult": result,
    }


def write_subagent(
    session_path: Path,
    agent_id: str,
    *,
    agent_type: str,
    spawn_depth: int = 1,
    turns: int = 1,
    tokens_per_turn: int = 10_000,
    parent_agent_id: str | None = None,
    model: str | None = None,
    write_log: bool = True,
    log_model: str = MODEL,
    log_timestamp: str = "2026-07-21T01:00:00.000Z",
) -> None:
    """`subagents/agent-<id>.jsonl` と `.meta.json` を書く。

    `write_log=False` は `.meta.json` だけがあってログファイル自体が無い状況の
    再現用（`find_subagent_files` はログ起点で探すため集計対象から外れる）。
    `turns=0` は「ログはあるが集計できるレコードが 0 件」の再現用。
    """
    directory = session_path.parent / session_path.stem / "subagents"
    directory.mkdir(parents=True, exist_ok=True)

    meta: dict[str, Any] = {
        "agentType": agent_type,
        "description": "test",
        "toolUseId": f"toolu_{agent_id}",
        "spawnDepth": spawn_depth,
    }
    if parent_agent_id is not None:
        meta["parentAgentId"] = parent_agent_id
    if model is not None:
        meta["model"] = model
    (directory / f"agent-{agent_id}.meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )

    if not write_log:
        return
    records = []
    for i in range(turns):
        message: dict[str, Any] = {
            "id": f"msg_{agent_id}_{i}",
            "usage": {"cache_read_input_tokens": tokens_per_turn},
            "content": [{"type": "text", "text": "ok"}],
        }
        # `log_model=""` は「ログにモデル名が入っていない」状況の再現用。
        if log_model:
            message["model"] = log_model
        records.append(
            {
                "type": "assistant",
                "uuid": f"{agent_id}-u{i}",
                "timestamp": log_timestamp,
                "message": message,
            }
        )
    (directory / f"agent-{agent_id}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


def parse_with_subagents(tmp_path: Path, records: list[dict[str, Any]]) -> SessionStat:
    """親セッションを書いて（subagents は事前に置かれている前提で）解析する。"""
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    stat = parse_claude_session(path, ScanCounters(), set())
    assert stat is not None
    return stat


def test_s3_flags_expensive_model_on_lightweight_agent(tmp_path: Path) -> None:
    """軽量用途の agentType に Opus が割り当たっていれば検出する（陽性）。"""
    session = tmp_path / "session.jsonl"
    write_subagent(session, "a1", agent_type="OutputSummarizer")
    stat = parse_with_subagents(
        tmp_path, [agent_call("u1", "2026-07-21T01:00:00.000Z", "a1")]
    )
    sig = analyze_s3([stat], RATES)
    assert sig.count == 1
    assert "軽量用途" in sig.rows[0][2] or sig.tokens == 10_000
    assert sig.rows[0][3] == "claude-opus-4-8"


def test_s3_ignores_cheap_model_on_lightweight_agent(tmp_path: Path) -> None:
    """軽量用途に Sonnet なら妥当な選択なので検出しない（陰性）。"""
    session = tmp_path / "session.jsonl"
    write_subagent(session, "a1", agent_type="OutputSummarizer")
    stat = parse_with_subagents(
        tmp_path,
        [
            agent_call(
                "u1",
                "2026-07-21T01:00:00.000Z",
                "a1",
                resolved_model="global.anthropic.claude-sonnet-5",
            )
        ],
    )
    sig = analyze_s3([stat], RATES)
    assert sig.count == 0
    assert sig.rows == []
    # agentType 別の参考表には出る。
    assert analyze_s3_reference([stat], RATES).rows


def test_s3_ignores_expensive_model_on_heavy_agent(tmp_path: Path) -> None:
    """重い用途の agentType に Opus は意図的な選択なので検出しない（陰性）。"""
    session = tmp_path / "session.jsonl"
    write_subagent(session, "a1", agent_type="Implementor")
    stat = parse_with_subagents(
        tmp_path, [agent_call("u1", "2026-07-21T01:00:00.000Z", "a1")]
    )
    sig = analyze_s3([stat], RATES)
    assert sig.count == 0


def test_s3_lightweight_matches_plugin_namespaced_type(tmp_path: Path) -> None:
    """プラグイン名前空間付きの agentType でも軽量用途として判定する。"""
    session = tmp_path / "session.jsonl"
    write_subagent(session, "a1", agent_type="myplugin:OutputSummarizer")
    stat = parse_with_subagents(
        tmp_path, [agent_call("u1", "2026-07-21T01:00:00.000Z", "a1")]
    )
    sig = analyze_s3([stat], RATES)
    assert sig.count == 1


def test_s3_resolves_nested_spawn_depth_and_parent(tmp_path: Path) -> None:
    """ネストしたサブエージェントも同一ディレクトリから深度・親付きで解決する。

    `spawnDepth` が深すぎるものは合計に数え、浅いものは参考行に留める。
    """
    session = tmp_path / "session.jsonl"
    write_subagent(session, "a1", agent_type="Orchestrator", spawn_depth=1)
    write_subagent(
        session,
        "a2",
        agent_type="Implementor",
        spawn_depth=S3_DEEP_SPAWN_DEPTH,
        parent_agent_id="a1",
    )
    stat = parse_with_subagents(
        tmp_path,
        [
            agent_call("u1", "2026-07-21T01:00:00.000Z", "a1"),
            agent_call("u2", "2026-07-21T01:01:00.000Z", "a2"),
        ],
    )
    by_id = {c.agent_id: c for c in stat.subagent_calls}
    assert by_id["a2"].parent_agent_id == "a1"
    assert by_id["a2"].spawn_depth == S3_DEEP_SPAWN_DEPTH
    assert by_id["a1"].parent_agent_id == ""

    sig = analyze_s3([stat], RATES)
    assert sig.count == 1
    assert sig.rows[0][4] == str(S3_DEEP_SPAWN_DEPTH)


def test_s3_async_call_without_total_tokens_uses_own_log(tmp_path: Path) -> None:
    """非同期呼び出しで totalTokens が無くても、ログの自前集計で数える。"""
    session = tmp_path / "session.jsonl"
    write_subagent(session, "a1", agent_type="Explore", turns=3, tokens_per_turn=20_000)
    stat = parse_with_subagents(
        tmp_path,
        [agent_call("u1", "2026-07-21T01:00:00.000Z", "a1", is_async=True)],
    )
    call = stat.subagent_calls[0]
    assert call.tokens == 60_000
    assert call.estimated is False
    # モデルは親の resolvedModel から解決できる（非同期でも入っている）。
    assert call.model == "claude-opus-4-8"


def test_s3_total_tokens_is_not_preferred_over_own_log(tmp_path: Path) -> None:
    """同期呼び出しでも totalTokens ではなくログの自前集計を使う。

    実データで totalTokens は最終ターン 1 回分の usage の和でしかなく、
    実消費の 2〜80 分の 1 になるため合計値として使えない。
    """
    session = tmp_path / "session.jsonl"
    write_subagent(session, "a1", agent_type="Explore", turns=4, tokens_per_turn=25_000)
    stat = parse_with_subagents(
        tmp_path,
        [agent_call("u1", "2026-07-21T01:00:00.000Z", "a1", total_tokens=8_949)],
    )
    assert stat.subagent_calls[0].tokens == 100_000


def test_s3_ignores_agent_without_log_file(tmp_path: Path) -> None:
    """`.meta.json` だけでログファイルが無い委譲は集計対象外になる。

    `find_subagent_files` は `agent-*.jsonl` 起点で探すため、ログファイル自体が
    無いと候補にならない。`totalTokens` へのフォールバックはこの経路では
    働かず、「ログはあるが 0 トークンだった」場合にのみ働く
    （`test_s3_falls_back_to_total_tokens_when_log_yields_nothing` を参照）。
    """
    session = tmp_path / "session.jsonl"
    write_subagent(session, "a1", agent_type="OutputSummarizer", write_log=False)
    stat = parse_with_subagents(
        tmp_path,
        [agent_call("u1", "2026-07-21T01:00:00.000Z", "a1", total_tokens=8_949)],
    )
    assert stat.subagent_calls == []


def test_s3_falls_back_to_total_tokens_when_log_yields_nothing(
    tmp_path: Path,
) -> None:
    """ログが 0 トークンなら totalTokens にフォールバックし「推定」と印を付ける。

    期間外・resume で uuid 既出・空ファイルのいずれでも起きる。ここでは
    ログのレコードを期間外の日付にして再現する。
    """
    session = tmp_path / "session.jsonl"
    write_subagent(
        session,
        "a1",
        agent_type="OutputSummarizer",
        log_timestamp="2026-06-01T01:00:00.000Z",
    )
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps(
            agent_call("u1", "2026-07-21T01:00:00.000Z", "a1", total_tokens=8_949)
        )
        + "\n",
        encoding="utf-8",
    )

    stat = parse_claude_session(
        path, ScanCounters(), set(), date(2026, 7, 1), date(2026, 7, 31)
    )
    assert stat is not None
    call = stat.subagent_calls[0]
    assert call.tokens == 8_949
    assert call.estimated is True
    # 内訳が分からないため cache_read 相当として置く。
    assert call.usage.cache_read == 8_949

    sig = analyze_s3([stat], RATES)
    assert sig.count == 1
    assert any("推定" in cell for cell in sig.rows[0])


def test_s3_uses_meta_model_alias_when_no_other_source(tmp_path: Path) -> None:
    """親の記録もログのモデル名も無い場合は `.meta.json` のエイリアスを使う。"""
    session = tmp_path / "session.jsonl"
    write_subagent(
        session, "a1", agent_type="OutputSummarizer", model="opus", log_model=""
    )
    stat = parse_with_subagents(tmp_path, [assistant("a0", "2026-07-21T01:00:00.000Z")])
    call = stat.subagent_calls[0]
    assert call.model == "opus"
    assert analyze_s3([stat], RATES).count == 1


def test_s3_resolves_model_from_log_when_parent_record_missing(
    tmp_path: Path,
) -> None:
    """ネストした委譲でもログ内の最頻 `message.model` からモデルを解決する。

    `spawnDepth` 2 以上の `Agent` の `toolUseResult`（`resolvedModel` を含む）は
    親セッションではなく親サブエージェントのログに記録されるため、親セッション
    からは `resolvedModel` が取れない。その場合でもサブエージェントログの
    assistant レコードは完全形のモデル名を持つので unknown にはならない。
    """
    session = tmp_path / "session.jsonl"
    write_subagent(
        session,
        "a2",
        agent_type="OutputSummarizer",
        spawn_depth=2,
        parent_agent_id="a1",
        model="opus",
        log_model="global.anthropic.claude-opus-4-8",
    )
    # 親セッションには a2 の `Agent` 結果が存在しない（深いネストの実態）。
    stat = parse_with_subagents(tmp_path, [assistant("a0", "2026-07-21T01:00:00.000Z")])
    call = stat.subagent_calls[0]
    assert call.model == "claude-opus-4-8"
    assert call.spawn_depth == 2


def test_s3_prefers_parent_resolved_model_over_log_model(tmp_path: Path) -> None:
    """親の `resolvedModel` が取れる場合はログの最頻モデルより優先する。"""
    session = tmp_path / "session.jsonl"
    write_subagent(session, "a1", agent_type="Explore", log_model="claude-haiku-4-5")
    stat = parse_with_subagents(
        tmp_path,
        [
            agent_call(
                "u1",
                "2026-07-21T01:00:00.000Z",
                "a1",
                resolved_model="global.anthropic.claude-sonnet-5",
            )
        ],
    )
    assert stat.subagent_calls[0].model == "claude-sonnet-5"


def test_s3_usd_weights_token_breakdown_by_field(tmp_path: Path) -> None:
    """USD は合計トークン一律 0.1 倍ではなく、フィールド別の単価で計算する。

    output は基準単価の 5 倍、cache_create は 1.25 倍で課金されるため、
    比率が小さくても金額寄与を無視できない。
    """
    directory = tmp_path / "session" / "subagents"
    directory.mkdir(parents=True)
    (directory / "agent-a1.meta.json").write_text(
        json.dumps({"agentType": "OutputSummarizer", "spawnDepth": 1}),
        encoding="utf-8",
    )
    (directory / "agent-a1.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "a1-u0",
                "timestamp": "2026-07-21T01:00:00.000Z",
                "message": {
                    "id": "msg_a1_0",
                    "model": MODEL,
                    "usage": {
                        "input_tokens": 1_000,
                        "output_tokens": 10_000,
                        "cache_read_input_tokens": 100_000,
                        "cache_creation_input_tokens": 20_000,
                    },
                    "content": [{"type": "text", "text": "ok"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    stat = parse_with_subagents(
        tmp_path, [agent_call("u1", "2026-07-21T01:00:00.000Z", "a1")]
    )
    call = stat.subagent_calls[0]
    assert call.usage.output == 10_000
    assert call.tokens == 131_000

    sig = analyze_s3([stat], RATES)
    weighted = 1_000 + 5.0 * 10_000 + 0.1 * 100_000 + 1.25 * 20_000
    assert abs(sig.usd - weighted * 5.0 / 1_000_000) < 1e-9
    # 一律 0.1 倍だと 131,000 * 0.1 = 13,100 相当で、約 6 分の 1 に潰れる。
    assert sig.usd > 131_000 * 0.1 * 5.0 / 1_000_000


def test_s3_reference_table_columns_match_values(tmp_path: Path) -> None:
    """agentType 別の参考集計は S3 本体と別表になり、列と値の意味が一致する。"""
    session = tmp_path / "session.jsonl"
    # 検出対象にならない agentType（重い用途）を使い、本体の表が空になることも見る。
    write_subagent(
        session, "a1", agent_type="Implementor", turns=2, tokens_per_turn=10_000
    )
    write_subagent(
        session, "a2", agent_type="Implementor", turns=1, tokens_per_turn=10_000
    )
    stat = parse_with_subagents(
        tmp_path,
        [
            agent_call("u1", "2026-07-21T01:00:00.000Z", "a1"),
            agent_call("u2", "2026-07-21T01:01:00.000Z", "a2"),
        ],
    )
    ref = analyze_s3_reference([stat], RATES)
    assert ref.reference is True
    assert [label for label, _ in ref.columns] == [
        "agentType",
        "件数",
        "合計トークン",
        "平均トークン/件",
        "推定 USD",
    ]
    assert ref.rows == [["Implementor", "2", "30,000", "15,000", _fmt_usd(ref.usd)]]
    # 本体の表には参考行が混ざらない（列の意味が違うため）。
    assert analyze_s3([stat], RATES).rows == []


def test_s3_reference_usd_excluded_from_total(tmp_path: Path) -> None:
    """参考表の金額は無駄の合計金額に含めない。"""
    project = tmp_path / "claude" / "-home-ken-proj"
    project.mkdir(parents=True)
    session = project / "s.jsonl"
    # S3 本体が検出しない組み合わせ（重い用途 + Opus）にして、参考表の金額だけが
    # 残る状況を作る。
    write_subagent(session, "a1", agent_type="Implementor")
    session.write_text(
        json.dumps(agent_call("u1", "2026-07-21T01:00:00.000Z", "a1")) + "\n",
        encoding="utf-8",
    )
    stats = collect_log_stats(
        rates=RATES,
        since=None,
        until=None,
        provider="claude",
        claude_root=tmp_path / "claude",
        codex_root=tmp_path / "codex",
    )
    ref = [s for s in stats.signals if s.key == "S3R"]
    assert len(ref) == 1
    assert ref[0].usd > 0
    assert stats.total_usd == 0.0


def test_s3_survives_broken_meta_json(tmp_path: Path) -> None:
    """`.meta.json` が壊れていてもトークン集計は続行する（例外にしない）。"""
    session = tmp_path / "session.jsonl"
    write_subagent(session, "a1", agent_type="Explore")
    meta = tmp_path / "session" / "subagents" / "agent-a1.meta.json"
    meta.write_text("{ broken", encoding="utf-8")
    stat = parse_with_subagents(
        tmp_path, [agent_call("u1", "2026-07-21T01:00:00.000Z", "a1")]
    )
    call = stat.subagent_calls[0]
    assert call.agent_type == "unknown"
    assert call.tokens == 10_000


def test_s3_returns_zero_without_subagents(tmp_path: Path) -> None:
    """サブエージェント委譲が無ければ S3 は発火しない。"""
    stat = parse(tmp_path, [assistant("a1", "2026-07-21T01:00:00.000Z")])
    sig = analyze_s3([stat], RATES)
    assert sig.count == 0
    assert sig.rows == []


# --- S2: セッション単位のキャッシュ非効率 --------------------------------
def session_with_cache(
    name: str, create: int, read: int, *, turns: int = S2_MIN_TURNS
) -> SessionStat:
    """指定した cache_create / cache_read を持つ SessionStat を組み立てる。

    ターン数は既定で母集団の下限ぴったりにする（短いセッションは
    S2 の母集団から外れるため、明示的に指定しない限り除外させない）。
    """
    return SessionStat(
        path=Path(f"/tmp/{name}.jsonl"),
        project=name,
        provider="claude",
        model=MODEL,
        started_at="2026-07-21T01:00:00.000Z",
        usage=TokenUsage(cache_create=create, cache_read=read),
        assistant_turns=turns,
    )


def normal_sessions(count: int) -> list[SessionStat]:
    """比率 0.1（cache がよく効いている）のセッションを count 件作る。"""
    return [session_with_cache(f"ok{i}", 10_000, 100_000) for i in range(count)]


def test_s2_detects_outlier_session() -> None:
    """中央値の閾値倍を超える比率のセッションを外れ値として検出する（陽性）。"""
    sessions = [
        *normal_sessions(S2_MIN_SAMPLES),
        session_with_cache("bad", 500_000, 100_000),
    ]
    sig = analyze_s2(sessions, RATES)
    assert sig.count == 1
    assert sig.rows[0][1] == "bad"
    # 超過分のみ（中央値 0.1 相当の 10,000 を差し引く）を数える。
    assert sig.tokens == 500_000 - 10_000


def test_s2_ignores_sessions_within_distribution() -> None:
    """分布の範囲内に収まるセッションは検出しない（陰性）。"""
    sessions = [
        *normal_sessions(S2_MIN_SAMPLES),
        session_with_cache("mild", 20_000, 100_000),
    ]
    sig = analyze_s2(sessions, RATES)
    assert sig.count == 0
    assert sig.rows == []


def test_s2_threshold_is_relative_to_median_not_absolute() -> None:
    """同じ比率でも、分布全体が高ければ外れ値にならない（相対評価である）。

    絶対閾値なら比率 2.0 は常に警告になるが、期間全体の中央値が 2.0 なら
    それは「その期間の標準的な使い方」であって逸脱ではない。
    """
    high_baseline = [session_with_cache(f"hi{i}", 200_000, 100_000) for i in range(5)]
    subject = session_with_cache("same", 200_000, 100_000)
    assert analyze_s2([*high_baseline, subject], RATES).count == 0

    low_baseline = normal_sessions(5)
    assert analyze_s2([*low_baseline, subject], RATES).count == 1


def test_s2_skipped_when_samples_are_insufficient() -> None:
    """サンプル数が閾値未満なら相対評価が成立しないのでシグナルを出さない。"""
    sessions = [
        *normal_sessions(S2_MIN_SAMPLES - 2),
        session_with_cache("bad", 900_000, 1_000),
    ]
    assert len(sessions) < S2_MIN_SAMPLES
    sig = analyze_s2(sessions, RATES)
    assert sig.count == 0
    assert sig.rows == []


def test_s2_ignores_sessions_without_cache_create() -> None:
    """cache_create が 0 のセッションは分布の母集団に入れない。"""
    sessions = [
        *normal_sessions(S2_MIN_SAMPLES),
        *[session_with_cache(f"zero{i}", 0, 100_000) for i in range(20)],
    ]
    sig = analyze_s2(sessions, RATES)
    assert sig.count == 0


def test_s2_ignores_sessions_without_cache_read() -> None:
    """cache_read が 0 のセッションは母集団に入れない（比率が桁違いに跳ねる）。

    以前は分母を `max(cache_read, 1)` にしていたため、cache_read が 0 の
    セッションで比率 79,014.0 のような値が表に出ていた。これは
    「同じ文脈を作り直している」無駄ではなく、単に cache が 1 度も
    ヒットしなかった短いセッションである。
    """
    sessions = [
        *normal_sessions(S2_MIN_SAMPLES),
        *[session_with_cache(f"noread{i}", 79_014, 0) for i in range(3)],
    ]
    sig = analyze_s2(sessions, RATES)
    assert sig.count == 0
    assert sig.rows == []


def test_s2_ignores_short_sessions() -> None:
    """ターン数が下限未満のセッションは母集団に入れない。

    1〜数ターンでは cache_create しか発生しないのが構造的に当然で、
    比率が高くても無駄の兆候ではない。
    """
    sessions = [
        *normal_sessions(S2_MIN_SAMPLES),
        session_with_cache("short", 500_000, 100_000, turns=S2_MIN_TURNS - 1),
    ]
    sig = analyze_s2(sessions, RATES)
    assert sig.count == 0
    # 同じ比率でもターン数が下限以上なら検出される（フィルタが効いている証拠）。
    long_enough = [
        *normal_sessions(S2_MIN_SAMPLES),
        session_with_cache("long", 500_000, 100_000, turns=S2_MIN_TURNS),
    ]
    assert analyze_s2(long_enough, RATES).count == 1


def test_s2_usd_uses_cache_create_multiplier() -> None:
    """USD は 超過分 × 単価 × 1.25（cache_create 相当）で計算する。"""
    sessions = [
        *normal_sessions(S2_MIN_SAMPLES),
        session_with_cache("bad", 1_010_000, 100_000),
    ]
    sig = analyze_s2(sessions, RATES)
    excess = 1_010_000 - 10_000
    assert abs(sig.usd - excess * 5.0 * 1.25 / 1_000_000) < 1e-9


def test_s2_signal_is_dropped_when_no_outlier() -> None:
    """外れ値が無い期間は S2 がシグナル一覧に載らない。"""
    sig = analyze_s2(normal_sessions(S2_MIN_SAMPLES + 3), RATES)
    assert sig.count == 0
    assert not sig.rows
    assert S2_OUTLIER_MULT > 1.0


# --- S7: 失敗・中断で捨てたトークン --------------------------------------
def error_result(
    uuid: str, ts: str, tool_id: str, size: int, *, is_error: bool | None = True
) -> dict[str, Any]:
    """`is_error` を制御できる tool_result を持つ user レコードを組み立てる。

    `is_error=None` は「キー自体が存在しない」形（実測で約 59% のレコード）の
    再現用。`False` との混同を検出するため両方を作れるようにしている。
    """
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": "x" * size,
    }
    if is_error is not None:
        block["is_error"] = is_error
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "message": {"role": "user", "content": [block]},
    }


def assistant_with_usage(uuid: str, ts: str, cache_read: int) -> dict[str, Any]:
    """指定した cache_read を持つ assistant レコードを組み立てる。"""
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "message": {
            "id": f"msg_{uuid}",
            "model": MODEL,
            "usage": {"cache_read_input_tokens": cache_read},
            "content": [{"type": "text", "text": "retry"}],
        },
    }


def test_s7_detects_error_tool_result(tmp_path: Path) -> None:
    """`is_error is True` の tool 結果は、直後の再送量を伴って検出される（陽性）。"""
    stat = parse(
        tmp_path,
        [
            assistant("a1", "2026-07-21T01:00:00.000Z", tool_id="t1"),
            error_result("e1", "2026-07-21T01:00:01.000Z", "t1", 300),
            assistant_with_usage("a2", "2026-07-21T01:00:02.000Z", 120_000),
        ],
    )
    assert len(stat.errors) == 1
    assert stat.errors[0].tokens == 120_000
    assert stat.errors[0].estimated is False

    sig = analyze_s7([stat], RATES)
    assert sig.count == 1
    assert sig.tokens == 120_000
    # USD は cache_read 相当（基準単価の 0.1 倍）。
    assert abs(sig.usd - 120_000 * 5.0 * 0.1 / 1_000_000) < 1e-9


def test_s7_ignores_missing_and_false_is_error(tmp_path: Path) -> None:
    """`is_error` が欠落 / False の tool 結果は検出しない（陰性）。

    キーが約 59% のレコードで欠落するため、`is True` 以外を成功として扱う。
    """
    stat = parse(
        tmp_path,
        [
            assistant("a1", "2026-07-21T01:00:00.000Z", tool_id="t1"),
            error_result("e1", "2026-07-21T01:00:01.000Z", "t1", 300, is_error=None),
            assistant("a2", "2026-07-21T01:00:02.000Z", tool_id="t2"),
            error_result("e2", "2026-07-21T01:00:03.000Z", "t2", 300, is_error=False),
            assistant_with_usage("a3", "2026-07-21T01:00:04.000Z", 120_000),
        ],
    )
    assert stat.errors == []
    sig = analyze_s7([stat], RATES)
    assert sig.count == 0
    assert sig.rows == []


def test_s7_does_not_bill_synthetic_records(tmp_path: Path) -> None:
    """`<synthetic>` レコードは課金対象ではないので再送量に数えない。

    `isApiErrorMessage: true` のクライアント側エラーは usage が全ゼロで
    実際のコストが発生していない。ここを取り違えると架空の数字が出る。
    """
    synthetic = {
        "type": "assistant",
        "uuid": "syn",
        "timestamp": "2026-07-21T01:00:02.000Z",
        "isApiErrorMessage": True,
        "message": {
            "id": "msg_syn",
            "model": "<synthetic>",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            "content": [{"type": "text", "text": "API Error"}],
        },
    }
    stat = parse(
        tmp_path,
        [
            assistant("a1", "2026-07-21T01:00:00.000Z", tool_id="t1"),
            error_result("e1", "2026-07-21T01:00:01.000Z", "t1", 400),
            synthetic,
            assistant_with_usage("a2", "2026-07-21T01:00:03.000Z", 50_000),
        ],
    )
    # synthetic を再送量として引き当てていれば 0 トークンになる。
    assert len(stat.errors) == 1
    assert stat.errors[0].tokens == 50_000
    assert stat.errors[0].estimated is False


def test_s7_falls_back_to_error_size_at_session_end(tmp_path: Path) -> None:
    """セッション末尾のエラーは再送が観測できないので本文サイズで近似する。"""
    stat = parse(
        tmp_path,
        [
            assistant("a1", "2026-07-21T01:00:00.000Z", tool_id="t1"),
            error_result("e1", "2026-07-21T01:00:01.000Z", "t1", 4_000),
        ],
    )
    assert len(stat.errors) == 1
    assert stat.errors[0].tokens == 4_000 // 4
    assert stat.errors[0].estimated is True
    assert any("推定" in cell for cell in analyze_s7([stat], RATES).rows[0])


def test_s7_splits_resend_across_parallel_errors(tmp_path: Path) -> None:
    """並列に失敗した複数の tool 結果で、1 回の再送を二重計上しない。"""
    parallel = {
        "type": "user",
        "uuid": "e1",
        "timestamp": "2026-07-21T01:00:01.000Z",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"t{i}",
                    "content": "x" * 100,
                    "is_error": True,
                }
                for i in (1, 2)
            ],
        },
    }
    stat = parse(
        tmp_path,
        [
            assistant("a1", "2026-07-21T01:00:00.000Z", tool_id="t1"),
            parallel,
            assistant_with_usage("a2", "2026-07-21T01:00:02.000Z", 100_000),
        ],
    )
    assert len(stat.errors) == 2
    assert analyze_s7([stat], RATES).tokens == 100_000


def codex_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Codex のロールアウトレコードに共通の timestamp を付ける。"""
    return [{"timestamp": "2026-07-21T01:00:00.000Z", **r} for r in records]


def codex_token_count(
    input_tokens: int, output_tokens: int = 0, reasoning: int = 0
) -> dict[str, Any]:
    """`token_count` イベント（累積値）を組み立てる。"""
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": 0,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": reasoning,
                },
                "last_token_usage": {"input_tokens": input_tokens},
            },
        },
    }


def parse_codex(tmp_path: Path, records: list[dict[str, Any]], name: str = "c") -> Any:
    """Codex のレコード列を JSONL に書いて SessionStat を得る。"""
    path = tmp_path / f"rollout-{name}.jsonl"
    path.write_text(
        "".join(
            json.dumps(r, ensure_ascii=False) + "\n" for r in codex_records(records)
        ),
        encoding="utf-8",
    )
    stat = parse_codex_session(path, ScanCounters(), set())
    assert stat is not None
    return stat


def test_s7_detects_codex_turn_aborted(tmp_path: Path) -> None:
    """Codex の `turn_aborted` を、ターン開始時点との累積差分付きで検出する。"""
    stat = parse_codex(
        tmp_path,
        [
            {"type": "turn_context", "payload": {"model": "gpt-5.5", "effort": "high"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            codex_token_count(200_000),
            {
                "type": "event_msg",
                "payload": {
                    "type": "turn_aborted",
                    "reason": "interrupted",
                    "duration_ms": 96_823,
                },
            },
        ],
    )
    assert len(stat.errors) == 1
    event = stat.errors[0]
    assert event.kind == "turn_aborted"
    assert event.detail == "interrupted"
    # `task_started` 時点の累積は 0 なので、差分は 200,000。
    assert event.tokens == 200_000

    sig = analyze_s7([stat], RATES)
    assert sig.count == 1
    assert sig.tokens == 200_000


def test_s7_codex_abort_uses_diff_not_cumulative_total(tmp_path: Path) -> None:
    """中断ターンの消費は累積値そのものではなく、ターン開始からの差分を使う。"""
    stat = parse_codex(
        tmp_path,
        [
            {"type": "event_msg", "payload": {"type": "task_started"}},
            codex_token_count(180_000),
            {"type": "event_msg", "payload": {"type": "task_started"}},
            codex_token_count(1_180_000),
            {
                "type": "event_msg",
                "payload": {"type": "turn_aborted", "reason": "interrupted"},
            },
        ],
    )
    assert stat.errors[0].tokens == 1_180_000 - 180_000


def test_s7_codex_abort_across_period_boundary_is_not_inflated(
    tmp_path: Path,
) -> None:
    """期間外の `task_started` でも基準を記録し、累積値を丸ごと計上しない。

    期間フィルタで `task_started` を落とすと、`turn_aborted` の差分が
    「セッション累積 - 0」に膨らむ（レビューで 9,000 倍の過大計上を再現）。
    """
    path = tmp_path / "rollout-boundary.jsonl"
    records: list[dict[str, Any]] = [
        # 6 月時点で既に累積 9 億まで積み上がっており、そこでターンが始まる。
        {"timestamp": "2026-06-30T01:00:00.000Z", **codex_token_count(900_000_000)},
        {
            "timestamp": "2026-06-30T01:00:01.000Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "t1"},
        },
        # 7 月に 10 万トークンだけ追加消費して中断された。
        {"timestamp": "2026-07-01T01:00:00.000Z", **codex_token_count(900_100_000)},
        {
            "timestamp": "2026-07-01T01:00:01.000Z",
            "type": "event_msg",
            "payload": {
                "type": "turn_aborted",
                "reason": "interrupted",
                "turn_id": "t1",
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )
    stat = parse_codex_session(path, ScanCounters(), set(), date(2026, 7, 1), None)
    assert stat is not None
    assert len(stat.errors) == 1
    assert stat.errors[0].tokens == 100_000


def test_s7_codex_abort_without_turn_start_falls_back_to_last_usage(
    tmp_path: Path,
) -> None:
    """`task_started` を観測できないターンは累積差分を計上しない（安全側）。"""
    cumulative: dict[str, Any] = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"input_tokens": 900_000_000},
                "last_token_usage": {"input_tokens": 120_000},
            },
        },
    }
    stat = parse_codex(
        tmp_path,
        [
            cumulative,
            {
                "type": "event_msg",
                "payload": {"type": "turn_aborted", "reason": "interrupted"},
            },
        ],
        name="nobase",
    )
    # 累積 9 億ではなく直近 1 リクエスト分（last_token_usage）に留まる。
    assert stat.errors[0].tokens == 120_000
    assert stat.errors[0].detail.endswith("（ターン基準不明）")


def test_s7_codex_forked_abort_counted_once(tmp_path: Path) -> None:
    """fork で複製された同一の `turn_aborted` は 1 回だけ計上する。

    Codex の fork は親の履歴を丸ごと複製する。実測で 6 月の S7 検出トークンの
    35% が、同一イベントを 4 ファイルから数えたことによる過大計上だった。
    """
    codex_root = tmp_path / "codex" / "2026" / "07" / "21"
    codex_root.mkdir(parents=True)
    records: list[dict[str, Any]] = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
        codex_token_count(200_000),
        {
            "type": "event_msg",
            "payload": {
                "type": "turn_aborted",
                "reason": "interrupted",
                "turn_id": "t1",
            },
        },
    ]
    body = "".join(
        json.dumps(r, ensure_ascii=False) + "\n" for r in codex_records(records)
    )
    for name in ("parent", "fork1", "fork2"):
        (codex_root / f"rollout-2026-07-21T01-00-00-{name}.jsonl").write_text(
            body, encoding="utf-8"
        )

    stats = collect_log_stats(
        rates=RATES,
        since=None,
        until=None,
        provider="codex",
        claude_root=tmp_path / "claude",
        codex_root=tmp_path / "codex",
    )
    s7 = [s for s in stats.signals if s.key == "S7"]
    assert len(s7) == 1
    assert s7[0].count == 1
    assert s7[0].tokens == 200_000


def test_s7_codex_abort_day_comes_from_completed_at(tmp_path: Path) -> None:
    """中断の日付はレコードの `timestamp` ではなく `completed_at` で決める。

    fork は複製した履歴のレコード側 `timestamp` を複製時刻に書き換えるため
    （実測で本来 06-12 の中断が 06-15 として現れた）、そのままでは fork の
    有無で検出日が動く。`completed_at` は元の中断時刻を保つ。
    """
    path = tmp_path / "rollout-forked.jsonl"
    # 2026-06-12T08:05:38Z を UNIX 秒で表したもの。
    completed_at = 1781251538
    records: list[dict[str, Any]] = [
        {
            "timestamp": "2026-06-15T04:14:12.000Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "t1"},
        },
        {"timestamp": "2026-06-15T04:14:12.000Z", **codex_token_count(50_000)},
        {
            "timestamp": "2026-06-15T04:14:12.000Z",
            "type": "event_msg",
            "payload": {
                "type": "turn_aborted",
                "reason": "interrupted",
                "turn_id": "t1",
                "completed_at": completed_at,
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )
    stat = parse_codex_session(path, ScanCounters(), set())
    assert stat is not None
    assert stat.errors[0].day == "2026-06-12"

    # 期間判定も `completed_at` に従う（06-15 起点では対象外になる）。
    later = parse_codex_session(path, ScanCounters(), set(), date(2026, 6, 15), None)
    assert later is None or later.errors == []


def test_s7_gives_up_on_resend_beyond_lookahead(tmp_path: Path) -> None:
    """`S7_MAX_LOOKAHEAD` を超えて離れた assistant には紐付けない。

    遠い assistant はエラーと無関係な後続ターンである可能性が高いので、
    再送量ではなくエラー本文サイズからの近似に落とす。
    """
    filler = [
        error_result(
            f"f{i}", f"2026-07-21T01:00:{i + 2:02d}.000Z", "tX", 10, is_error=None
        )
        for i in range(S7_MAX_LOOKAHEAD + 1)
    ]
    stat = parse(
        tmp_path,
        [
            assistant("a1", "2026-07-21T01:00:00.000Z", tool_id="t1"),
            error_result("e1", "2026-07-21T01:00:01.000Z", "t1", 4_000),
            *filler,
            assistant_with_usage("a2", "2026-07-21T01:01:00.000Z", 500_000),
        ],
    )
    assert len(stat.errors) == 1
    assert stat.errors[0].tokens == 4_000 // 4
    assert stat.errors[0].estimated is True


def test_s7_links_resend_within_lookahead(tmp_path: Path) -> None:
    """`S7_MAX_LOOKAHEAD` 以内なら、間にレコードが挟まっても再送量を引き当てる。"""
    stat = parse(
        tmp_path,
        [
            assistant("a1", "2026-07-21T01:00:00.000Z", tool_id="t1"),
            error_result("e1", "2026-07-21T01:00:01.000Z", "t1", 4_000),
            error_result("f1", "2026-07-21T01:00:02.000Z", "tX", 10, is_error=None),
            assistant_with_usage("a2", "2026-07-21T01:00:03.000Z", 500_000),
        ],
    )
    assert len(stat.errors) == 1
    assert stat.errors[0].tokens == 500_000
    assert stat.errors[0].estimated is False


def test_s7_returns_zero_without_errors(tmp_path: Path) -> None:
    """エラーも中断も無ければ S7 は発火しない。"""
    stat = parse(tmp_path, [assistant("a1", "2026-07-21T01:00:00.000Z")])
    sig = analyze_s7([stat], RATES)
    assert sig.count == 0
    assert sig.rows == []


# --- S8: reasoning トークン比率 vs effort 設定 ----------------------------
CODEX_MODEL = "gpt-5.5"
CODEX_RATES = RateTable(rates={CODEX_MODEL: 5.0}, fallback=5.0)


def codex_session(
    name: str,
    *,
    output: int,
    reasoning: int,
    effort: str = "xhigh",
    model: str = CODEX_MODEL,
) -> SessionStat:
    """指定した reasoning 比率を持つ Codex の SessionStat を組み立てる。"""
    return SessionStat(
        path=Path(f"/tmp/{name}.jsonl"),
        project=name,
        provider="codex",
        model=model,
        effort=effort,
        started_at="2026-07-21T01:00:00.000Z",
        usage=TokenUsage(output=output),
        reasoning_output=reasoning,
    )


def codex_cohort(count: int, *, effort: str = "xhigh") -> list[SessionStat]:
    """比率 0.4（実測の xhigh 中央値相当）のセッションを count 件作る。"""
    return [
        codex_session(f"{effort}{i}", output=100_000, reasoning=40_000, effort=effort)
        for i in range(count)
    ]


def test_s8_detects_outlier_within_cohort() -> None:
    """コホート中央値の閾値倍を超える比率のセッションを検出する（陽性）。"""
    sessions = [
        *codex_cohort(S8_MIN_COHORT_SESSIONS),
        codex_session("hot", output=100_000, reasoning=90_000),
    ]
    sig = analyze_s8(sessions, CODEX_RATES)
    assert sig.count == 1
    assert sig.rows[0][1] == "hot"
    # 超過分は 中央値比率 0.4 相当の 40,000 を差し引いた分だけ。
    assert sig.tokens == 90_000 - 40_000


def test_s8_ignores_sessions_within_cohort_distribution() -> None:
    """コホートの分布内に収まるセッションは検出しない（陰性）。"""
    sessions = [
        *codex_cohort(S8_MIN_COHORT_SESSIONS),
        codex_session("mild", output=100_000, reasoning=48_000),
    ]
    sig = analyze_s8(sessions, CODEX_RATES)
    assert sig.count == 0
    assert sig.rows == []


def test_s8_threshold_is_relative_to_cohort_not_absolute() -> None:
    """同じ比率でも、コホートの中央値が高ければ外れ値にならない。

    実測では xhigh の中央値 40% に対し high は 25% で、単一の絶対閾値では
    どちらか一方が必ず誤判定になる。
    """
    subject = codex_session("same", output=100_000, reasoning=50_000, effort="high")
    high_cohort = [
        codex_session(f"h{i}", output=100_000, reasoning=25_000, effort="high")
        for i in range(S8_MIN_COHORT_SESSIONS)
    ]
    assert analyze_s8([*high_cohort, subject], CODEX_RATES).count == 1

    generous_cohort = [
        codex_session(f"g{i}", output=100_000, reasoning=44_000, effort="high")
        for i in range(S8_MIN_COHORT_SESSIONS)
    ]
    assert analyze_s8([*generous_cohort, subject], CODEX_RATES).count == 0


def test_s8_skips_cohorts_with_too_few_sessions() -> None:
    """コホートのセッション数が不足していれば中央値を信頼せず評価しない。"""
    sessions = [
        *codex_cohort(S8_MIN_COHORT_SESSIONS - 2),
        codex_session("hot", output=100_000, reasoning=95_000),
    ]
    assert len(sessions) < S8_MIN_COHORT_SESSIONS
    sig = analyze_s8(sessions, CODEX_RATES)
    assert sig.count == 0
    assert sig.rows == []


def test_s8_cohorts_are_split_by_effort() -> None:
    """コホートは (モデル, effort) 単位。別 effort のセッションは母集団を埋めない。

    xhigh が十分な件数あっても、high が 1 件だけなら high は評価対象外になる。
    """
    sessions = [
        *codex_cohort(S8_MIN_COHORT_SESSIONS),
        codex_session("lone", output=100_000, reasoning=95_000, effort="high"),
    ]
    sig = analyze_s8(sessions, CODEX_RATES)
    assert sig.count == 0


def test_s8_unknown_effort_cohort_gets_no_recommendation() -> None:
    """effort 不明のコホートには「effort を下げよ」という推奨をしない。

    ラベルの無いデフォルト設定であって設定ミスではないため。
    """
    sessions = [
        *codex_cohort(S8_MIN_COHORT_SESSIONS, effort=S8_UNKNOWN_EFFORT),
        codex_session(
            "hot", output=100_000, reasoning=90_000, effort=S8_UNKNOWN_EFFORT
        ),
    ]
    sig = analyze_s8(sessions, CODEX_RATES)
    assert sig.count == 1
    assert sig.rows[0][-1] == "effort 未設定のため推奨なし"

    labeled = [
        *codex_cohort(S8_MIN_COHORT_SESSIONS),
        codex_session("hot2", output=100_000, reasoning=90_000),
    ]
    assert analyze_s8(labeled, CODEX_RATES).rows[0][-1] == "effort の見直し候補"


def test_s8_ignores_sessions_below_min_output() -> None:
    """output が下限未満のセッションは母集団に入れない（比率が不安定になる）。"""
    small = S8_MIN_OUTPUT_TOKENS - 1
    sessions = [
        *codex_cohort(S8_MIN_COHORT_SESSIONS),
        codex_session("tiny", output=small, reasoning=small),
    ]
    sig = analyze_s8(sessions, CODEX_RATES)
    assert sig.count == 0
    # 同じ比率でも下限以上なら検出される（フィルタが効いている証拠）。
    big = S8_MIN_OUTPUT_TOKENS
    enough = [
        *codex_cohort(S8_MIN_COHORT_SESSIONS),
        codex_session("big", output=big, reasoning=big),
    ]
    assert analyze_s8(enough, CODEX_RATES).count == 1


def test_s8_ignores_claude_sessions() -> None:
    """S8 は Codex 専用。Claude のセッションは母集団に入れない。"""
    claude = [
        SessionStat(
            path=Path(f"/tmp/cl{i}.jsonl"),
            project="claude",
            provider="claude",
            model=MODEL,
            usage=TokenUsage(output=100_000),
            reasoning_output=90_000,
        )
        for i in range(S8_MIN_COHORT_SESSIONS + 1)
    ]
    sig = analyze_s8(claude, RATES)
    assert sig.count == 0
    assert sig.rows == []


def test_s8_usd_uses_output_multiplier() -> None:
    """USD は 超過分 × 単価 × 5（output 相当）で計算する。

    Codex の `output_tokens` は `reasoning_output_tokens` を含むため、
    reasoning の超過分も output と同じ単価で課金されている。
    """
    sessions = [
        *codex_cohort(S8_MIN_COHORT_SESSIONS),
        codex_session("hot", output=100_000, reasoning=90_000),
    ]
    sig = analyze_s8(sessions, CODEX_RATES)
    excess = 90_000 - 40_000
    assert abs(sig.usd - excess * 5.0 * 5.0 / 1_000_000) < 1e-9
    assert S8_OUTLIER_MULT > 1.0


def test_s8_parses_effort_and_reasoning_from_codex_log(tmp_path: Path) -> None:
    """`turn_context.effort` と累積 `reasoning_output_tokens` をログから拾う。

    累積値なので合算せず最後の観測値で上書きする（合算すると二次関数的に
    過大計上する）。
    """
    stat = parse_codex(
        tmp_path,
        [
            {
                "type": "turn_context",
                "payload": {"model": "gpt-5.5", "effort": "xhigh"},
            },
            codex_token_count(10_000, output_tokens=400, reasoning=300),
            codex_token_count(28_000, output_tokens=583, reasoning=309),
        ],
    )
    assert stat.effort == "xhigh"
    assert stat.usage.output == 583
    assert stat.reasoning_output == 309


def test_s8_effort_null_stays_unknown(tmp_path: Path) -> None:
    """`turn_context.effort` が null のセッションは unknown コホートになる。"""
    stat = parse_codex(
        tmp_path,
        [
            {"type": "turn_context", "payload": {"model": "gpt-5.5", "effort": None}},
            codex_token_count(10_000, output_tokens=400, reasoning=300),
        ],
    )
    assert stat.effort == S8_UNKNOWN_EFFORT


# --- S4: モデル選択の妥当性（参考値） ------------------------------------
# 最安モデル（Haiku 相当 $1.00/M）と Opus（$5.00/M）を並べ、単価差の検算を可能にする。
S4_RATES = RateTable(
    rates={"claude-opus-5": 5.0, "claude-haiku-4-5": 1.0}, fallback=5.0
)


def model_session(
    name: str,
    *,
    model: str = MODEL,
    turns: int = 2,
    total: int = 50_000,
    provider: str = "claude",
) -> SessionStat:
    """S4 用のセッションを組み立てる（トークンは全て cache_read に置く）。"""
    return SessionStat(
        path=Path(f"/tmp/{name}.jsonl"),
        project=name,
        provider=provider,
        model=model,
        started_at="2026-07-21T01:00:00.000Z",
        usage=TokenUsage(cache_read=total),
        assistant_turns=turns,
    )


def test_s4_detects_short_work_on_expensive_model() -> None:
    """短く小規模な作業に高価なモデルを使ったセッションを検出する（陽性）。"""
    sig = analyze_s4([model_session("short-opus")], S4_RATES)
    assert sig.count == 1
    assert sig.rows[0][2] == MODEL
    assert sig.usd > 0


def test_s4_ignores_cheapest_model() -> None:
    """最安モデルを使ったセッションは削減余地が無いので検出しない（陰性）。"""
    sig = analyze_s4([model_session("short-haiku", model="claude-haiku-4-5")], S4_RATES)
    assert sig.count == 0
    assert sig.rows == []


def test_s4_ignores_large_context_sessions() -> None:
    """大量のトークンを扱った作業は母集団から除外する（陰性）。

    「大量の情報を読んで一発で答えを出した」可能性があり、
    高価なモデルの使用が正当化されやすいため。
    """
    big = analyze_s4([model_session("big", total=S4_MAX_TOTAL_TOKENS + 1)], S4_RATES)
    assert big.count == 0
    # 同じターン数でも総量が上限以下なら検出される（フィルタが効いている証拠）。
    small = analyze_s4([model_session("small", total=S4_MAX_TOTAL_TOKENS)], S4_RATES)
    assert small.count == 1


def test_s4_ignores_long_sessions() -> None:
    """ターン数が多いセッションは「短い作業」ではないので検出しない（陰性）。"""
    sig = analyze_s4([model_session("long", turns=S4_MAX_TURNS + 1)], S4_RATES)
    assert sig.count == 0
    assert analyze_s4([model_session("ok", turns=S4_MAX_TURNS)], S4_RATES).count == 1


def test_s4_usd_is_upper_bound_of_rate_gap() -> None:
    """金額は「最安モデルとの単価差 × 実効トークン量」の上限値である。"""
    sig = analyze_s4([model_session("gap", total=100_000)], S4_RATES)
    # cache_read は基準単価の 0.1 倍で課金される。
    expected = 100_000 * RATE_CACHE_READ_MULT * (5.0 - 1.0) / 1_000_000
    assert abs(sig.usd - expected) < 1e-9


def test_s4_is_reference_and_excluded_from_total(tmp_path: Path) -> None:
    """S4 は参考値なので合計 USD と示唆に含めない。"""
    assert analyze_s4([model_session("ref")], S4_RATES).reference is True


def test_s4_excludes_subagent_sessions(tmp_path: Path) -> None:
    """サブエージェントのログは S4 の母集団に含めない。

    親から見た軽量 agentType への高価モデル割り当ては S3 が検出する。
    S4 はユーザーが直接操作したトップレベルのセッションだけを見る。
    """
    project = tmp_path / "claude" / "-home-ken-proj"
    project.mkdir(parents=True)
    session = project / "s.jsonl"
    # 親セッション自体は 1 レコードだけ持ち、S4 の条件（短い / 小規模 / Opus）を
    # 満たすサブエージェントログを配下に置く。
    write_subagent(session, "a1", agent_type="Explore", turns=2, tokens_per_turn=10_000)
    session.write_text(
        json.dumps(agent_call("u1", "2026-07-21T01:00:00.000Z", "a1")) + "\n",
        encoding="utf-8",
    )
    stats = collect_log_stats(
        rates=S4_RATES,
        since=None,
        until=None,
        provider="claude",
        claude_root=tmp_path / "claude",
        codex_root=tmp_path / "codex",
    )
    s4 = [s for s in stats.signals if s.key == "S4"]
    # サブエージェントのパスが行に現れない（そもそも母集団に入っていない）。
    for sig in s4:
        for row in sig.rows:
            assert "subagents" not in row[1]
    assert stats.total_usd >= 0.0


def test_s4_signal_is_dropped_when_nothing_detected() -> None:
    """検出 0 件なら `collect_log_stats` の signals に残らない。"""
    sig = analyze_s4([model_session("cheap", model="claude-haiku-4-5")], S4_RATES)
    assert sig.count == 0
    assert not sig.rows


# --- S5: 重複 tool 呼び出し（参考値） ------------------------------------
def tool_use_record(
    uuid: str, ts: str, tool_id: str, tool: str, args: dict[str, Any]
) -> dict[str, Any]:
    """指定した tool 名と引数の `tool_use` を持つ assistant レコード。"""
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "message": {
            "id": f"msg_{uuid}",
            "model": MODEL,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [
                {"type": "tool_use", "id": tool_id, "name": tool, "input": args}
            ],
        },
    }


def repeated_calls(
    tool: str, args: dict[str, Any], count: int, start: int = 0
) -> list[dict[str, Any]]:
    """同一の tool + 引数の呼び出しを count 回並べる。"""
    return [
        tool_use_record(
            f"u{start + i}",
            f"2026-07-21T01:00:{start + i:02d}.000Z",
            f"t{start + i}",
            tool,
            args,
        )
        for i in range(count)
    ]


def test_s5_detects_repeated_identical_calls(tmp_path: Path) -> None:
    """同一の (tool 名, 引数) が閾値回数以上繰り返されたら検出する（陽性）。"""
    stat = parse(
        tmp_path, repeated_calls("Bash", {"command": "sleep 60"}, S5_MIN_REPEATS)
    )
    sig = analyze_s5([stat], RATES)
    assert sig.count == 1
    assert sig.rows[0][1] == "Bash"
    assert sig.rows[0][3] == str(S5_MIN_REPEATS)
    # 2 回目以降だけを数える。
    assert sig.tokens == (S5_MIN_REPEATS - 1) * S5_APPROX_RESULT_TOKENS


def test_s5_ignores_repeats_below_threshold(tmp_path: Path) -> None:
    """閾値未満の繰り返しは検出しない（陰性）。"""
    stat = parse(
        tmp_path, repeated_calls("Bash", {"command": "ls"}, S5_MIN_REPEATS - 1)
    )
    sig = analyze_s5([stat], RATES)
    assert sig.count == 0
    assert sig.rows == []


def test_s5_distinguishes_different_arguments(tmp_path: Path) -> None:
    """tool 名が同じでも引数が違えば別パターンとして数える。

    既存の `tool_calls` は tool 名だけのカウントなので、引数まで見るのは S5 専用。
    """
    records: list[dict[str, Any]] = []
    for i in range(S5_MIN_REPEATS):
        records += repeated_calls("Bash", {"command": f"echo {i}"}, 1, start=i)
    stat = parse(tmp_path, records)
    assert stat.tool_calls["Bash"] == S5_MIN_REPEATS
    assert analyze_s5([stat], RATES).count == 0


def test_s5_normalizes_argument_key_order(tmp_path: Path) -> None:
    """キーの順序が違うだけの引数は同一パターンとして扱う。"""
    records: list[dict[str, Any]] = []
    for i in range(S5_MIN_REPEATS):
        args = {"a": 1, "b": 2} if i % 2 == 0 else {"b": 2, "a": 1}
        records += repeated_calls("Grep", args, 1, start=i)
    stat = parse(tmp_path, records)
    assert analyze_s5([stat], RATES).count == 1


def test_s5_excludes_reread_after_edit(tmp_path: Path) -> None:
    """同一パスへの `Edit` が介在した後の再読み込みは重複に数えない（陰性）。

    変更結果の正当な再確認であるため。
    """
    path_args = {"file_path": "/tmp/a.py"}
    records: list[dict[str, Any]] = []
    index = 0
    for _ in range(S5_MIN_REPEATS):
        records += repeated_calls("Read", path_args, 1, start=index)
        index += 1
        records += repeated_calls(
            "Edit", {"file_path": "/tmp/a.py", "old_string": "x"}, 1, start=index
        )
        index += 1
    stat = parse(tmp_path, records)
    sig = analyze_s5([stat], RATES)
    # Read は毎回 Edit 後の再確認なので数え直され、閾値に到達しない。
    assert [r for r in sig.rows if r[1] == "Read"] == []


def test_s5_counts_reread_without_intervening_edit(tmp_path: Path) -> None:
    """変更が介在しない同一パスの再読み込みは重複として数える（陽性）。"""
    stat = parse(
        tmp_path,
        repeated_calls("Read", {"file_path": "/tmp/a.py"}, S5_MIN_REPEATS),
    )
    sig = analyze_s5([stat], RATES)
    assert sig.count == 1
    assert sig.rows[0][1] == "Read"


def test_s5_truncates_long_argument_preview(tmp_path: Path) -> None:
    """引数の要約は長すぎる場合に切り詰める（表が壊れないため）。"""
    stat = parse(
        tmp_path,
        repeated_calls("Bash", {"command": "x" * 500}, S5_MIN_REPEATS),
    )
    sig = analyze_s5([stat], RATES)
    assert len(sig.rows[0][2]) <= S5_ARGS_PREVIEW_CHARS + 1


def test_s5_is_reference_and_excluded_from_total(tmp_path: Path) -> None:
    """S5 は参考値なので合計 USD に含めない。"""
    project = tmp_path / "claude" / "-home-ken-proj"
    project.mkdir(parents=True)
    session = project / "s.jsonl"
    session.write_text(
        "".join(
            json.dumps(r) + "\n"
            for r in repeated_calls("Bash", {"command": "sleep 60"}, S5_MIN_REPEATS)
        ),
        encoding="utf-8",
    )
    stats = collect_log_stats(
        rates=RATES,
        since=None,
        until=None,
        provider="claude",
        claude_root=tmp_path / "claude",
        codex_root=tmp_path / "codex",
    )
    s5 = [s for s in stats.signals if s.key == "S5"]
    assert len(s5) == 1
    assert s5[0].reference is True
    assert s5[0].usd > 0
    assert stats.total_usd == 0.0


def test_s5_does_not_count_duplicate_uuid_records(tmp_path: Path) -> None:
    """fork/resume で複製されたレコードは重複回数に数えない。

    `seen_uuids` で既出と判定されたレコードは課金対象ではないため。
    """
    records = repeated_calls("Bash", {"command": "sleep 60"}, S5_MIN_REPEATS)
    path = tmp_path / "s.jsonl"
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records + records), encoding="utf-8"
    )
    stat = parse_claude_session(path, ScanCounters(), set())
    assert stat is not None
    sig = analyze_s5([stat], RATES)
    assert sig.rows[0][3] == str(S5_MIN_REPEATS)


def test_s5_ignores_calls_without_recorded_arguments(tmp_path: Path) -> None:
    """引数が記録されていない呼び出しは重複の根拠にならないので数えない。

    実データで `input` が空の `tool_use` が存在し（引数を持たない
    `EnterPlanMode` や、記録が落ちた `Bash`）、これを数えると中身の違う
    10 回の `Bash` が 1 パターンの重複として誤検出された。
    """
    stat = parse(tmp_path, repeated_calls("Bash", {}, S5_MIN_REPEATS * 2))
    assert stat.tool_calls["Bash"] == S5_MIN_REPEATS * 2
    assert analyze_s5([stat], RATES).count == 0


def write_subagent_with_calls(
    session_path: Path,
    agent_id: str,
    records: list[dict[str, Any]],
    *,
    agent_type: str = "Implementor",
) -> None:
    """任意のレコード列を持つサブエージェントログと `.meta.json` を書く。"""
    directory = session_path.parent / session_path.stem / "subagents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"agent-{agent_id}.meta.json").write_text(
        json.dumps({"agentType": agent_type, "spawnDepth": 1}), encoding="utf-8"
    )
    (directory / f"agent-{agent_id}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )


def subagent_calls(
    tool: str, args: dict[str, Any], count: int, prefix: str = "a1"
) -> list[dict[str, Any]]:
    """サブエージェントログ用の tool_use を持つ assistant レコード列。"""
    records = []
    for i in range(count):
        records.append(
            {
                "type": "assistant",
                "uuid": f"{prefix}-u{i}",
                "timestamp": f"2026-07-21T01:00:{i:02d}.000Z",
                "message": {
                    "id": f"msg_{prefix}_{i}",
                    "model": MODEL,
                    "usage": {"cache_read_input_tokens": 1_000},
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"{prefix}-t{i}",
                            "name": tool,
                            "input": args,
                        }
                    ],
                },
            }
        )
    return records


def test_s5_merges_subagent_duplicates_into_parent(tmp_path: Path) -> None:
    """サブエージェントログ内の重複を親の `SessionStat` に合流させる（陽性）。

    実装をサブエージェントに委譲する運用では tool 呼び出しの大半が構造的に
    サブエージェント側で発生するため（実データではトップレベルの
    1,141 セッションに閾値超えの重複が 1 件も無かった）、親のログだけを
    見ていると S5 が原理的に検出できない。
    """
    session = tmp_path / "session.jsonl"
    write_subagent_with_calls(
        session,
        "a1",
        subagent_calls("Bash", {"command": "sleep 60"}, S5_MIN_REPEATS),
    )
    stat = parse_with_subagents(
        tmp_path, [agent_call("u1", "2026-07-21T01:00:00.000Z", "a1")]
    )
    assert [d.tool for d in stat.duplicate_calls] == ["Bash"]
    assert stat.duplicate_calls[0].count == S5_MIN_REPEATS
    sig = analyze_s5([stat], RATES)
    assert sig.count == 1
    assert sig.rows[0][3] == str(S5_MIN_REPEATS)


def test_s5_subagent_duplicates_below_threshold_ignored(tmp_path: Path) -> None:
    """サブエージェント側でも閾値未満の繰り返しは検出しない（陰性）。"""
    session = tmp_path / "session.jsonl"
    write_subagent_with_calls(
        session,
        "a1",
        subagent_calls("Bash", {"command": "ls"}, S5_MIN_REPEATS - 1),
    )
    stat = parse_with_subagents(
        tmp_path, [agent_call("u1", "2026-07-21T01:00:00.000Z", "a1")]
    )
    assert stat.duplicate_calls == []


def test_s5_subagent_duplicates_not_double_counted(tmp_path: Path) -> None:
    """複数のサブエージェントの重複は独立に数え、uuid 既出分は数えない。

    `seen_uuids` は親と共有されているため、同じレコードが 2 度読まれても
    回数は増えない。
    """
    session = tmp_path / "session.jsonl"
    write_subagent_with_calls(
        session,
        "a1",
        subagent_calls("Bash", {"command": "sleep 60"}, S5_MIN_REPEATS, prefix="a1"),
    )
    write_subagent_with_calls(
        session,
        "a2",
        subagent_calls("Bash", {"command": "sleep 90"}, S5_MIN_REPEATS, prefix="a2"),
    )
    stat = parse_with_subagents(
        tmp_path,
        [
            agent_call("u1", "2026-07-21T01:00:00.000Z", "a1"),
            agent_call("u2", "2026-07-21T01:00:01.000Z", "a2"),
        ],
    )
    assert len(stat.duplicate_calls) == 2
    assert {d.count for d in stat.duplicate_calls} == {S5_MIN_REPEATS}


def codex_function_call(
    call_id: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Codex の `function_call` レコード（`arguments` は JSON 文字列）。"""
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": name,
            "call_id": call_id,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def test_s5_detects_codex_duplicate_function_calls(tmp_path: Path) -> None:
    """Codex の `function_call` も同一引数の繰り返しを検出する（陽性）。

    実測（2026 年の 1,228 ファイル / 69,109 件）で `call_id` は 100% 存在し、
    複数ファイルに同じ `call_id` が現れるケースは 0 件なので、ファイル内の
    dedup だけで二重計上を避けられる。
    """
    stat = parse_codex(
        tmp_path,
        [
            codex_token_count(1_000),
            *[
                codex_function_call(f"call_{i}", "exec_command", {"cmd": "sleep 300"})
                for i in range(S5_MIN_REPEATS)
            ],
        ],
    )
    sig = analyze_s5([stat], CODEX_RATES)
    assert sig.count == 1
    assert sig.rows[0][1] == "exec_command"
    assert sig.rows[0][3] == str(S5_MIN_REPEATS)


def test_s5_codex_normalizes_argument_key_order(tmp_path: Path) -> None:
    """Codex の `arguments`（JSON 文字列）もキー順の違いを同一と扱う。"""
    stat = parse_codex(
        tmp_path,
        [
            codex_token_count(1_000),
            *[
                codex_function_call(
                    f"call_{i}",
                    "exec_command",
                    {"cmd": "ls", "workdir": "/tmp"}
                    if i % 2 == 0
                    else {"workdir": "/tmp", "cmd": "ls"},
                )
                for i in range(S5_MIN_REPEATS)
            ],
        ],
    )
    assert analyze_s5([stat], CODEX_RATES).count == 1


def test_s5_codex_dedups_same_call_id(tmp_path: Path) -> None:
    """同じ `call_id` の再記録は 1 回の呼び出しとして数える（陰性）。"""
    calls = [
        codex_function_call(f"call_{i}", "exec_command", {"cmd": "sleep 300"})
        for i in range(S5_MIN_REPEATS - 1)
    ]
    stat = parse_codex(tmp_path, [codex_token_count(1_000), *calls, *calls])
    assert analyze_s5([stat], CODEX_RATES).count == 0


def test_s5_description_states_codex_is_covered() -> None:
    """説明文が対象範囲を事実として述べている（誤った技術的根拠を印字しない）。

    以前は「Codex の `response_item` は fork の複製を判別する横断キーを
    持たない」と書かれていたが、`call_id` は 100% 存在し一意である。
    """
    desc = analyze_s5([], RATES).description
    assert "call_id" in desc
    assert "横断キーを持たず" not in desc
    assert "サブエージェント" in desc


# --- S4: 期間境界をまたぐセッションの除外 ----------------------------------
def test_s4_excludes_sessions_starting_before_period(tmp_path: Path) -> None:
    """開始が期間外のセッションは S4 の母集団から外す（陰性）。

    期間フィルタで切り詰められた `assistant_turns` / `usage.total` を
    「短く小規模な作業」と誤認するため（実データで 1 ターン / 39,703
    トークンに見えるセッションが全期間では 6 ターン / 217,843 だった）。
    """
    stat = model_session("crossing")
    stat.first_record_at = "2026-06-30T23:00:00.000Z"
    since = date(2026, 7, 1)
    assert analyze_s4([stat], S4_RATES, since, None).count == 0
    # 開始が期間内なら従来どおり検出される（フィルタが効いている証拠）。
    stat.first_record_at = "2026-07-02T01:00:00.000Z"
    assert analyze_s4([stat], S4_RATES, since, None).count == 1


def test_s4_period_filter_is_noop_without_bounds() -> None:
    """期間指定が無ければ開始日時による絞り込みは働かない。"""
    stat = model_session("nobounds")
    stat.first_record_at = "2026-01-01T00:00:00.000Z"
    assert analyze_s4([stat], S4_RATES).count == 1


def test_parse_claude_session_records_full_time_range(tmp_path: Path) -> None:
    """`first_record_at` は期間フィルタを無視した実際の開始時刻を持つ。"""
    path = tmp_path / "s.jsonl"
    path.write_text(
        "".join(
            json.dumps(r) + "\n"
            for r in [
                assistant("u1", "2026-06-30T23:00:00.000Z"),
                assistant("u2", "2026-07-02T01:00:00.000Z"),
            ]
        ),
        encoding="utf-8",
    )
    stat = parse_claude_session(
        path, ScanCounters(), set(), date(2026, 7, 1), date(2026, 7, 31)
    )
    assert stat is not None
    # 集計対象は期間内の 1 件だけだが、セッション自体は期間前から始まっている。
    assert stat.assistant_turns == 1
    assert stat.started_at.startswith("2026-07-02")
    assert stat.first_record_at.startswith("2026-06-30")


def test_s4_description_does_not_claim_manual_selection() -> None:
    """説明文が「ユーザーが直接操作した」と断定しない。

    実データで検出 48 件のうち 40 件が、設定で固定されたモデルによる
    セキュリティレビューの自動実行だった。
    """
    desc = analyze_s4([], S4_RATES).description
    assert "ユーザーが直接操作した" not in desc
    assert "自動化ワークフロー" in desc
