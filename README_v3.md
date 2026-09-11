# Financial Crime Analytics: Transaction Monitoring & Network Detection

![SQL](https://img.shields.io/badge/SQL-MySQL-4479A1?logo=mysql&logoColor=white)
![Python](https://img.shields.io/badge/Python-NetworkX%20%7C%20pandas-3776AB?logo=python&logoColor=white)
![Tableau](https://img.shields.io/badge/Dashboard-Tableau-E97627?logo=tableau&logoColor=white)
![Status](https://img.shields.io/badge/Status-Complete-brightgreen)

A rule-based and network-graph anti-money-laundering (AML) detection pipeline built on a synthetic banking transaction dataset, with a validation layer that measures detection performance against ground-truth labels.

**TL;DR:** Standard SQL-based AML rules (structuring, layering, velocity) performed close to random chance (2–3% precision) on this dataset. Graph-based fan-in detection — accounts collecting funds from many distinct senders — hit 94% precision on a smaller catch rate, leading to a tiered-triage recommendation for how a real alert queue could combine both. See [Key Findings](#key-findings) below.

## Table of Contents
- [Business Context](#business-context)
- [Dataset](#dataset)
- [Architecture](#architecture)
- [Repository Structure](#repository-structure)
- [Methodology](#methodology)
- [Key Findings](#key-findings)
- [Concluding Insight: A Tiered Triage Model](#concluding-insight-a-tiered-triage-model)
- [Limitations](#limitations)
- [Tools Used](#tools-used)
- [Dashboard](#dashboard)

## Business Context

Financial institutions are required to monitor transactions for money laundering typologies (structuring, layering, rapid fund movement) and file Suspicious Activity Reports (SARs) where warranted. Rule-based transaction monitoring systems are the industry standard because they are explainable and auditable — a regulator can trace exactly why an account was flagged. However, rule-based systems alone tend to miss more sophisticated laundering structures (e.g., multi-hop layering chains, mule networks), which is where network/graph analysis adds value.

This project simulates that two-layer approach — as would be built by a compliance analytics function — and then quantifies how well each layer actually performs, which is a step most portfolio projects skip.

## Dataset

**IBM Transactions for Anti Money Laundering (AML)** — HI-Small subset (~5M synthetic transactions), sourced from Kaggle (`ealtman2019/ibm-transactions-for-anti-money-laundering-aml`). Each transaction includes sender/receiver account and bank identifiers, timestamp, amount, currency, payment format, and a ground-truth `is_laundering` label. Roughly 0.1–0.3% of transactions are labeled laundering, consistent with real-world alert base rates.

**Note on scope:** this is synthetic data used because real transaction data is confidential and access-restricted for obvious regulatory reasons. Detection logic and thresholds here are illustrative of methodology, not calibrated for production use.

## Architecture

```
Raw CSV (Kaggle)
      │
      ▼
MySQL: raw_transactions table
      │
      ├──► SQL rule-based detection ──► account_risk_score table
      │        (structuring, layering, velocity — aml_risk_score_flags.sql)
      │        key column: account
      │
      ├──► Python: aml_network_analysis.py
      │        (cycles, fan-in/out, rapid pass-through, centrality,
      │         fan-in threshold sweep)
      │        → rapid_movement_flags.csv (column: account)
      │        → centrality_rankings.csv (columns: account, betweenness_centrality)
      │        → fan_in_flags.csv (column: account) — fan-in was the
      │          standout typology (~114x baseline precision), so it
      │          gets its own export rather than being folded in
      │        → fan_out_hub.png, fan_in_collector.png,
      │          high_centrality_account.png, laundering_cycle_example.png
      │
      └──► Python: aml_validation.py
               (precision / recall / F1 / FPR against is_laundering,
                reads account_risk_score + all three CSVs above;
                reports fan-in alone AND a broad OR ensemble separately
                to avoid diluting fan-in's precision)
               → detection_validation_summary.csv
               → precision_recall_curve.png
```

## Repository Structure

```
fincrime-analytics/
├── README.md
├── sql/
│   └── aml_risk_score_flags.sql   # schema-agnostic: builds structuring,
│                                   layering, and velocity flags, joins them
│                                   into account_risk_score (single script)
├── python/
│   ├── aml_network_analysis.py    # graph construction + typology detection
│   │                               (cycles, fan-in/out, rapid pass-through,
│   │                                betweenness centrality, fan-in threshold
│   │                                sweep); exports the three CSVs consumed
│   │                                by aml_validation.py
│   └── aml_validation.py          # precision/recall/F1/FPR validation,
│                                   dual ensemble comparison (broad OR vs.
│                                   fan-in-led), PR-curve sweep
├── outputs/
│   ├── fan_out_hub.png
│   ├── fan_in_collector.png
│   ├── high_centrality_account.png
│   ├── laundering_cycle_example.png
│   ├── precision_recall_curve.png
│   ├── rapid_movement_flags.csv
│   ├── centrality_rankings.csv
│   ├── fan_in_flags.csv
│   ├── account_scorecard.csv       # one row per account: risk_score +
│   │                                 every flag + ground truth, joined -
│   │                                 built for direct use in Tableau/BI
│   │                                 tools without recreating flag logic
│   └── detection_validation_summary.csv
├── dashboard/
│   ├── fincrime_dashboard.twbx    # Tableau packaged workbook
│   ├── dashboard_overview.png
│   ├── precision_recall_chart.png
│   └── tiered_triage_funnel.png
└── report/
    └── findings_summary.pdf
```

**Run order:** `aml_risk_score_flags.sql` (MySQL) → `aml_network_analysis.py` (produces all three CSVs + PNGs) → `aml_validation.py` (consumes `account_risk_score` plus all three CSVs, produces the summary and PR curve).

## Methodology

### 1. Rule-based detection (SQL)
Three typologies detected via window functions and aggregations against `raw_transactions`:
- **Structuring** — multiple sub-threshold transactions from one account within a single day.
- **Layering** — funds moved out of an account within **1 hour** of arriving, **and** in a roughly matching amount (within ±15%), using `LEAD()` window functions. (Originally a 6-hour, any-amount window; tightened after an initial run showed it over-flagging ~40% of all accounts — most "in then out within 6h" activity turned out to be ordinary pass-through accounts, not laundering.)
- **Velocity anomalies** — transactions exceeding an account's own historical mean by 3 standard deviations.

Each flag is weighted and summed into a composite `risk_score` per account.

### 2. Network/graph detection (Python, NetworkX)
Flagged accounts (`risk_score >= RISK_SCORE_THRESHOLD` in `account_risk_score`) are pulled into a directed transaction graph to detect structures the SQL layer alone can't see:
- **Cycles** — funds looping back through a chain of accounts (classic layering signature), via `nx.simple_cycles`.
- **Fan-in / fan-out** — accounts receiving from or sending to an unusually high number of counterparties (mule collection / smurfing distribution).
- **Rapid pass-through** — a graph-side cross-check of the SQL layering flag, using the same "fast AND amount-matched" principle: for each account, whether an incoming transaction is followed by an outgoing transaction within 1 hour **and** in a roughly matching amount (within ±15%). Computed independently from the SQL layer (in Python, off the flagged-account subset) so the two can be compared in validation. Exported to `rapid_movement_flags.csv`.
- **Betweenness centrality** — accounts sitting on an unusually high number of fund-flow paths, catching layering hubs that degree-based thresholds miss. The full ranking (not just top hubs) is exported to `centrality_rankings.csv` so validation can compute percentile-based cutoffs on the complete distribution.

Only the top-ranked hub, collector, high-centrality account, and one example cycle are plotted (PNG) to keep visualizations readable — the full graph runs into hundreds of thousands of nodes.

### 3. Validation
All methods are scored against the dataset's ground-truth `is_laundering` label at the account level:
- Ground truth is built at the account level: an account is a true positive if it appears as sender or receiver on at least one transaction labeled `is_laundering = 1`.
- Precision, recall, F1, and false-positive rate computed per method (rule-based at multiple thresholds, rapid pass-through, high centrality, fan-in) and for **two** ensemble variants — see the dilution note below for why there are two rather than one.
- A precision-recall curve sweeps the rule-based risk-score threshold to show the alert-volume-vs-catch-rate tradeoff — the central operational tension in real AML programs.
- A separate fan-in threshold sweep (`sweep_fan_in_thresholds()`) tests whether tightening the in-degree cutoff trades flagged-account volume for even higher precision.

## Key Findings

**No single method wins outright — each sits at a different point on the precision/recall tradeoff.** Measured with `aml_validation.py`'s account-level evaluation (the correct way to score a classifier — precision/recall computed per *account*, not per transaction):

| Method | Precision | Recall | F1 | Alert Volume | TP | FP |
|---|---|---|---|---|---|---|
| Rule-based (threshold ≥ 3) | 1.83% | 57.68% | 0.036 | 199,918 | 3,667 | 196,251 |
| Rule-based (threshold ≥ 5) | 2.89% | 16.85% | 0.049 | 37,017 | 1,071 | 35,946 |
| Rule-based (threshold ≥ 7) | 12.88% | 0.33% | 0.006 | 163 | 21 | 142 |
| Graph: rapid pass-through | 2.87% | 16.61% | 0.049 | 36,777 | 1,056 | 35,721 |
| Graph: high betweenness centrality | 2.50% | 56.16% | 0.048 | 142,753 | 3,570 | 139,183 |
| **Graph: fan-in (collection point)** | **94.29%** | 0.52% | 0.010 | **35** | **33** | **2** |
| Ensemble (rule OR rapid OR centrality OR fan-in) | 2.50% | 56.16% | 0.048 | 142,771 | 3,570 | 139,201 |
| Fan-in-led (fan-in alone, not diluted by OR) | 94.29% | 0.52% | 0.010 | 35 | 33 | 2 |

**Interpretation:**
- **Fan-in is an almost perfect signal when it fires — 33 of 35 flagged accounts (94.29%) were genuine launderers** — but it only catches 0.52% of all launderers in the dataset. It's precise but narrow: excellent at *confirming* a case, poor at *finding* most cases on its own.
- **Rule-based thresholds and centrality sit at the opposite extreme**: broad recall (56–58% of all launderers caught) at the cost of enormous noise — roughly 55–58 false alarms for every real hit. This is a realistic reflection of how real-world AML alerting is widely known to run (industry false-positive rates above 90% are common).
- **Combining methods via a simple OR barely helps.** The ensemble's numbers are nearly identical to centrality alone, because centrality's broad net already covers most of what the other methods would add — folding in fan-in's 35 extremely precise alerts makes no visible dent in a pool of 142,753 flagged accounts. This confirms the earlier transaction-level check: naively OR-ing a narrow, high-precision method into a broad, noisy one dilutes the narrow method's value rather than improving the ensemble. **Lesson for ensemble design: a strong, narrow signal should be evaluated and used on its own, not folded into a broader OR-based alert set.**
- Note on methodology: this account-level precision (fan-in: 94.29%) differs sharply from an earlier transaction-level check on the same fan-in accounts (11.43%), because a small number of flagged accounts with many ordinary transactions dragged the transaction-level number down. Account-level evaluation is the standard, correct way to score this kind of classifier — the transaction-level check is left in `aml_network_analysis.py` as a secondary diagnostic, not the headline metric.

## Concluding Insight: A Tiered Triage Model

The four methods tested don't compete for "best detector" — they're suited to different roles in an alert-handling pipeline, which is exactly how real compliance teams are structured:

1. **Broad first-pass screen** — rule-based thresholds or centrality, tuned for high recall (56–58% catch rate here). This is the wide net: expensive in analyst hours, but ensures most laundering activity enters the queue somewhere.
2. **High-confidence escalation lane** — fan-in, run as a separate, parallel check rather than folded into the broad screen. When fan-in fires, treat it as a near-certain case (94.29% precision) and prioritize it for immediate investigation, ahead of the broad screen's queue.
3. **Analyst capacity allocation** — since the broad screen alone generates ~140,000+ alerts against roughly 6,000 true positives, no real team could review all of them manually. A practical design routes fan-in hits to senior investigators same-day, while broad-screen alerts get lower-touch automated pre-filtering (e.g., additional secondary checks, dollar-amount thresholds) before human review.

This mirrors how production AML systems are actually built: **no single rule or model is expected to "solve" detection alone.** The value is in layering methods with different precision/recall profiles and routing alerts accordingly — a tiered design is both more defensible to a regulator and a better use of finite analyst time than treating every alert as equally urgent.

## Limitations

- Synthetic data does not capture real customer behavior, seasonal patterns, or evolving typologies.
- Thresholds (structuring cutoff, layering window/amount-match tolerance, fan-in/out degree) were tuned iteratively against this specific dataset and would require re-calibration against a labeled sample and business risk appetite in any other setting — they are not universal AML thresholds.
- The strength of the fan-in signal may be specific to how this dataset's synthetic typologies were constructed; it should not be assumed to generalize to real transaction data without re-validation.
- No entity resolution / KYC data is joined in, so beneficial-ownership-based typologies (shell company networks) are out of scope here.
- This is a portfolio demonstration of methodology, not a production AML system — real deployments (e.g., NICE Actimize, SAS AML) incorporate case management, regulatory reporting workflows, and model governance not represented here.

## Tools Used

MySQL Workbench (SQL, window functions, schema design) · Python (pandas, NetworkX, scikit-learn, matplotlib) · Tableau Desktop (dashboarding) · Kaggle (data source)

## Dashboard

Built in Tableau Desktop, connected live to MySQL (`account_risk_score`) with the network-analysis and validation outputs (`account_scorecard.csv`, `detection_validation_summary.csv`) joined in as supplementary sources.

**Sheets:**
- Precision vs. recall by detection method
- Alert volume by method (log scale)
- Risk score distribution across all flagged accounts
- Tiered triage funnel: total accounts → broad screen → fan-in escalation → confirmed laundering
- Top flagged accounts table, highlighting fan-in hits

**Screenshots:** 

```
outputs/
├── dashboard_overview.png 
├── precision_recall_chart.png
└── tiered_triage_funnel.png
```

To embed in this README once added:
```markdown
![Dashboard Overview](outputs/dashboard_overview.png)
```

If publishing publicly, consider also publishing the workbook itself to [Tableau Public](https://public.tableau.com) and linking it here — this lets reviewers interact with the dashboard directly rather than viewing static images. Note: Tableau Public requires file-based extracts rather than a live MySQL connection, so you'd need to export the relevant tables/queries as CSVs before publishing.

## Regulatory Framing

Typologies detected here map to FATF (Financial Action Task Force) guidance on money laundering methods, and the structuring logic reflects the kind of sub-threshold pattern relevant to Suspicious Activity Report (SAR) filing triggers under frameworks such as the U.S. Bank Secrecy Act and equivalent EU/UK AML directives.

## Author

Emeka — Data Analyst, Financial Control & Strategic Planning, with 10+ years of banking experience in AML/KYC compliance and financial reporting, transitioning into technical data analytics roles.

[LinkedIn] · [GitHub]
