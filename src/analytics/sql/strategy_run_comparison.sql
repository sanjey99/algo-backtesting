WITH selected_runs AS (
    SELECT r.id, r.strategy_name, r.symbol, r.start_date, r.end_date,
           r.initial_capital, r.commission_pct, r.slippage_pct
    FROM backtest_runs AS r
    WHERE r.symbol = :symbol
      AND datetime(r.start_date) = datetime(:start_date)
      AND datetime(r.end_date) = datetime(:end_date)
      AND (:strategy_name IS NULL OR r.strategy_name = :strategy_name)
),
metric_pivot AS (
    SELECT m.backtest_id,
           MAX(CASE WHEN m.metric_name = 'sharpe_ratio' THEN m.metric_value END) AS sharpe_ratio,
           MAX(CASE WHEN m.metric_name = 'sortino_ratio' THEN m.metric_value END) AS sortino_ratio,
           MAX(CASE WHEN m.metric_name = 'cagr' THEN m.metric_value END) AS cagr,
           MAX(CASE WHEN m.metric_name = 'max_drawdown' THEN m.metric_value END) AS max_drawdown,
           MAX(CASE WHEN m.metric_name = 'max_drawdown_duration' THEN m.metric_value END) AS max_drawdown_duration,
           MAX(CASE WHEN m.metric_name = 'win_rate' THEN m.metric_value END) AS win_rate,
           MAX(CASE WHEN m.metric_name = 'profit_factor' THEN m.metric_value END) AS profit_factor,
           MAX(CASE WHEN m.metric_name = 'calmar_ratio' THEN m.metric_value END) AS calmar_ratio,
           MAX(CASE WHEN m.metric_name = 'total_trades' THEN m.metric_value END) AS metric_total_trades,
           MAX(CASE WHEN m.metric_name = 'total_return' THEN m.metric_value END) AS reported_total_return
    FROM metrics AS m
    JOIN selected_runs AS r ON r.id = m.backtest_id
    GROUP BY m.backtest_id
),
trade_stats AS (
    SELECT t.backtest_id,
           COUNT(CASE WHEN t.exit_date IS NOT NULL AND t.exit_price IS NOT NULL AND t.pnl IS NOT NULL THEN 1 END) AS closed_trade_count,
           COALESCE(SUM(CASE WHEN t.exit_date IS NOT NULL AND t.exit_price IS NOT NULL AND t.pnl IS NOT NULL THEN t.pnl ELSE 0 END), 0.0) AS cumulative_trade_pnl,
           COALESCE(SUM(CASE WHEN t.exit_date IS NOT NULL AND t.exit_price IS NOT NULL AND t.pnl IS NOT NULL THEN t.commission ELSE 0 END), 0.0) AS closed_trade_commission
    FROM trades AS t
    JOIN selected_runs AS r ON r.id = t.backtest_id
    GROUP BY t.backtest_id
),
ranked_equity AS (
    SELECT e.*,
           ROW_NUMBER() OVER (PARTITION BY e.backtest_id ORDER BY e.date DESC, e.id DESC) AS rn
    FROM equity_curve AS e
    JOIN selected_runs AS r ON r.id = e.backtest_id
),
run_facts AS (
    SELECT r.id AS run_id, r.strategy_name, r.symbol, r.start_date, r.end_date,
           r.initial_capital, r.commission_pct, r.slippage_pct,
           m.sharpe_ratio, m.sortino_ratio, m.cagr, m.max_drawdown,
           m.max_drawdown_duration, m.win_rate, m.profit_factor, m.calmar_ratio,
           m.metric_total_trades, m.reported_total_return,
           COALESCE(t.closed_trade_count, 0) AS closed_trade_count,
           COALESCE(t.cumulative_trade_pnl, 0.0) AS cumulative_trade_pnl,
           COALESCE(t.closed_trade_commission, 0.0) AS closed_trade_commission,
           e.equity AS latest_equity,
           CASE WHEN e.equity IS NULL THEN NULL
                ELSE (e.equity - r.initial_capital) / r.initial_capital END AS derived_total_return,
           CASE WHEN e.equity IS NULL OR m.reported_total_return IS NULL THEN NULL
                ELSE ((e.equity - r.initial_capital) / r.initial_capital) - m.reported_total_return END AS total_return_delta
    FROM selected_runs AS r
    LEFT JOIN metric_pivot AS m ON m.backtest_id = r.id
    LEFT JOIN trade_stats AS t ON t.backtest_id = r.id
    LEFT JOIN ranked_equity AS e ON e.backtest_id = r.id AND e.rn = 1
)
SELECT run_id, strategy_name, symbol, start_date, end_date, initial_capital,
       commission_pct, slippage_pct, sharpe_ratio, sortino_ratio, cagr,
       max_drawdown, max_drawdown_duration, win_rate, profit_factor, calmar_ratio,
       metric_total_trades, reported_total_return, closed_trade_count,
       cumulative_trade_pnl, closed_trade_commission, latest_equity,
       derived_total_return, total_return_delta,
       CASE WHEN derived_total_return IS NULL THEN NULL ELSE RANK() OVER (
           PARTITION BY symbol, start_date, end_date, initial_capital, commission_pct, slippage_pct
           ORDER BY derived_total_return DESC
       ) END AS return_rank,
       CASE WHEN sharpe_ratio IS NULL THEN NULL ELSE RANK() OVER (
           PARTITION BY symbol, start_date, end_date, initial_capital, commission_pct, slippage_pct
           ORDER BY sharpe_ratio DESC
       ) END AS sharpe_rank
FROM run_facts
ORDER BY return_rank, strategy_name, run_id
