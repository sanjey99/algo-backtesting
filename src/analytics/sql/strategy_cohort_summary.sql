WITH selected_runs AS (
    SELECT r.id,
           r.strategy_name,
           r.symbol,
           r.start_date,
           r.end_date,
           r.initial_capital,
           r.commission_pct,
           r.slippage_pct
    FROM backtest_runs AS r
    WHERE (:symbol IS NULL OR r.symbol = :symbol)
      AND (:start_date IS NULL OR r.start_date >= :start_date)
      AND (:end_date IS NULL OR r.end_date <= :end_date)
),
metric_pivot AS (
    SELECT m.backtest_id,
           MAX(CASE WHEN m.metric_name = 'sharpe_ratio' THEN m.metric_value END) AS sharpe_ratio,
           MAX(CASE WHEN m.metric_name = 'max_drawdown' THEN m.metric_value END) AS max_drawdown
    FROM metrics AS m
    JOIN selected_runs AS r ON r.id = m.backtest_id
    GROUP BY m.backtest_id
),
closed_trade_counts AS (
    SELECT t.backtest_id,
           COUNT(*) AS closed_trade_count
    FROM trades AS t
    JOIN selected_runs AS r ON r.id = t.backtest_id
    WHERE t.exit_date IS NOT NULL
      AND t.exit_price IS NOT NULL
      AND t.pnl IS NOT NULL
    GROUP BY t.backtest_id
),
ranked_equity AS (
    SELECT e.backtest_id,
           e.equity,
           ROW_NUMBER() OVER (
               PARTITION BY e.backtest_id ORDER BY e.date DESC, e.id DESC
           ) AS latest_rank
    FROM equity_curve AS e
    JOIN selected_runs AS r ON r.id = e.backtest_id
),
run_facts AS (
    SELECT r.strategy_name,
           r.symbol,
           r.start_date,
           r.end_date,
           r.initial_capital,
           r.commission_pct,
           r.slippage_pct,
           CASE WHEN e.equity IS NULL THEN NULL
                ELSE (e.equity - r.initial_capital) / r.initial_capital END AS derived_return,
           m.sharpe_ratio,
           m.max_drawdown,
           COALESCE(t.closed_trade_count, 0) AS closed_trade_count
    FROM selected_runs AS r
    LEFT JOIN metric_pivot AS m ON m.backtest_id = r.id
    LEFT JOIN closed_trade_counts AS t ON t.backtest_id = r.id
    LEFT JOIN ranked_equity AS e ON e.backtest_id = r.id AND e.latest_rank = 1
),
cohort_facts AS (
    SELECT strategy_name,
           symbol,
           start_date,
           end_date,
           initial_capital,
           commission_pct,
           slippage_pct,
           COUNT(*) AS run_count,
           AVG(derived_return) AS average_derived_return,
           AVG(sharpe_ratio) AS average_sharpe_ratio,
           MIN(max_drawdown) AS worst_drawdown,
           SUM(closed_trade_count) AS aggregate_closed_trade_count
    FROM run_facts
    GROUP BY strategy_name,
             symbol,
             start_date,
             end_date,
             initial_capital,
             commission_pct,
             slippage_pct
    HAVING COUNT(*) >= :minimum_run_count
),
ranked_cohorts AS (
    SELECT cohort_facts.*,
           DENSE_RANK() OVER (
               PARTITION BY symbol,
                            start_date,
                            end_date,
                            initial_capital,
                            commission_pct,
                            slippage_pct
               ORDER BY average_derived_return DESC
           ) AS return_rank
    FROM cohort_facts
)
SELECT strategy_name,
       symbol,
       start_date,
       end_date,
       initial_capital,
       commission_pct,
       slippage_pct,
       run_count,
       average_derived_return,
       average_sharpe_ratio,
       worst_drawdown,
       aggregate_closed_trade_count,
       return_rank
FROM ranked_cohorts
ORDER BY symbol,
         start_date,
         end_date,
         initial_capital,
         commission_pct,
         slippage_pct,
         return_rank,
         strategy_name
