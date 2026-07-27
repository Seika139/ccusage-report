"""単価逆算（build_rate_table）のテスト。

価格表をハードコードせず、コストを重み付きトークン数で割って $/M input を逆算する。
重みは `input + 5*output + 0.1*cache_read + 1.25*cache_create`。
実測では Opus 5.000 / Sonnet 2.000 / Haiku 1.000 に収束する。
"""

from logstats import (
    RATE_CACHE_CREATE_MULT,
    RATE_CACHE_READ_MULT,
    RATE_OUTPUT_MULT,
    build_rate_table,
)
from report import ModelAgg, Report

# Opus 相当（$5.00/M input）の使用量から逆算されるべきコストを作る。
OPUS_USAGE = ModelAgg(
    input=1_000_000,
    output=200_000,
    cache_read=10_000_000,
    cache_create=400_000,
)
OPUS_BASE = 5.0


def weighted(agg: ModelAgg) -> float:
    """逆算に使う重み付きトークン数（テスト側で独立に検算する）。"""
    return (
        agg.input
        + RATE_OUTPUT_MULT * agg.output
        + RATE_CACHE_READ_MULT * agg.cache_read
        + RATE_CACHE_CREATE_MULT * agg.cache_create
    )


def make_report(models: dict[str, ModelAgg]) -> Report:
    """ModelAgg の辞書から Report を組み立てる（依存を引数で注入する）。"""
    rep = Report()
    for name, agg in models.items():
        rep.by_model[name] = agg
    return rep


def test_rate_is_reverse_engineered_from_cost() -> None:
    """コストと重み付きトークン数から基準単価を正しく逆算する。"""
    agg = ModelAgg(
        input=OPUS_USAGE.input,
        output=OPUS_USAGE.output,
        cache_read=OPUS_USAGE.cache_read,
        cache_create=OPUS_USAGE.cache_create,
        cost=weighted(OPUS_USAGE) * OPUS_BASE / 1_000_000,
    )
    rates = build_rate_table(make_report({"claude-opus-5": agg}))
    assert abs(rates.base("claude-opus-5") - OPUS_BASE) < 1e-9


def test_output_is_five_times_input() -> None:
    """output のみのモデルは基準単価の 5 倍で課金されている前提で逆算される。"""
    agg = ModelAgg(output=1_000_000, cost=RATE_OUTPUT_MULT * OPUS_BASE)
    rates = build_rate_table(make_report({"claude-opus-5": agg}))
    assert abs(rates.base("claude-opus-5") - OPUS_BASE) < 1e-9


def test_date_suffix_is_normalized() -> None:
    """ccusage の日付付きモデル名は生ログ側の名前と突き合わせられる。"""
    agg = ModelAgg(input=1_000_000, cost=1.0)
    rates = build_rate_table(make_report({"claude-haiku-4-5-20251001": agg}))
    assert abs(rates.base("claude-haiku-4-5") - 1.0) < 1e-9


def test_zero_cost_model_does_not_raise() -> None:
    """コストゼロ・トークンゼロのモデルでゼロ除算しない。"""
    rates = build_rate_table(
        make_report({"free-model": ModelAgg(), "zero-cost": ModelAgg(input=100)})
    )
    assert rates.base("free-model") == 0.0
    assert rates.base("zero-cost") == 0.0


def test_unknown_model_falls_back_to_period_average() -> None:
    """未知モデルは KeyError ではなく期間全体の加重平均単価になる。"""
    cheap = ModelAgg(input=1_000_000, cost=1.0)
    pricey = ModelAgg(input=1_000_000, cost=5.0)
    rates = build_rate_table(make_report({"cheap": cheap, "pricey": pricey}))
    expected = 6.0 / (weighted(cheap) + weighted(pricey)) * 1_000_000
    assert abs(rates.base("never-seen-model") - expected) < 1e-9
    assert abs(rates.fallback - expected) < 1e-9


def test_fallback_is_zero_when_report_is_empty() -> None:
    """空 Report でもゼロ除算せず 0 を返す。"""
    rates = build_rate_table(Report())
    assert rates.fallback == 0.0
    assert rates.base("anything") == 0.0
