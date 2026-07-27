"""ログのパース堅牢性のテスト。

実測で 90,517 行中 3 行の破損を確認している。レポート生成を絶対に落とさないため、
破損行・型の揺れ・timestamp を持たないレコード型のいずれも例外にしてはならない。
"""

import json
from pathlib import Path
from typing import Any

from logstats import (
    S1_MIN_CHARS,
    ScanCounters,
    measure_codex_output,
    measure_content_size,
    parse_claude_session,
    parse_codex_session,
)

# `timestamp` も `message` も持たないレコード型。先に type で弾く必要がある。
METADATA_RECORDS: tuple[dict[str, Any], ...] = (
    {"type": "mode", "mode": "acceptEdits"},
    {"type": "last-prompt", "prompt": "..."},
    {"type": "file-history-snapshot", "snapshot": {}},
)

VALID_ASSISTANT: dict[str, Any] = {
    "type": "assistant",
    "uuid": "ok1",
    "timestamp": "2026-07-21T02:20:22.318Z",
    "message": {
        "id": "msg_ok",
        "model": "claude-opus-5",
        "usage": {"input_tokens": 7, "output_tokens": 11},
    },
}


def write_lines(path: Path, lines: tuple[str, ...]) -> Path:
    """生の行列を書き出す（破損行を含められるよう文字列で受け取る）。"""
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def test_broken_lines_are_counted_not_raised(tmp_path: Path) -> None:
    """破損 JSON 行は例外を投げず、件数として記録される。"""
    counters = ScanCounters()
    path = write_lines(
        tmp_path / "s.jsonl",
        (
            json.dumps(VALID_ASSISTANT),
            '{"type": "assistant", "uuid": "broken"',
            "not json at all",
            "[1, 2, 3]",
        ),
    )
    stat = parse_claude_session(path, counters, set())
    assert stat is not None
    assert stat.usage.output == 11
    assert counters.broken_lines == 3


def test_metadata_records_without_timestamp_are_safe(tmp_path: Path) -> None:
    """timestamp も message も持たないレコード型で例外を起こさない。"""
    path = write_lines(
        tmp_path / "s.jsonl",
        (*[json.dumps(r) for r in METADATA_RECORDS], json.dumps(VALID_ASSISTANT)),
    )
    stat = parse_claude_session(path, ScanCounters(), set())
    assert stat is not None
    assert stat.assistant_turns == 1


def test_tool_use_result_as_string_is_guarded(tmp_path: Path) -> None:
    """`toolUseResult` が文字列（エラー時）でも安全に扱える。"""
    record: dict[str, Any] = {
        "type": "user",
        "uuid": "tu1",
        "timestamp": "2026-07-21T02:20:24.318Z",
        "toolUseResult": "Error: command failed",
        "message": {"role": "user", "content": "plain string content"},
    }
    path = write_lines(tmp_path / "s.jsonl", (json.dumps(record),))
    stat = parse_claude_session(path, ScanCounters(), set())
    assert stat is not None
    assert stat.big_outputs == []


def test_broken_numeric_fields_do_not_lose_whole_file(tmp_path: Path) -> None:
    """数値フィールドの型が壊れたレコードは 0 扱いで隔離し、他の集計を守る。

    ここで例外を投げると `collect_sessions` のファイル単位 try/except まで伝播し、
    同じファイル内の正常なレコードの集計まで丸ごと失われる。
    """
    broken_usage: dict[str, Any] = {
        "type": "assistant",
        "uuid": "bad1",
        "timestamp": "2026-07-21T02:20:20.000Z",
        "message": {
            "id": "msg_bad",
            "model": "claude-opus-5",
            "usage": {"input_tokens": [], "output_tokens": "abc"},
        },
    }
    broken_compact: dict[str, Any] = {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": "bad2",
        "timestamp": "2026-07-21T02:20:21.000Z",
        "compactMetadata": {
            "trigger": "auto",
            "preTokens": "abc",
            "postTokens": {"nested": 1},
        },
    }
    path = write_lines(
        tmp_path / "s.jsonl",
        (
            json.dumps(broken_usage),
            json.dumps(broken_compact),
            json.dumps(VALID_ASSISTANT),
        ),
    )
    counters = ScanCounters()
    stat = parse_claude_session(path, counters, set())

    assert stat is not None
    assert counters.skipped_files == 0
    # 壊れたレコードは 0 として数え、正常なレコードの集計は保全される。
    assert stat.usage.input == 7
    assert stat.usage.output == 11
    assert len(stat.compactions) == 1
    assert stat.compactions[0].dropped == 0


def test_broken_codex_numeric_fields_are_isolated(tmp_path: Path) -> None:
    """Codex 側の usage も壊れた値で例外にせず、他のレコードを保全する。"""

    def token_count(uuid: str, ts: str, last: Any, total: Any) -> dict[str, Any]:
        return {
            "type": "event_msg",
            "uuid": uuid,
            "timestamp": ts,
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": last, "total_token_usage": total},
            },
        }

    path = write_lines(
        tmp_path / "rollout-x.jsonl",
        (
            json.dumps(
                token_count(
                    "c1",
                    "2026-07-21T01:00:00.000Z",
                    {"input_tokens": []},
                    {"input_tokens": "abc", "output_tokens": None},
                )
            ),
            json.dumps(
                token_count(
                    "c2",
                    "2026-07-21T01:01:00.000Z",
                    {"input_tokens": 500},
                    {"input_tokens": 1_200, "output_tokens": 34},
                )
            ),
        ),
    )
    counters = ScanCounters()
    stat = parse_codex_session(path, counters, set())

    assert stat is not None
    assert counters.skipped_files == 0
    assert stat.usage.input == 1_200
    assert stat.usage.output == 34


def test_measure_content_size_handles_str() -> None:
    """`tool_result.content` が str の場合は文字数そのもの。"""
    assert measure_content_size("x" * 1234) == 1234


def test_measure_content_size_handles_list() -> None:
    """list の場合は text 本文長 + image の base64 長を合計する。"""
    content: list[dict[str, Any]] = [
        {"type": "text", "text": "a" * 100},
        {"type": "image", "source": {"type": "base64", "data": "b" * 50}},
    ]
    assert measure_content_size(content) == 150


def test_measure_content_size_handles_unknown_block() -> None:
    """未知の型は JSON 文字列化した長さで測り、例外にしない。"""
    assert measure_content_size([{"type": "mystery", "payload": [1, 2]}]) > 0
    assert measure_content_size(None) == 0


def test_codex_output_prefers_original_token_count() -> None:
    """現行形式の `Original token count: N` をトークン数として優先する。"""
    body = "z" * (S1_MIN_CHARS * 4)
    output = (
        f"Chunk ID: abc\nWall time: 0.1\nOriginal token count: 1234\nOutput:\n{body}"
    )
    chars, tokens = measure_codex_output(output)
    assert chars == len(output)
    assert tokens == 1234


def test_codex_output_handles_json_string_form() -> None:
    """古い JSON 文字列形式 `{"output": ..., "metadata": ...}` の本文長を使う。"""
    payload = json.dumps({"output": "y" * 300, "metadata": {"exit_code": 0}})
    chars, tokens = measure_codex_output(payload)
    assert chars == 300
    assert tokens is None


def test_codex_output_handles_list_and_none() -> None:
    """list（view_image 等）と None で例外を投げない。"""
    chars, tokens = measure_codex_output([{"type": "text", "text": "q" * 42}])
    assert chars == 42
    assert tokens is None
    assert measure_codex_output(None) == (0, None)


def test_codex_output_falls_back_to_raw_length() -> None:
    """どの形式にも当たらなければ生の文字列長を使う。"""
    chars, tokens = measure_codex_output("Exit code: 0\nOutput:\n" + "w" * 10)
    assert chars == len("Exit code: 0\nOutput:\n") + 10
    assert tokens is None
