WITH ordered_equity AS (
    SELECT e.id AS equity_point_id,
           e.date,
           e.equity,
           e.drawdown_pct AS stored_drawdown_pct,
           ROW_NUMBER() OVER (ORDER BY e.date, e.id) AS audit_sequence,
           LAG(e.equity) OVER (ORDER BY e.date, e.id) AS prior_equity
    FROM equity_curve AS e
    WHERE e.backtest_id = :run_id
),
running_peak AS (
    SELECT ordered_equity.*,
           MAX(equity) OVER (
               ORDER BY date, equity_point_id ROWS UNBOUNDED PRECEDING
           ) AS running_peak
    FROM ordered_equity
),
calculated AS (
    SELECT running_peak.*,
           CASE WHEN prior_equity IS NULL THEN NULL
                ELSE (equity - prior_equity) / prior_equity END AS point_return,
           (equity - running_peak) / running_peak AS derived_drawdown_pct
    FROM running_peak
)
SELECT equity_point_id,
       date,
       equity,
       stored_drawdown_pct,
       audit_sequence,
       prior_equity,
       point_return,
       running_peak,
       derived_drawdown_pct,
       ABS(stored_drawdown_pct - derived_drawdown_pct) AS drawdown_delta_abs,
       CASE WHEN ABS(stored_drawdown_pct - derived_drawdown_pct) > :tolerance THEN 1
            ELSE 0 END AS is_mismatch
FROM calculated
ORDER BY date, equity_point_id
