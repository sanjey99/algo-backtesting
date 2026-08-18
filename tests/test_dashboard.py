"""Offline startup smoke coverage for the Streamlit dashboard."""
from __future__ import annotations

from streamlit.testing.v1 import AppTest


def test_dashboard_starts_offline_with_expected_controls() -> None:
    app = AppTest.from_file("src/dashboard/app.py", default_timeout=10).run()

    assert not app.exception
    assert [title.value for title in app.title] == ["📈 Algorithmic Backtester"]
    assert app.selectbox[0].value == "ma_crossover"
    assert app.selectbox[0].options == [
        "Ma Crossover",
        "Rsi Mean Reversion",
        "Breakout",
    ]
    assert app.text_input[0].value == "SPY"
    assert [button.label for button in app.button] == ["▶ Run Backtest"]
    assert [message.value for message in app.info] == [
        "Configure and click **▶ Run Backtest** to get started."
    ]
