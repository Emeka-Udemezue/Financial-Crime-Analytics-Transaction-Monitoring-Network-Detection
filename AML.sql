-- CREATE STATEMENT
CREATE DATABASE fincrime_analytics;
USE fincrime_analytics;

CREATE TABLE raw_transactions (
    txn_id INT AUTO_INCREMENT PRIMARY KEY,
    txn_timestamp DATETIME,
    from_bank VARCHAR(20),
    from_account VARCHAR(30),
    to_bank VARCHAR(20),
    to_account VARCHAR(30),
    amount_received DECIMAL(18,2),
    receiving_currency VARCHAR(10),
    amount_paid DECIMAL(18,2),
    payment_currency VARCHAR(10),
    payment_format VARCHAR(30),
    is_laundering TINYINT,
    INDEX idx_from_acct (from_account, txn_timestamp),
    INDEX idx_to_acct (to_account, txn_timestamp)
);

-- LOADING DATA FROM .CSV FILE INTO TABLE
LOAD DATA LOCAL INFILE '/Users/emekaudemezue/Downloads/Personal/Learning/Data Analytics/Portfolio Project/AML/HI-Small_Trans.csv'
INTO TABLE raw_transactions
FIELDS TERMINATED BY ',' ENCLOSED BY '"'
LINES TERMINATED BY '\n'
IGNORE 1 ROWS
(txn_timestamp, from_bank, from_account, to_bank, to_account,
 amount_received, receiving_currency, amount_paid, payment_currency,
 payment_format, is_laundering);
 
select * from `raw_transactions`
limit 100;

SELECT from_account, DATE(txn_timestamp) AS txn_date,
       COUNT(*) AS txn_count, SUM(amount_paid) AS daily_total
FROM raw_transactions
WHERE amount_paid BETWEEN 8000 AND 9999   -- just under a 10k reporting threshold
GROUP BY from_account, DATE(txn_timestamp)
HAVING COUNT(*) >= 3;

WITH acct_flow AS (
  SELECT to_account AS account, txn_timestamp AS in_time, amount_received AS amt_in
  FROM raw_transactions
  UNION ALL
  SELECT from_account, txn_timestamp, -amount_paid
  FROM raw_transactions
)
SELECT account,
       in_time,
       amt_in,
       LEAD(in_time) OVER (PARTITION BY account ORDER BY in_time) AS next_move,
       TIMESTAMPDIFF(HOUR, in_time, LEAD(in_time) OVER (PARTITION BY account ORDER BY in_time)) AS hrs_between
FROM acct_flow
HAVING hrs_between <= 6;   -- funds moved out within 6 hrs of 


-- =====================================================================
-- Financial Crime Analytics Portfolio Project
-- Full Flag Join: Structuring + Layering + Velocity -> Account Risk Score
-- Database: MySQL 8.0+ (uses window functions, CTEs)
-- =====================================================================

USE fincrime_analytics;

-- ---------------------------------------------------------------------
-- STEP 1: Structuring flag
-- Accounts sending 3+ transactions in a single day, each just under a
-- reporting threshold (here: $8,000 - $9,999, adjust to your dataset)
-- ---------------------------------------------------------------------
DROP TEMPORARY TABLE IF EXISTS flag_structuring;
CREATE TEMPORARY TABLE flag_structuring AS
SELECT
    from_account,
    DATE(txn_timestamp)  AS txn_date,
    COUNT(*)             AS structuring_txn_count,
    SUM(amount_paid)     AS structuring_daily_total
FROM raw_transactions
WHERE amount_paid BETWEEN 8000 AND 9999
GROUP BY from_account, DATE(txn_timestamp)
HAVING COUNT(*) >= 3;

CREATE INDEX idx_struct_acct ON flag_structuring (from_account);

-- ---------------------------------------------------------------------
-- STEP 2: Layering flag
-- Funds arriving into an account and leaving again within a tight window,
-- in roughly the same amount (the actual layering signature - fast AND
-- amount-matched pass-through, not just "money moved eventually").
-- Built from a unified in/out ledger per account
--
-- Tuning:
--   LAYERING_WINDOW_HOURS = 1     (was 6 - too loose, flagged ~40% of accounts)
--   AMOUNT_MATCH_TOLERANCE = 15%  (outgoing amount within +/-15% of incoming)
-- ---------------------------------------------------------------------
DROP TEMPORARY TABLE IF EXISTS acct_flow;
CREATE TEMPORARY TABLE acct_flow AS
SELECT
    to_account          AS account,
    txn_timestamp        AS move_time,
    amount_received      AS amount,
    'IN'                 AS direction
FROM raw_transactions
UNION ALL
SELECT
    from_account         AS account,
    txn_timestamp        AS move_time,
    amount_paid          AS amount,
    'OUT'                AS direction
FROM raw_transactions;

CREATE INDEX idx_flow_acct_time ON acct_flow (account, move_time);

DROP TEMPORARY TABLE IF EXISTS flag_layering;
CREATE TEMPORARY TABLE flag_layering AS
WITH ranked_flow AS (
    SELECT
        account,
        move_time,
        direction,
        amount,
        LEAD(move_time)  OVER (PARTITION BY account ORDER BY move_time) AS next_move_time,
        LEAD(direction)  OVER (PARTITION BY account ORDER BY move_time) AS next_direction,
        LEAD(amount)     OVER (PARTITION BY account ORDER BY move_time) AS next_amount
    FROM acct_flow
)
SELECT
    account,
    COUNT(*) AS layering_event_count
FROM ranked_flow
WHERE direction = 'IN'
  AND next_direction = 'OUT'
  AND TIMESTAMPDIFF(HOUR, move_time, next_move_time) <= 1
  AND next_amount BETWEEN amount * 0.85 AND amount * 1.15
GROUP BY account;

CREATE INDEX idx_layer_acct ON flag_layering (account);

-- ---------------------------------------------------------------------
-- STEP 3: Velocity / statistical anomaly flag
-- Transactions that exceed the account's own mean + 3 standard deviations
-- ---------------------------------------------------------------------
DROP TEMPORARY TABLE IF EXISTS flag_velocity;
CREATE TEMPORARY TABLE flag_velocity AS
WITH acct_stats AS (
    SELECT
        from_account,
        txn_timestamp,
        amount_paid,
        AVG(amount_paid)    OVER (PARTITION BY from_account) AS avg_amt,
        STDDEV(amount_paid) OVER (PARTITION BY from_account) AS std_amt
    FROM raw_transactions
)
SELECT
    from_account,
    COUNT(*) AS velocity_event_count
FROM acct_stats
WHERE amount_paid > avg_amt + (3 * std_amt)
  AND std_amt > 0
GROUP BY from_account;

CREATE INDEX idx_velo_acct ON flag_velocity (from_account);

-- ---------------------------------------------------------------------
-- STEP 4: Distinct account universe (anything that appeared as a sender)
-- ---------------------------------------------------------------------
DROP TEMPORARY TABLE IF EXISTS all_accounts;
CREATE TEMPORARY TABLE all_accounts AS
SELECT DISTINCT from_account AS account FROM raw_transactions;

-- ---------------------------------------------------------------------
-- STEP 5: Full join -> weighted risk score
-- Weights are illustrative; document your rationale in the README
--   Structuring : 3   (deliberate threshold evasion - high intent signal)
--   Layering    : 4   (fast, amount-matched in/out movement within 1h -
--                       strongest laundering signal; tightened from an
--                       earlier 6h/any-amount version that over-flagged
--                       ~40% of accounts)
--   Velocity    : 2   (statistical outlier - could be legitimate)
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS account_risk_score;
CREATE TABLE account_risk_score AS
SELECT
    a.account,
    COALESCE(s.structuring_txn_count, 0)   AS structuring_txn_count,
    COALESCE(l.layering_event_count, 0)    AS layering_event_count,
    COALESCE(v.velocity_event_count, 0)    AS velocity_event_count,
    (CASE WHEN s.from_account IS NOT NULL THEN 1 ELSE 0 END) AS structuring_flag,
    (CASE WHEN l.account      IS NOT NULL THEN 1 ELSE 0 END) AS layering_flag,
    (CASE WHEN v.from_account IS NOT NULL THEN 1 ELSE 0 END) AS velocity_flag,
    (
        (CASE WHEN s.from_account IS NOT NULL THEN 1 ELSE 0 END) * 3
      + (CASE WHEN l.account      IS NOT NULL THEN 1 ELSE 0 END) * 4
      + (CASE WHEN v.from_account IS NOT NULL THEN 1 ELSE 0 END) * 2
    ) AS risk_score
FROM all_accounts a
LEFT JOIN (
    SELECT from_account, SUM(structuring_txn_count) AS structuring_txn_count
    FROM flag_structuring
    GROUP BY from_account
) s ON a.account = s.from_account
LEFT JOIN flag_layering l ON a.account = l.account
LEFT JOIN flag_velocity v ON a.account = v.from_account;

CREATE INDEX idx_risk_score ON account_risk_score (risk_score DESC);

-- ---------------------------------------------------------------------
-- STEP 6: Quick validation views
-- ---------------------------------------------------------------------

-- Top 50 highest-risk accounts
SELECT * FROM account_risk_score
ORDER BY risk_score DESC
LIMIT 50;

-- Distribution of risk scores (sanity check against a real alert queue)
SELECT risk_score, COUNT(*) AS num_accounts
FROM account_risk_score
GROUP BY risk_score
ORDER BY risk_score DESC;

-- Cross-check flagged accounts against the dataset's own labeled ground truth
SELECT
    ars.risk_score,
    COUNT(DISTINCT rt.from_account) AS accounts_in_bucket,
    SUM(rt.is_laundering)           AS labeled_laundering_txns
FROM account_risk_score ars
JOIN raw_transactions rt ON ars.account = rt.from_account
GROUP BY ars.risk_score
ORDER BY ars.risk_score DESC;


SELECT COUNT(*) AS flagged_txn_count
FROM raw_transactions rt
JOIN account_risk_score ars ON rt.from_account = ars.account
WHERE ars.risk_score >= 4;





