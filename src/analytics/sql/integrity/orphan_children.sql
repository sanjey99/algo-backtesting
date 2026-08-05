SELECT 'ORPHAN_TRADES' AS defect_code,
       'trades' AS table_name,
       CAST(t.id AS TEXT) AS record_id,
       t.backtest_id AS run_id
FROM trades AS t
LEFT JOIN backtest_runs AS r ON r.id = t.backtest_id
WHERE r.id IS NULL AND (:scope_all = 1 OR t.backtest_id IN :run_ids)
UNION ALL
SELECT 'ORPHAN_EQUITY_POINTS', 'equity_curve', CAST(e.id AS TEXT), e.backtest_id
FROM equity_curve AS e
LEFT JOIN backtest_runs AS r ON r.id = e.backtest_id
WHERE r.id IS NULL AND (:scope_all = 1 OR e.backtest_id IN :run_ids)
UNION ALL
SELECT 'ORPHAN_METRICS', 'metrics', CAST(m.id AS TEXT), m.backtest_id
FROM metrics AS m
LEFT JOIN backtest_runs AS r ON r.id = m.backtest_id
WHERE r.id IS NULL AND (:scope_all = 1 OR m.backtest_id IN :run_ids)
ORDER BY defect_code, record_id
