#!/usr/bin/env python3
"""Refresh and compare low-temperature tail-NO backtests.

This script orchestrates the existing forecast and tail analysis scripts, then
prints a focused comparison of:

  * NO ask 80-84 cents
  * NO ask 85-89 cents
  * NO ask 80-89 cents

Results are shown separately by fixed forecast horizon. A ticker appears at
most once per horizon. Returns are gross of fees unless a flat fee assumption
is supplied.

Place this script beside:
  analyze_forecast_bucket_accuracy.py
  analyze_tail_mispricing.py
  weather_track_download.db

Typical use after refreshing and resolving the database:
  python rerun_low_tail_backtest.py

Reuse existing candidate files without rebuilding:
  python rerun_low_tail_backtest.py --no-rebuild
"""

from __future__ import annotations

import argparse
import csv
import math
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path


PRICE_RANGES = (
    ("80-84", 80, 84),
    ("85-89", 85, 89),
    ("80-89", 80, 89),
)

SUMMARY_COLUMNS = (
    "horizon_hours",
    "spread_filter",
    "price_range",
    "min_no_ask",
    "max_no_ask",
    "n",
    "no_wins",
    "no_losses",
    "no_win_rate",
    "avg_no_ask",
    "break_even_rate",
    "gross_profit_cents",
    "gross_roi",
    "net_profit_cents",
    "net_roi",
    "wilson_low",
    "wilson_high",
    "max_drawdown_cents",
    "train_n",
    "train_win_rate",
    "train_net_roi",
    "validation_n",
    "validation_win_rate",
    "validation_net_roi",
    "validation_start",
)


def parse_args() -> argparse.Namespace:
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=base / "weather_track_download.db",
        help="Refreshed SQLite database",
    )
    parser.add_argument(
        "--script-dir",
        type=Path,
        default=base,
        help="Folder containing the two prerequisite analysis scripts",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=base,
        help="Folder for intermediate and final CSV outputs",
    )
    parser.add_argument(
        "--horizons",
        default="36",
        help="Comma-separated fixed horizons; e.g. 40,39,38,37,36,35,34,33,32",
    )
    parser.add_argument("--margin", type=float, default=1.0)
    parser.add_argument("--min-models", type=int, default=4)
    parser.add_argument("--max-quote-age", type=float, default=4.0)
    parser.add_argument(
        "--max-spread",
        type=float,
        default=6.0,
        help="Also report a scanner-like model-spread subset at or below this value",
    )
    parser.add_argument(
        "--validation-start",
        help="First validation date, YYYY-MM-DD; default is final 30%% of dates",
    )
    parser.add_argument(
        "--validation-fraction",
        type=float,
        default=0.30,
        help="Chronological holdout fraction when --validation-start is omitted",
    )
    parser.add_argument(
        "--fee-per-contract-cents",
        type=float,
        default=0.0,
        help="Optional flat round-trip fee/slippage assumption per contract",
    )
    parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Reuse output-dir/tail_candidates_unique.csv",
    )
    return parser.parse_args()


def run_checked(command: list[str]) -> None:
    print("\n>", " ".join(command))
    completed = subprocess.run(command, check=False)
    if completed.returncode:
        raise RuntimeError(
            f"analysis command failed with exit code {completed.returncode}"
        )


def rebuild(args: argparse.Namespace, horizons: list[float]) -> Path:
    scripts = args.script_dir.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    db = args.db.expanduser().resolve()
    forecast_script = scripts / "analyze_forecast_bucket_accuracy.py"
    tail_script = scripts / "analyze_tail_mispricing.py"
    missing = [
        str(path)
        for path in (db, forecast_script, tail_script)
        if not path.exists()
    ]
    if missing:
        raise RuntimeError("missing required file(s): " + ", ".join(missing))

    horizon_text = ",".join(f"{value:g}" for value in horizons)
    run_checked(
        [
            sys.executable,
            str(forecast_script),
            "--db",
            str(db),
            "--output-dir",
            str(output),
            "--horizons",
            horizon_text,
            "--max-age",
            str(args.max_quote_age),
        ]
    )
    run_checked(
        [
            sys.executable,
            str(tail_script),
            "--db",
            str(db),
            "--forecast-detail",
            str(output / "forecast_bucket_accuracy_detail.csv"),
            "--output-dir",
            str(output),
            "--margin",
            str(args.margin),
            "--min-models",
            str(args.min_models),
            "--max-quote-age",
            str(args.max_quote_age),
            # This field is recalculated below using the requested split.
            "--validation-start",
            args.validation_start or "9999-12-31",
        ]
    )
    return output / "tail_candidates_unique.csv"


def load_candidates(path: Path) -> list[dict]:
    if not path.exists():
        raise RuntimeError(f"candidate ledger not found: {path}")
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                {
                    **row,
                    "horizon_hours": float(row["horizon_hours"]),
                    "model_spread_f": float(row["model_spread_f"]),
                    "contradiction_distance_f": float(
                        row["contradiction_distance_f"]
                    ),
                    "no_ask": float(row["no_ask"]),
                    "no_won": int(row["no_won"]),
                    "gross_profit_cents": float(row["gross_profit_cents"]),
                }
            )
    return rows


def choose_validation_start(
    rows: list[dict],
    explicit: str | None,
    fraction: float,
) -> str:
    if explicit:
        date.fromisoformat(explicit)
        return explicit
    dates = sorted({row["target_date"] for row in rows})
    if len(dates) < 2:
        return dates[0] if dates else "9999-12-31"
    fraction = min(max(fraction, 0.05), 0.50)
    validation_count = max(1, math.ceil(len(dates) * fraction))
    validation_count = min(validation_count, len(dates) - 1)
    return dates[-validation_count]


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return math.nan, math.nan
    p = wins / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    margin = (
        z
        * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
        / denominator
    )
    return center - margin, center + margin


def max_drawdown(rows: list[dict], fee: float) -> float:
    equity = peak = drawdown = 0.0
    for row in sorted(
        rows,
        key=lambda value: (
            value["target_date"],
            value["icao"],
            value["ticker"],
        ),
    ):
        equity += row["gross_profit_cents"] - fee
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def roi(rows: list[dict], fee: float) -> tuple[int, float | None, float | None]:
    if not rows:
        return 0, None, None
    wins = sum(row["no_won"] for row in rows)
    cost = sum(row["no_ask"] for row in rows)
    net_profit = sum(row["gross_profit_cents"] - fee for row in rows)
    return len(rows), wins / len(rows), net_profit / cost if cost else None


def metrics(
    rows: list[dict],
    horizon: float,
    spread_label: str,
    price_label: str,
    minimum: int,
    maximum: int,
    validation_start: str,
    fee: float,
) -> dict:
    wins = sum(row["no_won"] for row in rows)
    n = len(rows)
    cost = sum(row["no_ask"] for row in rows)
    gross_profit = sum(row["gross_profit_cents"] for row in rows)
    net_profit = gross_profit - fee * n
    low, high = wilson_interval(wins, n)
    train = [row for row in rows if row["target_date"] < validation_start]
    validation = [
        row for row in rows if row["target_date"] >= validation_start
    ]
    train_n, train_win, train_roi = roi(train, fee)
    val_n, val_win, val_roi = roi(validation, fee)
    return {
        "horizon_hours": f"{horizon:g}",
        "spread_filter": spread_label,
        "price_range": price_label,
        "min_no_ask": minimum,
        "max_no_ask": maximum,
        "n": n,
        "no_wins": wins,
        "no_losses": n - wins,
        "no_win_rate": wins / n if n else None,
        "avg_no_ask": cost / n if n else None,
        "break_even_rate": cost / n / 100 if n else None,
        "gross_profit_cents": gross_profit,
        "gross_roi": gross_profit / cost if cost else None,
        "net_profit_cents": net_profit,
        "net_roi": net_profit / cost if cost else None,
        "wilson_low": low if n else None,
        "wilson_high": high if n else None,
        "max_drawdown_cents": max_drawdown(rows, fee),
        "train_n": train_n,
        "train_win_rate": train_win,
        "train_net_roi": train_roi,
        "validation_n": val_n,
        "validation_win_rate": val_win,
        "validation_net_roi": val_roi,
        "validation_start": validation_start,
    }


def build_comparison(
    candidates: list[dict],
    horizons: list[float],
    max_spread: float,
    validation_start: str,
    fee: float,
) -> tuple[list[dict], list[dict]]:
    base = [
        row
        for row in candidates
        if row["market_type"] == "low"
        and row["contradiction_distance_f"] > 1.0
    ]
    summary = []
    selected = []
    for horizon in horizons:
        horizon_rows = [
            row
            for row in base
            if math.isclose(row["horizon_hours"], horizon, abs_tol=1e-6)
        ]
        spread_sets = (
            ("all", horizon_rows),
            (
                f"<= {max_spread:g}F",
                [
                    row
                    for row in horizon_rows
                    if row["model_spread_f"] <= max_spread
                ],
            ),
        )
        for spread_label, spread_rows in spread_sets:
            for price_label, minimum, maximum in PRICE_RANGES:
                rows = [
                    row
                    for row in spread_rows
                    if minimum <= row["no_ask"] <= maximum
                ]
                summary.append(
                    metrics(
                        rows,
                        horizon,
                        spread_label,
                        price_label,
                        minimum,
                        maximum,
                        validation_start,
                        fee,
                    )
                )
                for row in rows:
                    selected.append(
                        {
                            **row,
                            "comparison_price_range": price_label,
                            "comparison_spread_filter": spread_label,
                            "comparison_validation_period": (
                                "validation"
                                if row["target_date"] >= validation_start
                                else "train"
                            ),
                        }
                    )
    return summary, selected


def format_percent(value) -> str:
    return "--" if value is None else f"{100 * value:>+6.1f}%"


def print_summary(rows: list[dict], fee: float) -> None:
    print("\nFocused low-market tail-NO comparison")
    print(
        f"{'Hr':>4} {'Spread':<8} {'NO ask':<6} {'N':>5} {'Wins':>6} "
        f"{'Win rate':>9} {'Avg ask':>8} {'Net ROI':>9} "
        f"{'Train':>9} {'Valid':>9} {'Val N':>6}"
    )
    print("-" * 105)
    for row in rows:
        avg = (
            "--"
            if row["avg_no_ask"] is None
            else f"{row['avg_no_ask']:.1f}c"
        )
        print(
            f"{row['horizon_hours']:>4} "
            f"{row['spread_filter']:<8} "
            f"{row['price_range']:<6} "
            f"{row['n']:>5} {row['no_wins']:>6} "
            f"{format_percent(row['no_win_rate']):>9} {avg:>8} "
            f"{format_percent(row['net_roi']):>9} "
            f"{format_percent(row['train_net_roi']):>9} "
            f"{format_percent(row['validation_net_roi']):>9} "
            f"{row['validation_n']:>6}"
        )
    print(
        "\nNet ROI includes "
        f"{fee:g} cents per contract of assumed fees/slippage."
    )


def write_csv(path: Path, columns: tuple[str, ...] | list[str],
              rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=columns,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def run(args: argparse.Namespace) -> int:
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    horizons = sorted(
        {float(value.strip()) for value in args.horizons.split(",")},
        reverse=True,
    )
    candidate_path = (
        output / "tail_candidates_unique.csv"
        if args.no_rebuild
        else rebuild(args, horizons)
    )
    candidates = load_candidates(candidate_path)
    relevant = [
        row
        for row in candidates
        if row["market_type"] == "low"
        and any(
            math.isclose(row["horizon_hours"], horizon, abs_tol=1e-6)
            for horizon in horizons
        )
    ]
    validation_start = choose_validation_start(
        relevant,
        args.validation_start,
        args.validation_fraction,
    )
    summary, selected = build_comparison(
        candidates,
        horizons,
        args.max_spread,
        validation_start,
        args.fee_per_contract_cents,
    )

    summary_path = output / "low_tail_price_range_backtest.csv"
    trade_path = output / "low_tail_price_range_trades.csv"
    write_csv(summary_path, SUMMARY_COLUMNS, summary)
    trade_columns = list(selected[0]) if selected else [
        "icao",
        "target_date",
        "ticker",
        "comparison_price_range",
    ]
    write_csv(trade_path, trade_columns, selected)

    print(f"\nChronological validation begins {validation_start}.")
    print_summary(summary, args.fee_per_contract_cents)
    print(f"\nWrote summary: {summary_path}")
    print(f"Wrote trade audit: {trade_path}")
    print(
        "\nDo not pool multiple horizons: the same ticker can appear once at "
        "each checkpoint. Compare horizons as separate strategies."
    )
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
