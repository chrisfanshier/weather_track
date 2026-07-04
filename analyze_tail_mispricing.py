#!/usr/bin/env python3
"""Build a unique-contract mispricing map for model-contradicted Kalshi tails.

Requires the detailed output from analyze_forecast_bucket_accuracy.py. For
each fixed horizon, every ticker appears at most once. The script uses the
latest executable quote at or before the horizon, determines whether the
bucket lies beyond all available deterministic forecasts, and compares the
realized NO win rate with the executable NO ask.

Results are gross of fees.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path


SOURCES = {
    "openmeteo_daily",
    "ecmwf_ifs025",
    "gem_global",
    "gfs_seamless",
    "icon_global",
    "ukmo_global_deterministic_10km",
}
VALIDATION_START = "2026-06-18"

CANDIDATE_COLUMNS = (
    "icao", "target_date", "market_type", "horizon_hours", "cutoff_utc",
    "ticker", "label", "bucket_low", "bucket_high", "tail_direction",
    "bucket_shape", "models_n", "model_min_f", "model_max_f",
    "model_spread_f", "contradiction_distance_f", "distance_band",
    "spread_band", "yes_ask", "no_ask", "no_price_band", "quote_run_at",
    "quote_age_hours", "actual_temp_f", "no_won", "gross_profit_cents",
    "sample_period",
)

MAP_COLUMNS = (
    "view", "horizon_hours", "market_type", "tail_direction", "bucket_shape",
    "distance_band", "spread_band", "no_price_band", "n", "no_wins",
    "no_losses", "no_win_rate", "avg_no_ask", "break_even_rate",
    "gross_edge_pct_points", "gross_profit_dollars", "gross_roi",
    "wilson_low", "wilson_high", "conservative_edge_pct_points",
    "train_n", "train_win_rate", "train_gross_roi", "validation_n",
    "validation_win_rate", "validation_gross_roi",
)


def parse_args() -> argparse.Namespace:
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=base / "weather_track_download.db")
    parser.add_argument(
        "--forecast-detail",
        type=Path,
        default=base / "forecast_bucket_accuracy_detail.csv",
    )
    parser.add_argument("--output-dir", type=Path, default=base)
    parser.add_argument("--margin", type=float, default=1.0)
    parser.add_argument("--min-models", type=int, default=4)
    parser.add_argument("--max-quote-age", type=float, default=4.0)
    parser.add_argument("--validation-start", default=VALIDATION_START)
    return parser.parse_args()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def no_price_band(value: int) -> str:
    if value < 50:
        return "01-49"
    if value < 60:
        return "50-59"
    if value < 70:
        return "60-69"
    if value < 75:
        return "70-74"
    if value < 80:
        return "75-79"
    if value < 85:
        return "80-84"
    if value < 90:
        return "85-89"
    if value < 95:
        return "90-94"
    if value < 98:
        return "95-97"
    return "98-100"


def distance_band(value: float) -> str:
    if value < 1.5:
        return "1.0-1.49"
    if value < 2:
        return "1.5-1.99"
    if value < 3:
        return "2.0-2.99"
    if value < 4:
        return "3.0-3.99"
    return "4.0+"


def spread_band(value: float) -> str:
    if value <= 2:
        return "0-2"
    if value <= 4:
        return "2-4"
    if value <= 6:
        return "4-6"
    return "6+"


def load_forecasts(path: Path) -> tuple[dict, dict]:
    groups: dict[tuple, dict[str, float]] = defaultdict(dict)
    metadata = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["source"] not in SOURCES:
                continue
            key = (
                row["icao"],
                row["target_date"],
                row["market_type"],
                float(row["horizon_hours"]),
            )
            groups[key][row["source"]] = float(row["forecast_f"])
            metadata[key] = (row["cutoff_utc"], float(row["actual_f"]))
    return groups, metadata


def load_database(conn: sqlite3.Connection) -> tuple[dict, dict]:
    contracts = defaultdict(list)
    for row in conn.execute(
        """
        SELECT icao, target_date, market_type, ticker, label, bucket_low,
               bucket_high, actual_temp_f, yes_won
        FROM contract_outcomes
        """
    ):
        contracts[(row["icao"], row["target_date"], row["market_type"])].append(
            dict(row)
        )

    histories = defaultdict(list)
    for row in conn.execute(
        """
        SELECT ticker, run_at, yes_ask, no_ask
        FROM kalshi_snapshots
        ORDER BY ticker, run_at
        """
    ):
        histories[row["ticker"]].append(
            (parse_dt(row["run_at"]), int(row["yes_ask"]), int(row["no_ask"]))
        )

    prepared = {}
    for ticker, values in histories.items():
        prepared[ticker] = ([value[0] for value in values], values)
    return contracts, prepared


def build_candidates(args: argparse.Namespace, groups: dict, metadata: dict,
                     contracts: dict, histories: dict) -> list[dict]:
    rows = []
    seen = set()
    for key, source_values in groups.items():
        if len(source_values) < args.min_models:
            continue
        model_values = list(source_values.values())
        model_min = min(model_values)
        model_max = max(model_values)
        spread = model_max - model_min
        cutoff_text, actual = metadata[key]
        cutoff = parse_dt(cutoff_text)

        for contract in contracts.get(key[:3], []):
            unique_key = (contract["ticker"], key[3])
            if unique_key in seen:
                continue
            seen.add(unique_key)

            history = histories.get(contract["ticker"])
            if not history:
                continue
            times, values = history
            index = bisect.bisect_right(times, cutoff) - 1
            if index < 0:
                continue
            quote_time, yes_ask, no_ask = values[index]
            quote_age = (cutoff - quote_time).total_seconds() / 3600
            if quote_age > args.max_quote_age:
                continue

            low = float(contract["bucket_low"])
            high = float(contract["bucket_high"])
            if low > -900 and low > model_max + args.margin:
                direction = "hot"
                distance = low - model_max
            elif high < 900 and high < model_min - args.margin:
                direction = "cold"
                distance = model_min - high
            else:
                continue

            no_won = 1 - int(contract["yes_won"])
            gross_profit = (100 - no_ask) if no_won else -no_ask
            shape = "open_ended" if low <= -900 or high >= 900 else "interior"
            rows.append(
                {
                    "icao": key[0],
                    "target_date": key[1],
                    "market_type": key[2],
                    "horizon_hours": key[3],
                    "cutoff_utc": cutoff_text,
                    "ticker": contract["ticker"],
                    "label": contract["label"],
                    "bucket_low": low,
                    "bucket_high": high,
                    "tail_direction": direction,
                    "bucket_shape": shape,
                    "models_n": len(model_values),
                    "model_min_f": round(model_min, 3),
                    "model_max_f": round(model_max, 3),
                    "model_spread_f": round(spread, 3),
                    "contradiction_distance_f": round(distance, 3),
                    "distance_band": distance_band(distance),
                    "spread_band": spread_band(spread),
                    "yes_ask": yes_ask,
                    "no_ask": no_ask,
                    "no_price_band": no_price_band(no_ask),
                    "quote_run_at": quote_time.isoformat().replace("+00:00", "Z"),
                    "quote_age_hours": round(quote_age, 3),
                    "actual_temp_f": actual,
                    "no_won": no_won,
                    "gross_profit_cents": gross_profit,
                    "sample_period": (
                        "validation"
                        if key[1] >= args.validation_start
                        else "train"
                    ),
                }
            )
    return rows


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
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


def subset_metrics(rows: list[dict]) -> tuple[int, float | None, float | None]:
    if not rows:
        return 0, None, None
    wins = sum(row["no_won"] for row in rows)
    cost = sum(row["no_ask"] for row in rows)
    profit = sum(row["gross_profit_cents"] for row in rows)
    return len(rows), wins / len(rows), profit / cost if cost else None


def aggregate(view: str, dimensions: tuple[str, ...],
              candidates: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for row in candidates:
        groups[tuple(row[dimension] for dimension in dimensions)].append(row)

    output = []
    for values, rows in groups.items():
        labels = dict(zip(dimensions, values))
        n = len(rows)
        wins = sum(row["no_won"] for row in rows)
        losses = n - wins
        cost = sum(row["no_ask"] for row in rows)
        profit = sum(row["gross_profit_cents"] for row in rows)
        win_rate = wins / n
        average_price = cost / n
        low, high = wilson_interval(wins, n)
        train = [row for row in rows if row["sample_period"] == "train"]
        validation = [row for row in rows if row["sample_period"] == "validation"]
        train_n, train_rate, train_roi = subset_metrics(train)
        val_n, val_rate, val_roi = subset_metrics(validation)

        output.append(
            {
                "view": view,
                "horizon_hours": labels.get("horizon_hours", "ALL"),
                "market_type": labels.get("market_type", "all"),
                "tail_direction": labels.get("tail_direction", "all"),
                "bucket_shape": labels.get("bucket_shape", "all"),
                "distance_band": labels.get("distance_band", "all"),
                "spread_band": labels.get("spread_band", "all"),
                "no_price_band": labels.get("no_price_band", "all"),
                "n": n,
                "no_wins": wins,
                "no_losses": losses,
                "no_win_rate": round(win_rate, 5),
                "avg_no_ask": round(average_price, 3),
                "break_even_rate": round(average_price / 100, 5),
                "gross_edge_pct_points": round(
                    100 * (win_rate - average_price / 100), 3
                ),
                "gross_profit_dollars": round(profit / 100, 2),
                "gross_roi": round(profit / cost, 5) if cost else None,
                "wilson_low": round(low, 5),
                "wilson_high": round(high, 5),
                "conservative_edge_pct_points": round(
                    100 * (low - average_price / 100), 3
                ),
                "train_n": train_n,
                "train_win_rate": (
                    round(train_rate, 5) if train_rate is not None else None
                ),
                "train_gross_roi": (
                    round(train_roi, 5) if train_roi is not None else None
                ),
                "validation_n": val_n,
                "validation_win_rate": (
                    round(val_rate, 5) if val_rate is not None else None
                ),
                "validation_gross_roi": (
                    round(val_roi, 5) if val_roi is not None else None
                ),
            }
        )
    return output


def build_map(candidates: list[dict]) -> list[dict]:
    specifications = (
        ("price", ("no_price_band",)),
        ("horizon_price", ("horizon_hours", "no_price_band")),
        ("type_price", ("market_type", "no_price_band")),
        ("horizon_type_price", ("horizon_hours", "market_type", "no_price_band")),
        ("shape_price", ("bucket_shape", "no_price_band")),
        ("direction_price", ("tail_direction", "no_price_band")),
        ("distance_price", ("distance_band", "no_price_band")),
        (
            "horizon_type_shape_distance_price",
            (
                "horizon_hours", "market_type", "bucket_shape",
                "distance_band", "no_price_band",
            ),
        ),
        (
            "full_segment",
            (
                "horizon_hours", "market_type", "tail_direction", "bucket_shape",
                "distance_band", "spread_band", "no_price_band",
            ),
        ),
    )
    rows = []
    for view, dimensions in specifications:
        rows.extend(aggregate(view, dimensions, candidates))
    return sorted(
        rows,
        key=lambda row: (
            row["view"],
            str(row["horizon_hours"]),
            row["market_type"],
            row["no_price_band"],
        ),
    )


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def print_findings(rows: list[dict]) -> None:
    eligible = [
        row for row in rows
        if row["n"] >= 30
        and row["train_n"] >= 15
        and row["validation_n"] >= 10
        and row["train_gross_roi"] is not None
        and row["validation_gross_roi"] is not None
    ]
    ranked = sorted(
        eligible,
        key=lambda row: (
            min(row["train_gross_roi"], row["validation_gross_roi"]),
            row["conservative_edge_pct_points"],
        ),
        reverse=True,
    )
    print("\nMost persistent segments (minimum 30 total, 10 validation):")
    print(
        f"{'View':<26} {'Hr':>4} {'Type':<5} {'Shape':<10} {'Dist':<10} "
        f"{'NO px':<7} {'N':>5} {'Win':>7} {'ROI':>7} {'Train':>7} {'Valid':>7}"
    )
    print("-" * 115)
    for row in ranked[:20]:
        print(
            f"{row['view']:<26} {str(row['horizon_hours']):>4} "
            f"{row['market_type']:<5} {row['bucket_shape']:<10} "
            f"{row['distance_band']:<10} {row['no_price_band']:<7} "
            f"{row['n']:>5} {row['no_win_rate']:>6.1%} "
            f"{row['gross_roi']:>6.1%} {row['train_gross_roi']:>6.1%} "
            f"{row['validation_gross_roi']:>6.1%}"
        )


def run(args: argparse.Namespace) -> int:
    db = args.db.expanduser().resolve()
    detail = args.forecast_detail.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    groups, metadata = load_forecasts(detail)
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        contracts, histories = load_database(conn)
    candidates = build_candidates(
        args, groups, metadata, contracts, histories
    )
    map_rows = build_map(candidates)

    candidate_path = output / "tail_candidates_unique.csv"
    map_path = output / "tail_mispricing_map.csv"
    write_csv(candidate_path, CANDIDATE_COLUMNS, candidates)
    write_csv(map_path, MAP_COLUMNS, map_rows)
    print(f"Wrote {len(candidates):,} unique ticker/horizon candidates to {candidate_path}")
    print(f"Wrote {len(map_rows):,} mispricing segments to {map_path}")
    print_findings(map_rows)
    print("\nAll returns are gross of fees.")
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
