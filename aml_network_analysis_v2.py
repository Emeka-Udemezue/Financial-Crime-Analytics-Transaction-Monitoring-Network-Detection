"""
Financial Crime Analytics Portfolio Project
Network Analysis Module

Pulls high-risk accounts and their transactions from MySQL,
builds a directed transaction graph with NetworkX, detects
common laundering patterns, and creates visualizations of
the main fund-flow relationships.

Requirements:
    pip install mysql-connector-python networkx matplotlib pandas
"""

import mysql.connector
import networkx as nx
import matplotlib.pyplot as plt
import pandas as pd
import time
from itertools import islice

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "Password123!",
    "database": "fincrime_analytics",
}

# Only analyse accounts with a risk score at or above this level.
RISK_SCORE_THRESHOLD = 6

# Ignore very long cycles as they are less useful for this analysis.
MAX_CYCLE_LENGTH = 6

# Limit the number of hubs shown in visualizations.
TOP_N_HUBS_TO_PLOT = 25


def get_connection():
    return mysql.connector.connect(**DB_CONFIG)


# ---------------------------------------------------------------------
# STEP 1: Pull flagged accounts + their transactions
# ---------------------------------------------------------------------
def load_flagged_transactions(conn, risk_threshold=RISK_SCORE_THRESHOLD):
    """
    Retrieves transactions involving accounts that meet the
    selected risk score threshold.
    """
    query = """
        SELECT rt.from_account, rt.to_account, rt.amount_paid, rt.amount_received,
               rt.payment_currency, rt.txn_timestamp, rt.is_laundering
        FROM raw_transactions rt
        JOIN account_risk_score ars
          ON rt.from_account = ars.account
        WHERE ars.risk_score >= %s
    """
    df = pd.read_sql(query, conn, params=(risk_threshold,))
    print(f"Loaded {len(df):,} transactions involving flagged accounts.")
    return df


# ---------------------------------------------------------------------
# STEP 2: Build directed graph
# ---------------------------------------------------------------------
def build_graph(df):
    """
    Builds a directed transaction graph where accounts are nodes
    and transactions are edges.

    Repeated transactions between the same accounts are combined,
    with the total amount and transaction count stored on the edge.
    """
    G = nx.DiGraph()

    for _, row in df.iterrows():
        src, dst, amt = row["from_account"], row["to_account"], row["amount_paid"]
        if G.has_edge(src, dst):
            G[src][dst]["weight"] += amt
            G[src][dst]["txn_count"] += 1
        else:
            G.add_edge(src, dst, weight=amt, txn_count=1)

    print(f"Graph built: {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges.")
    return G


# ---------------------------------------------------------------------
# STEP 3: Detect typologies
# ---------------------------------------------------------------------
def detect_cycles(G, max_length=MAX_CYCLE_LENGTH, limit=100):
    """
    Finds simple cycles up to the specified length.

    A cycle can indicate funds moving through several accounts and
    eventually returning to the original account. The number of
    cycles returned is capped to keep the analysis manageable.
    """
    cycles = []
    for cycle in islice(nx.simple_cycles(G, length_bound=max_length), limit):
        cycles.append(cycle)
    print(f"Found {len(cycles)} laundering-candidate cycles (<= {max_length} hops).")
    return cycles


def detect_fan_out(G, min_out_degree=5):
    """Finds accounts sending funds to several distinct recipients."""
    fan_out = [(n, G.out_degree(n)) for n in G.nodes() if G.out_degree(n) >= min_out_degree]
    fan_out.sort(key=lambda x: x[1], reverse=True)
    print(f"Found {len(fan_out)} fan-out accounts (out-degree >= {min_out_degree}).")
    return fan_out


def detect_fan_in(G, min_in_degree=5):
    """Finds accounts receiving funds from several distinct senders."""
    fan_in = [(n, G.in_degree(n)) for n in G.nodes() if G.in_degree(n) >= min_in_degree]
    fan_in.sort(key=lambda x: x[1], reverse=True)
    print(f"Found {len(fan_in)} fan-in accounts (in-degree >= {min_in_degree}).")
    return fan_in


def sweep_fan_in_thresholds(G, df, thresholds=(3, 5, 10, 15, 20, 30)):
    """
    Tests different fan-in thresholds to see how precision changes
    as fewer accounts are flagged.

    The sweep is for comparison only. The main fan-in detection
    continues to use the default threshold of 5.
    """
    print("\n" + "=" * 60)
    print("FAN-IN THRESHOLD SWEEP")
    print("=" * 60)

    baseline = df["is_laundering"].mean()
    rows = []
    for t in thresholds:
        accounts = {n for n in G.nodes() if G.in_degree(n) >= t}
        precision = evaluate_against_labels(
            df, accounts, label=f"fan-in accounts (in-degree >= {t})"
        )
        rows.append({"min_in_degree": t, "accounts_flagged": len(accounts), "precision": precision})

    sweep_df = pd.DataFrame(rows)
    print("\nSweep summary:")
    for _, row in sweep_df.iterrows():
        lift = row["precision"] / baseline if baseline > 0 else float("nan")
        print(
            f"  min_in_degree={int(row['min_in_degree']):>3}  "
            f"accounts={int(row['accounts_flagged']):>7,}  "
            f"precision={row['precision']:.2%}  "
            f"({lift:.1f}x baseline)"
        )
    return sweep_df


def detect_rapid_movement(df, window_hours=1, amount_tolerance=0.15):
    """
    Looks for accounts where an incoming payment is followed by
    an outgoing payment within the selected time window and for
    a similar amount.

    The check is done separately from the SQL layering rule so
    the two approaches can be compared.

    The current version uses a shorter time window and an amount
    tolerance because the earlier 6-hour version produced too many
    ordinary pass-through accounts.

    The incoming and outgoing transactions are merged by account
    rather than looping through the full transaction set for each
    account.
    
    Returns a set of accounts flagged for rapid pass-through.
    """
    incoming = df[["to_account", "txn_timestamp", "amount_received"]].rename(
        columns={"to_account": "account", "txn_timestamp": "in_time", "amount_received": "in_amount"}
    )
    outgoing = df[["from_account", "txn_timestamp", "amount_paid"]].rename(
        columns={"from_account": "account", "txn_timestamp": "out_time", "amount_paid": "out_amount"}
    )

    # Match incoming and outgoing transactions for each account.
    merged = incoming.merge(outgoing, on="account", how="inner")

    delta_hours = (merged["out_time"] - merged["in_time"]).dt.total_seconds() / 3600
    within_window = (delta_hours >= 0) & (delta_hours <= window_hours)

    low = merged["in_amount"] * (1 - amount_tolerance)
    high = merged["in_amount"] * (1 + amount_tolerance)
    amount_matches = merged["out_amount"].between(low, high)

    flagged = set(merged.loc[within_window & amount_matches, "account"].unique())

    print(
        f"Found {len(flagged)} accounts with rapid, amount-matched pass-through "
        f"(<= {window_hours}h, amount within +/-{int(amount_tolerance * 100)}%)."
    )
    return flagged


def compute_centrality(G):
    """
    Calculates betweenness centrality to identify accounts that
    sit on a large number of shortest paths through the network.

    A sample is used instead of calculating exact centrality for
    every possible path, which is more practical for a large graph.

    Returns the full ranked list so percentile-based cutoffs can
    be calculated later.
    """
    k = min(500, G.number_of_nodes())
    centrality = nx.betweenness_centrality(G, k=k, seed=42)
    ranked = sorted(centrality.items(), key=lambda x: x[1], reverse=True)
    print(f"Computed betweenness centrality for {len(ranked)} accounts.")
    return ranked


# ---------------------------------------------------------------------
# STEP 4: Visualize
# ---------------------------------------------------------------------
def plot_subgraph(G, nodes_of_interest, title, filename):
    """
    Plots the selected accounts together with their immediate
    incoming and outgoing connections.

    A smaller subgraph is used because plotting the full network
    would make the visualization difficult to read.
    """
    sub_nodes = set(nodes_of_interest)
    for n in nodes_of_interest:
        sub_nodes.update(G.predecessors(n))
        sub_nodes.update(G.successors(n))

    H = G.subgraph(sub_nodes)

    plt.figure(figsize=(12, 9))
    pos = nx.spring_layout(H, k=0.6, seed=42)

    node_colors = ["red" if n in nodes_of_interest else "lightblue" for n in H.nodes()]
    edge_widths = [max(0.5, min(5, H[u][v]["txn_count"])) for u, v in H.edges()]

    nx.draw_networkx_nodes(H, pos, node_color=node_colors, node_size=300, alpha=0.85)
    nx.draw_networkx_edges(H, pos, width=edge_widths, alpha=0.4, arrows=True, arrowsize=10)
    nx.draw_networkx_labels(H, pos, font_size=7)

    plt.title(title)
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(filename, dpi=150)
    plt.close()
    print(f"Saved visualization: {filename}")


def plot_cycle(G, cycle, filename):
    """Plots a detected cycle as a closed transaction path."""
    H = G.subgraph(cycle)
    plt.figure(figsize=(8, 8))
    pos = nx.circular_layout(H)

    nx.draw_networkx_nodes(H, pos, node_color="orange", node_size=500)
    nx.draw_networkx_edges(H, pos, arrows=True, arrowsize=15, width=2)
    nx.draw_networkx_labels(H, pos, font_size=8)

    edge_labels = {(u, v): f"${H[u][v]['weight']:,.0f}" for u, v in H.edges()}
    nx.draw_networkx_edge_labels(H, pos, edge_labels=edge_labels, font_size=7)

    plt.title(f"Candidate Laundering Cycle ({len(cycle)} accounts)")
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(filename, dpi=150)
    plt.close()
    print(f"Saved cycle visualization: {filename}")


# ---------------------------------------------------------------------
# STEP 5: Validate against ground-truth labels
# ---------------------------------------------------------------------
def evaluate_against_labels(df, flagged_accounts, label="flagged accounts"):
    """
    Calculates the proportion of flagged transactions that are
    labelled as laundering in the source data.
    """
    flagged_txns = df[
        df["from_account"].isin(flagged_accounts) | df["to_account"].isin(flagged_accounts)
    ]
    if len(flagged_txns) == 0:
        print(f"No transactions found for {label}.")
        return 0.0

    precision = flagged_txns["is_laundering"].mean()
    print(f"Precision of {label} vs. ground truth: {precision:.2%}")
    print(f"({flagged_txns['is_laundering'].sum():,} of {len(flagged_txns):,} flagged txns are true positives)")
    return precision


def compare_typology_precision(df, cycles, fan_out, fan_in, rule_based_accounts):
    """
    Compares the graph-based typologies with the SQL rule-based
    detection using the dataset's laundering labels.

    The comparison covers cycles, fan-out, fan-in, and the combined
    graph typologies.
    """
    print("\n" + "=" * 60)
    print("TYPOLOGY PRECISION COMPARISON")
    print("=" * 60)

    # Get all accounts involved in the detected cycles.
    cycle_accounts = set()
    for cycle in cycles:
        cycle_accounts.update(cycle)

    fan_out_accounts = {acct for acct, _ in fan_out}
    fan_in_accounts = {acct for acct, _ in fan_in}
    typology_accounts = cycle_accounts | fan_out_accounts | fan_in_accounts

    results = {}
    results["SQL rule-based (structuring/layering/velocity)"] = evaluate_against_labels(
        df, rule_based_accounts, label="SQL rule-based accounts"
    )
    results["Cycle accounts"] = evaluate_against_labels(
        df, cycle_accounts, label="cycle-detected accounts"
    )
    results["Fan-out accounts"] = evaluate_against_labels(
        df, fan_out_accounts, label="fan-out accounts"
    )
    results["Fan-in accounts"] = evaluate_against_labels(
        df, fan_in_accounts, label="fan-in accounts"
    )
    results["Combined typology (cycle + fan-out + fan-in)"] = evaluate_against_labels(
        df, typology_accounts, label="combined typology accounts"
    )

    baseline = df["is_laundering"].mean()
    print(f"\nDataset baseline laundering rate (random-guess precision): {baseline:.2%}")
    print("\nSummary (higher is better, compare against baseline above):")
    for name, precision in results.items():
        flag = " <-- BELOW BASELINE, no better than random" if precision < baseline else ""
        print(f"  {name:50s}: {precision:.2%}{flag}")

    return results


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------
def main():
    conn = get_connection()
    try:
        t0 = time.time()
        df = load_flagged_transactions(conn)
        print(f"  [{time.time() - t0:.1f}s elapsed]")

        t0 = time.time()
        G = build_graph(df)
        print(f"  [{time.time() - t0:.1f}s elapsed]")

        t0 = time.time()
        cycles = detect_cycles(G)
        print(f"  [{time.time() - t0:.1f}s elapsed]")

        t0 = time.time()
        fan_out = detect_fan_out(G)
        print(f"  [{time.time() - t0:.1f}s elapsed]")

        t0 = time.time()
        fan_in = detect_fan_in(G)
        print(f"  [{time.time() - t0:.1f}s elapsed]")

        t0 = time.time()
        centrality = compute_centrality(G)
        print(f"  [{time.time() - t0:.1f}s elapsed]")

        t0 = time.time()
        rapid_accounts = detect_rapid_movement(df)
        print(f"  [{time.time() - t0:.1f}s elapsed]")

        # Use the accounts that triggered the SQL risk score for comparison.
        rule_based_accounts = set(df["from_account"].unique())
        compare_typology_precision(df, cycles, fan_out, fan_in, rule_based_accounts)

        # Check how fan-in precision changes at different thresholds.
        sweep_fan_in_thresholds(G, df)

        # ---- Export CSVs consumed by aml_validation.py ----
        fan_in_accounts = [acct for acct, _ in fan_in]
        pd.DataFrame({"account": fan_in_accounts}).to_csv(
            "fan_in_flags.csv", index=False
        )
        print("Saved fan_in_flags.csv")

        pd.DataFrame({"account": list(rapid_accounts)}).to_csv(
            "rapid_movement_flags.csv", index=False
        )
        print("Saved rapid_movement_flags.csv")

        pd.DataFrame(centrality, columns=["account", "betweenness_centrality"]).to_csv(
            "centrality_rankings.csv", index=False
        )
        print("Saved centrality_rankings.csv")

        # Visualize the largest fan-out hub.
        if fan_out:
            top_hub = fan_out[0][0]
            plot_subgraph(G, [top_hub], f"Fan-Out Hub: {top_hub}", "fan_out_hub.png")

        # Visualize the largest fan-in hub.
        if fan_in:
            top_collector = fan_in[0][0]
            plot_subgraph(G, [top_collector], f"Fan-In Collector: {top_collector}", "fan_in_collector.png")

        # Visualize the highest-centrality account.
        if centrality:
            top_mule = centrality[0][0]
            plot_subgraph(G, [top_mule], f"High-Centrality Account: {top_mule}", "high_centrality_account.png")

        # Visualize the first detected cycle.
        if cycles:
            plot_cycle(G, cycles[0], "laundering_cycle_example.png")
        else:
            print("No cycles detected within the length bound - consider raising MAX_CYCLE_LENGTH.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
