"""resolve_since の期間決定ロジックのテスト。

優先順位: --all（全期間）> --since 明示 > --days 逆算 > 既定 30 日。
"""

from datetime import date

from report import DEFAULT_SINCE_DAYS, resolve_since

TODAY = date(2026, 6, 26)


def test_default_is_recent_30_days() -> None:
    """何も指定しなければ既定日数前の日付を返す。"""
    assert resolve_since(None, None, show_all=False, today=TODAY) == "20260527"
    assert DEFAULT_SINCE_DAYS == 30


def test_all_returns_none() -> None:
    """--all は全期間（None）。他指定より優先される。"""
    assert resolve_since("20260101", 7, show_all=True, today=TODAY) is None


def test_since_takes_precedence_over_days() -> None:
    """--since 明示は --days より優先する。"""
    assert resolve_since("20260101", 7, show_all=False, today=TODAY) == "20260101"


def test_days_overrides_default() -> None:
    """--days 指定時はその日数で逆算する。"""
    assert resolve_since(None, 7, show_all=False, today=TODAY) == "20260619"


def test_days_zero_means_today() -> None:
    """--days 0 は今日（境界値）。"""
    assert resolve_since(None, 0, show_all=False, today=TODAY) == "20260626"
