SELECT 'INVALID_RUN_DATE_RANGE' AS defect_code,
       'backtest_runs' AS table_name,
       r.id AS record_id,
       r.id AS run_id
FROM backtest_runs AS r
WHERE r.start_date > r.end_date AND (:scope_all = 1 OR r.id IN :run_ids)
UNION ALL
SELECT 'NONPOSITIVE_INITIAL_CAPITAL', 'backtest_runs', r.id, r.id
FROM backtest_runs AS r
WHERE r.initial_capital <= 0 AND (:scope_all = 1 OR r.id IN :run_ids)
UNION ALL
SELECT 'NEGATIVE_COMMISSION_PCT', 'backtest_runs', r.id, r.id
FROM backtest_runs AS r
WHERE r.commission_pct < 0 AND (:scope_all = 1 OR r.id IN :run_ids)
UNION ALL
SELECT 'NEGATIVE_SLIPPAGE_PCT', 'backtest_runs', r.id, r.id
FROM backtest_runs AS r
WHERE r.slippage_pct < 0 AND (:scope_all = 1 OR r.id IN :run_ids)
UNION ALL
SELECT 'NONPOSITIVE_TRADE_QUANTITY', 'trades', CAST(t.id AS TEXT), t.backtest_id
FROM trades AS t
WHERE t.quantity <= 0 AND (:scope_all = 1 OR t.backtest_id IN :run_ids)
UNION ALL
SELECT 'INCONSISTENT_TRADE_CLOSE_FIELDS', 'trades', CAST(t.id AS TEXT), t.backtest_id
FROM trades AS t
WHERE (
    (
        t.exit_date IS NULL
        AND (t.exit_price IS NOT NULL OR t.pnl IS NOT NULL OR t.pnl_pct IS NOT NULL)
    )
    OR (
        t.exit_date IS NOT NULL
        AND (t.exit_price IS NULL OR t.pnl IS NULL OR t.pnl_pct IS NULL)
    )
)
AND (:scope_all = 1 OR t.backtest_id IN :run_ids)
ORDER BY defect_code, run_id, record_id
