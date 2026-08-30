"""
Financial Crime Analytics — Detection Validation Module

Compares rule-based and graph-based detection methods against the
is_laundering label in the IBM AML dataset.

The analysis covers:
- Rule-based risk scores
- Rapid pass-through
- Betweenness centrality
- Fan-in
- Combined detection methods

For each method, the script calculates precision, recall, F1,
false positive rate, and the confusion matrix. It also generates
a precision-recall curve for the rule-based risk score.

Requirements:
    pip install mysql-connector-python pandas scikit-learn matplotlib
"""

import mysql.connector
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from sklearn.metrics import (
    precision_score, recall_score, f1_score,
    confusion_matrix, precision_recall_curve, auc
)

DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "Password123!",
    "database": "fincrime_analytics",
}

# Accounts above this percentile are flagged on centrality.
CENTRALITY_FLAG_PERCENTILE = 0.95


# ------------------------------------------------------------------
# 1. BUILD ACCOUNT-LEVEL GROUND TRUTH
# ------------------------------------------------------------------
def fetch_ground_truth(conn):
    """
    Creates the account-level ground truth from the transaction data.

    An account is treated as a positive case if it appears as either
    sender or receiver on at least one transaction marked as laundering.
    """
    query = """
        SELECT account, MAX(is_laundering) AS is_laundering
        FROM (
            SELECT from_account AS account, is_laundering FROM raw_transactions
            UNION ALL
            SELECT to_account AS account, is_laundering FROM raw_transactions
        ) combined
        GROUP BY account
    """
    df = pd.read_sql(query, conn)
    df.set_index("account", inplace=True)
    print(f"Ground truth built for {len(df)} accounts "
          f"({df['is_laundering'].sum()} true launderers, "
          f"{df['is_laundering'].mean()*100:.3f}% positive rate)")
    return df


# ------------------------------------------------------------------
# 2. LOAD EACH DETECTION METHOD'S OUTPUT
# ------------------------------------------------------------------
def fetch_rule_based_scores(conn):
    """
    Loads the rule-based risk score for each account.

    The current account_risk_score table uses `account` as its key.
    """
    query = "SELECT account, risk_score FROM account_risk_score"
    df = pd.read_sql(query, conn)
    df.set_index("account", inplace=True)
    return df


def load_graph_based_flags():
    """
    Loads the graph detection outputs and converts them into
    account-level flags.
    """
    rapid = pd.read_csv("rapid_movement_flags.csv")
    rapid_flag = pd.Series(1, index=rapid["account"].unique(), name="rapid_flag")

    centrality = pd.read_csv("centrality_rankings.csv")
    cutoff = centrality["betweenness_centrality"].quantile(CENTRALITY_FLAG_PERCENTILE)
    centrality_flag = pd.Series(
        1,
        index=centrality.loc[centrality["betweenness_centrality"] >= cutoff, "account"],
        name="centrality_flag",
    )

    # Fan-in performed much better than the other graph methods,
    # so keep it as a separate detection method.
    fan_in = pd.read_csv("fan_in_flags.csv")
    fan_in_flag = pd.Series(1, index=fan_in["account"].unique(), name="fan_in_flag")

    return rapid_flag, centrality_flag, fan_in_flag


# ------------------------------------------------------------------
# 3. ASSEMBLE A SINGLE SCORECARD
# ------------------------------------------------------------------
def build_scorecard(ground_truth, rule_scores, rapid_flag, centrality_flag, fan_in_flag, rule_threshold):
    """
    Combines the ground truth and detection outputs into one
    account-level scorecard.

    The broad ensemble flags an account if any of the four methods
    trigger. Fan-in is also kept as a separate result for comparison.
    """
    card = ground_truth.copy()
    card = card.join(rule_scores, how="left")
    card["risk_score"] = card["risk_score"].fillna(0)
    card["rule_flag"] = (card["risk_score"] >= rule_threshold).astype(int)

    card = card.join(rapid_flag, how="left")
    card["rapid_flag"] = card["rapid_flag"].fillna(0).astype(int)

    card = card.join(centrality_flag, how="left")
    card["centrality_flag"] = card["centrality_flag"].fillna(0).astype(int)

    card = card.join(fan_in_flag, how="left")
    card["fan_in_flag"] = card["fan_in_flag"].fillna(0).astype(int)

    card["broad_ensemble"] = (
        (card["rule_flag"] + card["rapid_flag"] + card["centrality_flag"] + card["fan_in_flag"]) > 0
    ).astype(int)

    card["fan_in_led_ensemble"] = card["fan_in_flag"]

    return card


# ------------------------------------------------------------------
# 4. METRICS
# ------------------------------------------------------------------
def evaluate_method(card, flag_col, label="Method"):
    y_true = card["is_laundering"]
    y_pred = card[flag_col]

    prec = precision_score(y_true, y_pred, zero_division=0)
    rec = recall_score(y_true, y_pred, zero_division=0)
    f1 = f1_score(y_true, y_pred, zero_division=0)

    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0

    print(f"\n--- {label} ---")
    print(f"  Precision: {prec:.4f}   Recall: {rec:.4f}   F1: {f1:.4f}")
    print(f"  Confusion matrix -> TP: {tp}  FP: {fp}  FN: {fn}  TN: {tn}")
    print(f"  False Positive Rate: {fpr:.4%}")
    print(f"  Alert volume (flagged accounts): {y_pred.sum()}")

    return {
        "method": label, "precision": prec, "recall": rec, "f1": f1,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn, "fpr": fpr,
        "alert_volume": int(y_pred.sum()),
    }


def precision_recall_sweep(card):
    """
    Tests different risk score thresholds and plots the resulting
    precision-recall tradeoff.
    """
    y_true = card["is_laundering"]
    y_score = card["risk_score"]

    precision, recall, thresholds = precision_recall_curve(y_true, y_score)
    pr_auc = auc(recall, precision)

    plt.figure(figsize=(8, 6))
    plt.plot(recall, precision, label=f"Rule-based score (AUC={pr_auc:.3f})")
    plt.xlabel("Recall (catch rate)")
    plt.ylabel("Precision (alert accuracy)")
    plt.title("Precision-Recall Curve — Rule-Based Risk Score")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig("precision_recall_curve.png", dpi=200)
    plt.close()
    print(f"\nSaved precision_recall_curve.png (AUC = {pr_auc:.4f})")

    return pr_auc


# ------------------------------------------------------------------
# 5. MAIN
# ------------------------------------------------------------------
def main():
    conn = mysql.connector.connect(**DB_CONFIG)
    try:
        ground_truth = fetch_ground_truth(conn)
        rule_scores = fetch_rule_based_scores(conn)
        rapid_flag, centrality_flag, fan_in_flag = load_graph_based_flags()

        # Compare a few thresholds before selecting the middle one
        # for the graph and ensemble comparison.
        results = []
        for threshold in [3, 5, 7]:
            card = build_scorecard(
                ground_truth, rule_scores, rapid_flag, centrality_flag, fan_in_flag, threshold
            )
            results.append(evaluate_method(card, "rule_flag", f"Rule-based (threshold >= {threshold})"))

        # Use threshold 5 for the remaining comparisons.
        card = build_scorecard(ground_truth, rule_scores, rapid_flag, centrality_flag, fan_in_flag, rule_threshold=5)
        results.append(evaluate_method(card, "rapid_flag", "Graph: rapid pass-through"))
        results.append(evaluate_method(card, "centrality_flag", "Graph: high betweenness centrality"))
        results.append(evaluate_method(card, "fan_in_flag", "Graph: fan-in (collection point)"))
        results.append(evaluate_method(card, "broad_ensemble", "Ensemble (rule OR rapid OR centrality OR fan-in)"))
        results.append(evaluate_method(card, "fan_in_led_ensemble", "Fan-in-led (fan-in alone, not diluted by OR)"))

        precision_recall_sweep(card)

        summary = pd.DataFrame(results)[
            ["method", "precision", "recall", "f1", "fpr", "alert_volume", "tp", "fp", "fn", "tn"]
        ]
        summary.to_csv("detection_validation_summary.csv", index=False)
        print("\n=== SUMMARY ===")
        print(summary.to_string(index=False))
        print(
            "\nNote: compare 'broad_ensemble' vs 'fan_in_led' above - the broad OR "
            "typically shows higher recall but lower precision than fan-in alone, "
            "illustrating that combining a strong method with weaker ones via OR "
            "dilutes precision rather than improving it."
        )
        print("\nSaved detection_validation_summary.csv")

        # Export the account-level results for Tableau/Power BI.
        scorecard_export = card.reset_index()  # index is already named "account"
        scorecard_export.to_csv("account_scorecard.csv", index=False)
        print("Saved account_scorecard.csv (per-account flags + ground truth for BI tools)")

    finally:
        conn.close()


if __name__ == "__main__":
    main()

