# finance-flows

Collects public market-flow data once a week (Saturday morning, India time) and sends a summary to Telegram.
Holds market data only. No personal portfolio data is stored here.

- `data/fii_dii_daily.csv`: NSE FII/DII net buying, one row per day the job ran
- `data/fpi_monthly.csv`: NSDL foreign portfolio investor net investment by month (Rs crore)
- `data/fpi_mtd_snapshots.csv`: month-to-date FPI equity flow at each weekly run (the weekly flow is the difference)
- `data/indicators.csv`: weekly closes and changes for dollar index, USD/INR, US 10y yield, Brent, Nifty, India VIX, S&P 500, INDA, EEM, SPY
- `data/run_log.csv`: what worked and what failed on each run
