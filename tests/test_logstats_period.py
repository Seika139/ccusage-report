"""期間フィルタのテスト。

Claude はファイル名が UUID で日付を含まないためレコードの timestamp で判定する。
Codex はパスに `sessions/YYYY/MM/DD/` を含むのでディレクトリ単位で枝刈りできる。
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

from logstats import (
    ScanCounters,
    find_codex_files,
    in_period,
    parse_claude_session,
    parse_day_arg,
)

SINCE = date(2026, 7, 10)
UNTIL = date(2026, 7, 20)


def assistant_on(day: str, uuid: str) -> dict[str, Any]:
    """指定日の assistant レコードを組み立てる。"""
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": f"{day}T12:00:00.000Z",
        "message": {
            "id": f"msg_{uuid}",
            "model": "claude-opus-5",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def write_session(path: Path, days: tuple[str, ...]) -> Path:
    """各日 1 レコードのセッションログを書き出す。"""
    path.write_text(
        "".join(
            json.dumps(assistant_on(day, f"u{i}")) + "\n" for i, day in enumerate(days)
        ),
        encoding="utf-8",
    )
    return path


def test_parse_day_arg_matches_report_format() -> None:
    """report.py と同じ YYYYMMDD 文字列を date にする。None は無制限。"""
    assert parse_day_arg("20260710") == SINCE
    assert parse_day_arg(None) is None
    assert parse_day_arg("") is None
    assert parse_day_arg("not-a-date") is None


def test_in_period_includes_boundaries() -> None:
    """since / until は境界を含む。"""
    assert in_period(SINCE, SINCE, UNTIL)
    assert in_period(UNTIL, SINCE, UNTIL)
    assert not in_period(date(2026, 7, 9), SINCE, UNTIL)
    assert not in_period(date(2026, 7, 21), SINCE, UNTIL)


def test_in_period_without_bounds_accepts_everything() -> None:
    """since=None / until=None は全期間を含む。"""
    assert in_period(date(2020, 1, 1), None, None)
    assert in_period(date(2030, 1, 1), None, None)


def test_records_outside_period_are_skipped(tmp_path: Path) -> None:
    """期間外のレコードは集計に入らない（ファイル内で混在していても）。"""
    path = write_session(
        tmp_path / "s.jsonl", ("2026-07-05", "2026-07-15", "2026-07-25")
    )
    stat = parse_claude_session(path, ScanCounters(), set(), SINCE, UNTIL)
    assert stat is not None
    assert stat.assistant_turns == 1
    assert stat.day == "2026-07-15"


def test_all_records_included_when_since_is_none(tmp_path: Path) -> None:
    """since=None は全期間を対象にする。"""
    path = write_session(
        tmp_path / "s.jsonl", ("2026-07-05", "2026-07-15", "2026-07-25")
    )
    stat = parse_claude_session(path, ScanCounters(), set(), None, None)
    assert stat is not None
    assert stat.assistant_turns == 3


def test_codex_directory_dates_are_pruned(tmp_path: Path) -> None:
    """Codex はパスの YYYY/MM/DD で枝刈りする（境界日は含む）。"""
    for day in ("05", "10", "15", "20", "25"):
        target = tmp_path / "2026" / "07" / day
        target.mkdir(parents=True)
        (target / f"rollout-2026-07-{day}T00-00-00-uuid.jsonl").write_text(
            "", encoding="utf-8"
        )

    found = find_codex_files(tmp_path, SINCE, UNTIL)
    assert sorted(p.parts[-2] for p in found) == ["10", "15", "20"]


def test_codex_all_files_found_without_bounds(tmp_path: Path) -> None:
    """期間指定なしなら日付ディレクトリを全て見る。"""
    for day in ("01", "28"):
        target = tmp_path / "2026" / "07" / day
        target.mkdir(parents=True)
        (target / f"rollout-2026-07-{day}T00-00-00-uuid.jsonl").write_text(
            "", encoding="utf-8"
        )
    assert len(find_codex_files(tmp_path, None, None)) == 2


def test_codex_missing_root_returns_empty(tmp_path: Path) -> None:
    """ディレクトリが無くても例外にせず空リストを返す。"""
    assert find_codex_files(tmp_path / "nope", SINCE, UNTIL) == []
