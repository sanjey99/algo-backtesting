SELECT 'backtest_runs' AS table_name, COUNT(*) AS row_count FROM backtest_runs
UNION ALL
SELECT 'trades', COUNT(*) FROM trades
UNION ALL
SELECT 'equity_curve', COUNT(*) FROM equity_curve
UNION ALL
SELECT 'metrics', COUNT(*) FROM metrics
ORDER BY table_name
