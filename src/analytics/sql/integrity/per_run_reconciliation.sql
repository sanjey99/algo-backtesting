WITH trade_counts AS (
    SELECT t.backtest_id AS run_id,
           COUNT(*) AS trade_count,
           SUM(CASE WHEN t.exit_date IS NOT NULL THEN 1 ELSE 0 END) AS closed_trade_count,
           SUM(CASE WHEN t.exit_date IS NULL THEN 1 ELSE 0 END) AS open_trade_count
    FROM trades AS t
    GROUP BY t.backtest_id
),
equity_counts AS (
    SELECT e.backtest_id AS run_id, COUNT(*) AS equity_count
    FROM equity_curve AS e
    GROUP BY e.backtest_id
),
metric_counts AS (
    SELECT m.backtest_id AS run_id,
           COUNT(*) AS metric_count,
           COUNT(DISTINCT m.metric_name) AS distinct_metric_count,
           COUNT(DISTINCT CASE WHEN m.metric_name IN (
               'sharpe_ratio', 'sortino_ratio', 'cagr', 'max_drawdown',
               'max_drawdown_duration', 'win_rate', 'profit_factor',
               'calmar_ratio', 'total_trades', 'total_return'
           ) THEN m.metric_name END) AS optional_metric_count
    FROM metrics AS m
    GROUP BY m.backtest_id
)
SELECT r.id AS run_id,
       COALESCE(t.trade_count, 0) AS trade_count,
       COALESCE(t.closed_trade_count, 0) AS closed_trade_count,
       COALESCE(t.open_trade_count, 0) AS open_trade_count,
       COALESCE(e.equity_count, 0) AS equity_count,
       COALESCE(m.metric_count, 0) AS metric_count,
       COALESCE(m.distinct_metric_count, 0) AS distinct_metric_count,
       COALESCE(m.optional_metric_count, 0) AS optional_metric_count
FROM backtest_runs AS r
LEFT JOIN trade_counts AS t ON t.run_id = r.id
LEFT JOIN equity_counts AS e ON e.run_id = r.id
LEFT JOIN metric_counts AS m ON m.run_id = r.id
WHERE :scope_all = 1 OR r.id IN :run_ids
ORDER BY r.id
