---
title: "Algorithmic Trading Backtesting Platform"
date: "2026-03-21"
tags:
  - project
  - project/algorithmic-trading-backtesting
status: planning
domain: quantitative-finance
target: Goldman Sachs, JP Morgan, Morgan Stanley — Quant Dev / Tech Analyst roles
---

# Algorithmic Trading Backtesting Platform

## Goal

Build a production-quality backtesting engine for testing trading strategies (moving average crossover, RSI-based, etc.) against real historical market data, with a polished risk analytics dashboard showing P&L, Sharpe ratio, CAGR, max drawdown, and trade history.

Demonstrates: financial domain knowledge, data pipeline work, API integration, statistical validation, and clean architecture — hitting every box for bank tech and quant roles.

## Key Skills Showcased

- Event-driven and vectorized backtesting architecture (OOP)
- Walk-Forward validation and Permutation Testing (anti-overfitting)
- Sharpe, CAGR, Max Drawdown, Profit Factor risk metrics
- Historical market data via Yahoo Finance / Alpha Vantage
- FastAPI backend, React/Streamlit frontend
- PostgreSQL/SQLite for trade history persistence

## Differentiators vs. Tutorial Projects

- Permutation testing to detect overfitting (not just backtest results)
- Walk-Forward analysis with periodic re-optimization
- Bar-by-bar returns (more stable objective functions than trade-by-trade)
- Position sizing module (Kelly Criterion / fixed fractional)

## Stretch Goals

- Deploy on AWS/GCP
- User authentication + multi-strategy comparison
- WebSocket paper trading in simulated real-time

## Research

- [[research/algorithmic-trading-backtesting/algorithmic-trading-backtesting_research_2026-03-21|YT Research: Algo Trading Backtesting]]
- [[research/algorithmic-trading-backtesting/algorithmic-trading-backtesting_report_2026-03-21|NotebookLM Briefing Report]]

## Status

- [x] Research complete
- [ ] Blueprint
- [ ] System design
- [ ] Implementation

## Resources

- [[projects/_index|Projects Index]]
