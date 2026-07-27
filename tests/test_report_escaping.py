"""HTML 出力のエスケープのテスト。

生成物は `file://` オリジンで開かれるうえ、埋め込む文言はログ由来
（tool 名・プロジェクトのパス・シグナルの説明文）である。S7 の説明文には
`<synthetic>` という文言が含まれ、エスケープを忘れるとタグとして解釈されて
ブラウザ上で表示が消える（実測で確認済み）。
"""

from logstats import LogStats, SignalResult
from report import render_waste_sections


def signal_with(description: str) -> LogStats:
    """指定した説明文を持つシグナル 1 件だけの LogStats を作る。"""
    return LogStats(
        signals=[
            SignalResult(
                key="S7",
                title="失敗・中断で捨てたトークン",
                confidence="medium",
                count=1,
                tokens=100,
                usd=1.0,
                description=description,
                columns=[("日付", False)],
                rows=[["2026-07-21"]],
            )
        ]
    )


def test_description_is_html_escaped() -> None:
    """説明文の `<synthetic>` はタグにならずそのまま表示される。"""
    html_out = render_waste_sections(signal_with("`<synthetic>` は課金されない"))
    assert "&lt;synthetic&gt;" in html_out
    assert "<synthetic>" not in html_out


def test_title_and_confidence_labels_are_escaped() -> None:
    """タイトル・確度ラベルも同じ経路でエスケープする。"""
    stats = signal_with("説明")
    stats.signals[0].title = "<b>危険</b>"
    stats.signals[0].confidence = "<i>low</i>"
    html_out = render_waste_sections(stats)
    assert "<b>危険</b>" not in html_out
    assert "&lt;b&gt;危険&lt;/b&gt;" in html_out
    assert "&lt;i&gt;low&lt;/i&gt;" in html_out
