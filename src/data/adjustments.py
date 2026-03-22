"""Corporate-action adjustment utilities for OHLCV DataFrames.

Functions
---------
adjust_for_splits      — scale prices / volume for a stock split.
adjust_for_dividends   — apply backward dividend adjustment to prices.
validate_adjustment    — flag suspiciously large single-day price gaps.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

# ---------------------------------------------------------------------------
# Split adjustment
# ---------------------------------------------------------------------------


def adjust_for_splits(df: pd.DataFrame, split_factor: float) -> pd.DataFrame:
    """Apply a stock-split adjustment to an OHLCV DataFrame.

    For a 2-for-1 split pass ``split_factor=2``.  Prices are divided by
    *split_factor* and volume is multiplied by *split_factor* so that the
    total market capitalisation is preserved.

    Parameters
    ----------
    df:
        DataFrame with at least columns ``open, high, low, close, adj_close``
        and ``volume``.
    split_factor:
        The ratio new_shares / old_shares.  Must be positive.

    Returns
    -------
    pd.DataFrame
        A new DataFrame with adjusted values; the original is not mutated.

    Raises
    ------
    ValueError
        If *split_factor* is not positive.
    """
    if split_factor <= 0:
        raise ValueError(f"split_factor must be positive, got {split_factor}")

    df = df.copy()
    price_cols = [c for c in ("open", "high", "low", "close", "adj_close") if c in df.columns]
    for col in price_cols:
        df[col] = df[col] / split_factor

    if "volume" in df.columns:
        df["volume"] = df["volume"] * split_factor

    return df


# ---------------------------------------------------------------------------
# Dividend adjustment
# ---------------------------------------------------------------------------


def adjust_for_dividends(
    df: pd.DataFrame,
    dividend: float,
    ex_date: datetime,
) -> pd.DataFrame:
    """Apply a backward dividend adjustment to prices before the ex-dividend date.

    The standard backward-adjustment formula scales all price columns for
    rows *strictly before* ``ex_date`` by the factor::

        factor = (close_prior_day - dividend) / close_prior_day

    Because the prior-day close is not always available, this implementation
    uses the ``close`` value on the ex-date row itself as the reference price
    (i.e. the first available close on or after ``ex_date``).  If no such row
    exists, all pre-ex-date rows are adjusted using the first row's close.

    Parameters
    ----------
    df:
        DataFrame with a :class:`pandas.DatetimeIndex` and at least
        ``open, high, low, close`` columns.
    dividend:
        Dividend amount per share in the same currency as the prices.
    ex_date:
        The ex-dividend date.  Rows with index **strictly before** this date
        are adjusted.

    Returns
    -------
    pd.DataFrame
        A new DataFrame with adjusted price columns; the original is not mutated.

    Raises
    ------
    ValueError
        If *dividend* is negative.
    """
    if dividend < 0:
        raise ValueError(f"dividend must be non-negative, got {dividend}")

    df = df.copy()
    ex_date_ts = pd.Timestamp(ex_date)

    # Determine reference close price — use the ex-date close if available,
    # otherwise fall back to the first close in the series.
    on_or_after = df.loc[df.index >= ex_date_ts]
    if not on_or_after.empty and "close" in df.columns:
        ref_close: float = float(on_or_after["close"].iloc[0])
    elif "close" in df.columns and not df.empty:
        ref_close = float(df["close"].iloc[0])
    else:
        # Nothing to do — no close column or empty frame
        return df

    if ref_close == 0:
        return df  # avoid division by zero

    factor = (ref_close - dividend) / ref_close

    mask = df.index < ex_date_ts
    price_cols = [c for c in ("open", "high", "low", "close", "adj_close") if c in df.columns]
    for col in price_cols:
        df.loc[mask, col] = df.loc[mask, col] * factor

    return df


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_adjustment(
    df: pd.DataFrame,
    threshold: float = 0.20,
) -> list[str]:
    """Return warning strings for single-day close price gaps above *threshold*.

    A "gap" is the absolute percentage change between consecutive close
    prices::

        abs((close[i] - close[i-1]) / close[i-1])

    Any gap exceeding *threshold* produces a warning string of the form::

        "Large price gap on {date}: {pct:.1%} change"

    Parameters
    ----------
    df:
        DataFrame with a ``close`` column and a :class:`pandas.DatetimeIndex`.
    threshold:
        Fractional threshold (default ``0.20`` == 20 %).  Gaps **strictly
        greater** than this value are flagged.

    Returns
    -------
    list[str]
        Possibly empty list of human-readable warning strings.
    """
    warnings: list[str] = []

    if "close" not in df.columns or len(df) < 2:
        return warnings

    close = df["close"].astype(float)
    pct_changes = close.pct_change().dropna()

    for date, pct in pct_changes.items():
        if abs(pct) > threshold:
            warnings.append(
                f"Large price gap on {date}: {abs(pct):.1%} change"
            )

    return warnings
