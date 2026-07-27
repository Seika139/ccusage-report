"""Claude ログの 2 段階 dedup のテスト。

素朴な合計は最大 3 倍以上に膨らむ。原因は (1) 1 レスポンスが同じ `message.id` を
共有する複数レコードに分割記録される、(2) fork/resume が履歴を別ファイルへ複製する。
"""

import json
from pathlib import Path
from typing import Any

from logstats import ScanCounters, TokenUsage, parse_claude_session

# 分割記録された 1 レスポンス。message.id が同一で output_tokens だけが増えていく。
SPLIT_RECORDS: tuple[dict[str, Any], ...] = (
    {
        "type": "assistant",
        "uuid": "u1",
        "timestamp": "2026-07-21T02:20:22.318Z",
        "message": {
            "id": "msg_a",
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 100,
                "cache_read_input_tokens": 5_000,
                "cache_creation_input_tokens": 200,
            },
        },
    },
    {
        "type": "assistant",
        "uuid": "u2",
        "timestamp": "2026-07-21T02:20:23.318Z",
        "message": {
            "id": "msg_a",
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 450,
                "cache_read_input_tokens": 5_000,
                "cache_creation_input_tokens": 200,
            },
        },
    },
)


def write_jsonl(path: Path, records: tuple[dict[str, Any], ...]) -> Path:
    """レコード列を JSONL として書き出す（実ログは一切読まない）。"""
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )
    return path


def test_message_id_group_takes_max_not_sum(tmp_path: Path) -> None:
    """同一 message.id の分割レコードはフィールドごとの max に畳まれる。"""
    path = write_jsonl(tmp_path / "s.jsonl", SPLIT_RECORDS)
    stat = parse_claude_session(path, ScanCounters(), set())
    assert stat is not None
    assert stat.usage == TokenUsage(
        input=10, output=450, cache_read=5_000, cache_create=200
    )


def test_message_id_group_counts_as_one_turn(tmp_path: Path) -> None:
    """分割レコードは 1 ターンとして数える（増幅倍率の分母を膨らませない）。"""
    path = write_jsonl(tmp_path / "s.jsonl", SPLIT_RECORDS)
    stat = parse_claude_session(path, ScanCounters(), set())
    assert stat is not None
    assert stat.assistant_turns == 1


def test_duplicate_uuid_skipped_across_files(tmp_path: Path) -> None:
    """fork/resume で複製された uuid は 2 つ目のファイルで無視される。"""
    seen: set[str] = set()
    counters = ScanCounters()
    original = write_jsonl(tmp_path / "a.jsonl", SPLIT_RECORDS)
    forked = write_jsonl(tmp_path / "b.jsonl", SPLIT_RECORDS)

    first = parse_claude_session(original, counters, seen)
    second = parse_claude_session(forked, counters, seen)

    assert first is not None
    assert first.usage.output == 450
    # 複製ファイルには新規レコードが 1 件も無いので集計対象にならない。
    assert second is None or second.usage.total == 0


def test_synthetic_model_is_excluded(tmp_path: Path) -> None:
    """`<synthetic>` はクライアント側エラーで課金されないため集計から除く。"""
    records: tuple[dict[str, Any], ...] = (
        {
            "type": "assistant",
            "uuid": "s1",
            "timestamp": "2026-07-21T02:20:22.318Z",
            "isApiErrorMessage": True,
            "message": {
                "id": "msg_synth",
                "model": "<synthetic>",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
        *SPLIT_RECORDS,
    )
    path = write_jsonl(tmp_path / "s.jsonl", records)
    stat = parse_claude_session(path, ScanCounters(), set())
    assert stat is not None
    assert stat.model == "claude-opus-5"
    assert stat.assistant_turns == 1


def test_distinct_message_ids_are_summed(tmp_path: Path) -> None:
    """別の message.id は別レスポンスなので合算する（max で潰さない）。"""
    second = json.loads(json.dumps(SPLIT_RECORDS[1]))
    second["uuid"] = "u3"
    second["message"]["id"] = "msg_b"
    path = write_jsonl(tmp_path / "s.jsonl", (*SPLIT_RECORDS, second))
    stat = parse_claude_session(path, ScanCounters(), set())
    assert stat is not None
    assert stat.usage.output == 900
    assert stat.assistant_turns == 2
