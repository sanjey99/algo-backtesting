"""Tests for standalone analytics report generation."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from src.analytics import report as report_module
from src.analytics.report import (
    _build_equity_chart_html,
    generate_html_report,
    generate_report,
)
from src.engine.backtest import BacktestResult
from src.models.portfolio import EquityPoint


def _result(
    *,
    strategy_name: str = "ma_crossover",
    symbol: str = "SPY",
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> BacktestResult:
    start = start_date or datetime(2024, 1, 2, tzinfo=UTC)
    end = end_date or datetime(2024, 1, 3, tzinfo=UTC)
    return BacktestResult(
        strategy_name=strategy_name,
        symbol=symbol,
        start_date=start,
        end_date=end,
        parameters={"fast_period": 5, "slow_period": 20},
        trades=[],
        equity_curve=[
            EquityPoint(date=start, equity=100_000.0),
            EquityPoint(date=end, equity=101_000.0),
        ],
        final_equity=101_000.0,
        initial_capital=100_000.0,
    )


def test_html_report_escapes_metadata_at_the_template_boundary() -> None:
    payloads = {
        "strategy": '</title><script id="strategy-xss">alert(1)</script>',
        "symbol": '<img src=x onerror="alert(2)">',
        "start": '<svg onload="alert(3)">',
        "end": '</p><script id="end-xss">alert(4)</script>',
    }
    result = _result(
        strategy_name=payloads["strategy"],
        symbol=payloads["symbol"],
        start_date=cast(datetime, payloads["start"]),
        end_date=cast(datetime, payloads["end"]),
    )

    report = generate_html_report(result, {"total_return": 0.01})

    assert all(payload not in report for payload in payloads.values())
    assert "&lt;/title&gt;&lt;script id=&quot;strategy-xss&quot;&gt;" in report
    assert "&lt;img src=x onerror=&quot;alert(2)&quot;&gt;" in report
    assert "&lt;svg onload=&quot;alert(3)&quot;&gt;" in report


def test_html_report_escapes_custom_metric_labels_and_entities() -> None:
    metric_name = "<img src=x onerror=&#97;&#108;&#101;&#114;&#116;(1)>"

    report = generate_html_report(_result(), {metric_name: 1.0})

    assert "<img" not in report.casefold()
    assert "&lt;Img Src=X Onerror=&amp;#97;" in report


def test_plotly_chart_json_cannot_break_out_of_its_script_element() -> None:
    payload = '</script><script id="chart-xss">alert(1)</script>'

    chart = _build_equity_chart_html([{"date": payload, "equity": 100.0}])

    assert payload not in chart
    assert "\\u003c\\u002fscript\\u003e" in chart


def test_html_report_accepts_a_deterministic_chart_div_id() -> None:
    report = generate_html_report(_result(), chart_div_id="cloud-run-123e4567")

    assert 'id="cloud-run-123e4567"' in report


def test_chart_has_clear_empty_and_missing_dependency_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _build_equity_chart_html([]) == "<p>No equity data.</p>"

    monkeypatch.setattr(report_module, "PLOTLY_AVAILABLE", False)
    assert _build_equity_chart_html([]) == (
        "<p>Plotly not available — install plotly to see the equity chart.</p>"
    )


def test_html_and_dict_reports_include_expected_summary_data() -> None:
    result = _result()

    html_report = generate_html_report(result, {"total_return": 0.125, "sharpe_ratio": 1.25})
    summary = generate_report(result)

    assert "<h1>Backtest Report</h1>" in html_report
    assert "Total Return</td><td>12.50%" in html_report
    assert "Plotly.newPlot" in html_report
    assert summary["strategy_name"] == "ma_crossover"
    assert summary["symbol"] == "SPY"
    assert summary["final_equity"] == 101_000.0
    assert summary["total_trades"] == 0


def test_html_report_computes_default_metrics() -> None:
    report = generate_html_report(_result())

    assert "Sharpe Ratio" in report
    assert "Total Return" in report
