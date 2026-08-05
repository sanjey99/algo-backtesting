SELECT e.backtest_id AS run_id,
       CAST(e.date AS TEXT) AS duplicate_key,
       COUNT(*) AS duplicate_count
FROM equity_curve AS e
WHERE :scope_all = 1 OR e.backtest_id IN :run_ids
GROUP BY e.backtest_id, e.date
HAVING COUNT(*) > 1
ORDER BY e.backtest_id, e.date
