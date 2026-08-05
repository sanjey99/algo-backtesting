SELECT m.backtest_id AS run_id,
       m.metric_name AS duplicate_key,
       COUNT(*) AS duplicate_count
FROM metrics AS m
WHERE :scope_all = 1 OR m.backtest_id IN :run_ids
GROUP BY m.backtest_id, m.metric_name
HAVING COUNT(*) > 1
ORDER BY m.backtest_id, m.metric_name
