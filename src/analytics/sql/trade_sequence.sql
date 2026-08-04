WITH closed_trades AS (
    SELECT t.id AS trade_id, t.exit_date, t.pnl
    FROM trades AS t
    WHERE t.backtest_id = :run_id
      AND t.exit_date IS NOT NULL
      AND t.exit_price IS NOT NULL
      AND t.pnl IS NOT NULL
)
SELECT trade_id,
       exit_date,
       pnl,
       ROW_NUMBER() OVER (ORDER BY exit_date, trade_id) AS trade_sequence,
       SUM(pnl) OVER (
           ORDER BY exit_date, trade_id ROWS UNBOUNDED PRECEDING
       ) AS cumulative_pnl,
       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) OVER (
           ORDER BY exit_date, trade_id ROWS UNBOUNDED PRECEDING
       ) AS cumulative_wins,
       1.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) OVER (
           ORDER BY exit_date, trade_id ROWS UNBOUNDED PRECEDING
       ) / ROW_NUMBER() OVER (ORDER BY exit_date, trade_id) AS cumulative_win_rate,
       AVG(pnl) OVER (
           ORDER BY exit_date, trade_id ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
       ) AS rolling_5_trade_avg_pnl
FROM closed_trades
ORDER BY exit_date, trade_id
