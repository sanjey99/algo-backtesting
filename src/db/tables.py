"""SQLAlchemy 2.0 ORM table definitions."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False, default=100_000.0)
    commission_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.001)
    slippage_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0005)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    trades: Mapped[list[TradeRecord]] = relationship(
        "TradeRecord", back_populates="backtest", cascade="all, delete-orphan"
    )
    equity_points: Mapped[list[EquityCurvePoint]] = relationship(
        "EquityCurvePoint", back_populates="backtest", cascade="all, delete-orphan"
    )
    metrics: Mapped[list[MetricRecord]] = relationship(
        "MetricRecord", back_populates="backtest", cascade="all, delete-orphan"
    )


class TradeRecord(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backtest_id: Mapped[str] = mapped_column(String, ForeignKey("backtest_runs.id"), nullable=False)
    entry_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    exit_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    backtest: Mapped[BacktestRun] = relationship("BacktestRun", back_populates="trades")


class EquityCurvePoint(Base):
    __tablename__ = "equity_curve"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backtest_id: Mapped[str] = mapped_column(String, ForeignKey("backtest_runs.id"), nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    backtest: Mapped[BacktestRun] = relationship("BacktestRun", back_populates="equity_points")


class MetricRecord(Base):
    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backtest_id: Mapped[str] = mapped_column(String, ForeignKey("backtest_runs.id"), nullable=False)
    metric_name: Mapped[str] = mapped_column(String, nullable=False)
    metric_value: Mapped[float] = mapped_column(Float, nullable=False)

    backtest: Mapped[BacktestRun] = relationship("BacktestRun", back_populates="metrics")
