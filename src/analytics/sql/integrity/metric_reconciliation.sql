WITH metric_pivot AS (
    SELECT m.backtest_id AS run_id,
           MAX(CASE WHEN m.metric_name = 'total_return' THEN m.metric_value END)
               AS stored_total_return
    FROM metrics AS m
    GROUP BY m.backtest_id
),
ranked_equity AS (
    SELECT e.id,
           e.backtest_id AS run_id,
           e.date,
           e.equity,
           e.drawdown_pct,
           ROW_NUMBER() OVER (
               PARTITION BY e.backtest_id ORDER BY e.date DESC, e.id DESC
           ) AS latest_sequence,
           MAX(e.equity) OVER (
               PARTITION BY e.backtest_id
               ORDER BY e.date, e.id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
           ) AS running_peak
    FROM equity_curve AS e
),
final_equity AS (
    SELECT run_id, equity
    FROM ranked_equity
    WHERE latest_sequence = 1
),
return_mismatches AS (
    SELECT r.id AS run_id
    FROM backtest_runs AS r
    JOIN metric_pivot AS m ON m.run_id = r.id
    JOIN final_equity AS f ON f.run_id = r.id
    WHERE r.initial_capital > 0
      AND m.stored_total_return IS NOT NULL
      AND ABS(m.stored_total_return - ((f.equity / r.initial_capital) - 1.0)) > :tolerance
      AND (:scope_all = 1 OR r.id IN :run_ids)
),
drawdown_mismatches AS (
    SELECT e.id, e.run_id
    FROM ranked_equity AS e
    WHERE e.running_peak > 0
      AND ABS(e.drawdown_pct - ((e.equity / e.running_peak) - 1.0)) > :tolerance
      AND (:scope_all = 1 OR e.run_id IN :run_ids)
)
SELECT 'TOTAL_RETURN_MISMATCH' AS defect_code,
       'metrics' AS table_name,
       r.run_id AS record_id,
       r.run_id AS run_id
FROM return_mismatches AS r
UNION ALL
SELECT 'DRAWDOWN_MISMATCH', 'equity_curve', CAST(d.id AS TEXT), d.run_id
FROM drawdown_mismatches AS d
ORDER BY defect_code, record_id
