#!/usr/bin/env python3
"""Live-source scanner for the historically promising low-tail NO setup.

This scanner does not use SQLite or downloaded CSVs. It reuses the live API
helpers in tier_ev_new.py and the STATIONS configuration in tracker.py.

Default rule:
  * low-temperature contracts
  * 36 hours (+/- 4 hours) before the NWS CLI climate-day end
  * executable NO ask from 85 to 89 cents
  * bucket at least 1 F beyond every available deterministic model
  * NWS hourly forecast must confirm the contradiction

Example:
    python scan_low_tails_live.py --project-dir C:\\weather_track
"""

from __future__ import annotations

import argparse
import bisect
import csv
import importlib
import math
import sqlite3
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path


# Local standard-time offsets define the official CLI climate day.
STANDARD_UTC_OFFSETS = {
    "KATL": -5, "KAUS": -6, "KBOS": -5, "KMDW": -6, "KDFW": -6,
    "KDEN": -7, "KIAH": -6, "KLAS": -8, "KLAX": -8, "KMIA": -5,
    "KMSP": -6, "KMSY": -6, "KNYC": -5, "KOKC": -6, "KPHL": -5,
    "KPHX": -7, "KSAT": -6, "KSFO": -8, "KSEA": -8, "KDCA": -5,
}

MODEL_NAMES = {
    "ecmwf_ifs025",
    "gem_global",
    "gfs_seamless",
    "icon_global",
    "ukmo_global_deterministic_10km",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="Folder containing tracker.py and tier_ev_new.py",
    )
    parser.add_argument(
        "--history-db",
        type=Path,
        help="Historical database; defaults to PROJECT-DIR/weather_track_download.db",
    )
    parser.add_argument("--target-hours", type=float, default=36.0)
    parser.add_argument("--window-hours", type=float, default=4.0)
    parser.add_argument("--min-no-ask", type=float, default=85.0)
    parser.add_argument("--max-no-ask", type=float, default=89.0)
    parser.add_argument("--margin", type=float, default=1.0)
    parser.add_argument(
        "--history-min-samples",
        type=int,
        default=15,
        help="Minimum historical station/type observations required",
    )
    parser.add_argument(
        "--bias-prior-strength",
        type=float,
        default=20.0,
        help="Shrinkage strength pulling city bias toward zero",
    )
    parser.add_argument(
        "--lower-quantile",
        type=float,
        default=0.10,
        help="Historical residual quantile used for cold-tail protection",
    )
    parser.add_argument(
        "--upper-quantile",
        type=float,
        default=0.90,
        help="Historical residual quantile used for warm-tail protection",
    )
    parser.add_argument(
        "--no-history-filter",
        action="store_true",
        help="Display historical calibration but do not require interval clearance",
    )
    parser.add_argument(
        "--min-deterministic-models",
        type=int,
        default=4,
        help="Minimum Open-Meteo/model-family forecasts, excluding NWS",
    )
    parser.add_argument(
        "--allow-missing-nws",
        action="store_true",
        help="Allow candidates without an NWS hourly forecast",
    )
    parser.add_argument(
        "--interior-only",
        action="store_true",
        help="Exclude open-ended 'X or above/below' contracts",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of stations scanned concurrently",
    )
    parser.add_argument("--csv", type=Path, help="Optional candidate CSV output")
    return parser.parse_args()


def load_project(project_dir: Path):
    project_dir = project_dir.expanduser().resolve()
    required = (project_dir / "tracker.py", project_dir / "tier_ev_new.py")
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("missing project files: " + ", ".join(missing))
    sys.path.insert(0, str(project_dir))
    tracker = importlib.import_module("tracker")
    tev = importlib.import_module("tier_ev_new")
    return tracker.STATIONS, tev


def climate_day_end_utc(icao: str, target_date: str) -> datetime:
    standard_end = datetime.combine(
        date.fromisoformat(target_date) + timedelta(days=1), dt_time.min
    )
    return (
        standard_end - timedelta(hours=STANDARD_UTC_OFFSETS[icao])
    ).replace(tzinfo=timezone.utc)


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (position - low) * (ordered[high] - ordered[low])


def asof_value(
    rows: list[tuple[datetime, float]] | None,
    cutoff: datetime,
    max_age_hours: float = 4.0,
) -> float | None:
    if not rows:
        return None
    index = bisect.bisect_right([row[0] for row in rows], cutoff) - 1
    if index < 0:
        return None
    run_at, value = rows[index]
    age = (cutoff - run_at).total_seconds() / 3600
    return value if age <= max_age_hours else None


def load_historical_bias(
    db_path: Path,
    target_hours: float,
    min_models: int,
    prior_strength: float,
    lower_quantile: float,
    upper_quantile: float,
) -> dict[str, dict]:
    """Build station-level low residual distributions at the target horizon."""
    if not db_path.exists():
        raise RuntimeError(f"historical database not found: {db_path}")

    model_rows: dict[tuple[str, str, str], list[tuple[datetime, float]]] = {}
    om_rows: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    actuals = []
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            """
            SELECT run_at, icao, forecast_date, model_name, low_f
            FROM model_family_snapshots
            WHERE low_f IS NOT NULL
            ORDER BY icao, forecast_date, model_name, run_at
            """
        ):
            key = (row["icao"], row["forecast_date"], row["model_name"])
            model_rows.setdefault(key, []).append(
                (datetime.fromisoformat(row["run_at"].replace("Z", "+00:00")),
                 float(row["low_f"]))
            )
        for row in conn.execute(
            """
            SELECT run_at, icao, forecast_date, low_f
            FROM openmeteo_snapshots
            WHERE low_f IS NOT NULL
            ORDER BY icao, forecast_date, run_at
            """
        ):
            key = (row["icao"], row["forecast_date"])
            om_rows.setdefault(key, []).append(
                (datetime.fromisoformat(row["run_at"].replace("Z", "+00:00")),
                 float(row["low_f"]))
            )
        actuals = list(
            conn.execute(
                """
                SELECT icao, target_date, low_f
                FROM cli_actual_temperatures
                WHERE low_f IS NOT NULL
                """
            )
        )

    residuals: dict[str, list[float]] = {}
    for row in actuals:
        icao = row["icao"]
        target_date = row["target_date"]
        if icao not in STANDARD_UTC_OFFSETS:
            continue
        cutoff = climate_day_end_utc(icao, target_date) - timedelta(
            hours=target_hours
        )
        values = []
        om = asof_value(om_rows.get((icao, target_date)), cutoff)
        if om is not None:
            values.append(om)
        for model in MODEL_NAMES:
            value = asof_value(
                model_rows.get((icao, target_date, model)), cutoff
            )
            if value is not None:
                values.append(value)
        if len(values) < min_models:
            continue
        consensus = statistics.median(values)
        residuals.setdefault(icao, []).append(float(row["low_f"]) - consensus)

    output = {}
    for icao, values in residuals.items():
        n = len(values)
        raw_mean = statistics.mean(values)
        shrinkage = n / (n + max(0.0, prior_strength))
        shrunk_mean = raw_mean * shrinkage
        raw_low = percentile(values, lower_quantile)
        raw_high = percentile(values, upper_quantile)
        # Preserve empirical spread while shrinking only the location shift.
        adjusted_low = shrunk_mean + (raw_low - raw_mean)
        adjusted_high = shrunk_mean + (raw_high - raw_mean)
        output[icao] = {
            "n": n,
            "raw_mean": raw_mean,
            "shrunk_mean": shrunk_mean,
            "raw_lower": raw_low,
            "raw_upper": raw_high,
            "adjusted_lower": adjusted_low,
            "adjusted_upper": adjusted_high,
            "median": statistics.median(values),
        }
    return output


def contradiction(
    low: float,
    high: float,
    values: list[float],
    margin: float,
) -> tuple[str, float] | None:
    if not values:
        return None
    model_min = min(values)
    model_max = max(values)
    if low > -900 and low > model_max + margin:
        return "warm_low_tail", low - model_max
    if high < 900 and high < model_min - margin:
        return "cold_low_tail", model_min - high
    return None


def scan_station(
    icao: str,
    info: dict,
    tev,
    args: argparse.Namespace,
    now_utc: datetime,
    historical_bias: dict[str, dict],
) -> tuple[list[dict], list[str]]:
    errors = []
    target_dates = tev.target_dates_for_station(info["tz"])

    try:
        buckets = tev.fetch_station_buckets(info, target_dates, "low")
    except Exception as exc:
        return [], [f"{icao} Kalshi: {exc}"]
    if not buckets:
        return [], []

    om_det = None
    try:
        om_det = tev.fetch_openmeteo_deterministic(
            info["lat"], info["lon"], info["tz"]
        )
    except Exception as exc:
        errors.append(f"{icao} Open-Meteo: {exc}")

    family_payload = None
    try:
        family_payload = tev.fetch_openmeteo_model_families(
            info["lat"], info["lon"], info["tz"]
        )
    except Exception as exc:
        errors.append(f"{icao} model families: {exc}")

    nws_hourly = {}
    try:
        nws_hourly = tev.fetch_nws_hourly_extremes(
            info["lat"], info["lon"], info["tz"]
        )
    except Exception as exc:
        errors.append(f"{icao} NWS hourly: {exc}")

    # Period forecast is context only. Historical validation used NWS hourly.
    nws_period = {}
    try:
        nws_period = tev.fetch_nws_period_extremes(
            info["lat"], info["lon"], info["tz"]
        )
    except Exception as exc:
        errors.append(f"{icao} NWS periods: {exc}")

    rows = []
    for bucket in buckets:
        target_date = bucket["target_date"]
        hours_remaining = (
            climate_day_end_utc(icao, target_date) - now_utc
        ).total_seconds() / 3600
        if abs(hours_remaining - args.target_hours) > args.window_hours:
            continue
        if not args.min_no_ask <= bucket["no"] <= args.max_no_ask:
            continue

        low = float(bucket["bucket_low"])
        high = float(bucket["bucket_high"])
        open_ended = low <= -900 or high >= 900
        if args.interior_only and open_ended:
            continue

        deterministic = {}
        om_value = tev.om_daily_value(om_det, target_date, "low")
        if om_value is not None:
            deterministic["openmeteo_daily"] = float(om_value)
        deterministic.update(
            tev.model_family_values(family_payload, target_date, "low")
        )
        if len(deterministic) < args.min_deterministic_models:
            continue

        nws_value = None
        nws_time = None
        if target_date in nws_hourly:
            item = nws_hourly[target_date].get("low")
            if item:
                nws_value, nws_time = float(item[0]), item[1]
        if nws_value is None and not args.allow_missing_nws:
            continue

        all_values = list(deterministic.values())
        if nws_value is not None:
            all_values.append(nws_value)
        result = contradiction(low, high, all_values, args.margin)
        if result is None:
            continue
        direction, distance = result

        history = historical_bias.get(icao)
        if (
            history is None
            or history["n"] < args.history_min_samples
        ):
            errors.append(
                f"{icao} historical calibration unavailable or too small"
            )
            continue
        current_consensus = statistics.median(deterministic.values())
        corrected_consensus = current_consensus + history["shrunk_mean"]
        conservative_low = current_consensus + history["adjusted_lower"]
        conservative_high = current_consensus + history["adjusted_upper"]
        if direction == "cold_low_tail":
            interval_clearance = conservative_low - high
        else:
            interval_clearance = low - conservative_high
        if interval_clearance <= 0 and not args.no_history_filter:
            continue

        period_low = None
        if target_date in nws_period:
            value = nws_period[target_date].get("low")
            if value is not None:
                period_low = float(value)

        model_min = min(all_values)
        model_max = max(all_values)
        no_profit = 100.0 - float(bucket["no"])
        rows.append(
            {
                "icao": icao,
                "city": info["name"],
                "target_date": target_date,
                "hours_remaining": round(hours_remaining, 2),
                "ticker": bucket["ticker"],
                "label": bucket["label"],
                "direction": direction,
                "bucket_shape": "open_ended" if open_ended else "interior",
                "bucket_low": low,
                "bucket_high": high,
                "yes_ask": float(bucket["yes"]),
                "no_ask": float(bucket["no"]),
                "no_profit_if_right": round(no_profit, 2),
                "no_roi_if_right_pct": round(
                    no_profit / float(bucket["no"]) * 100, 2
                ),
                "deterministic_models_n": len(deterministic),
                "all_forecasts_n": len(all_values),
                "forecast_min_f": round(model_min, 2),
                "forecast_max_f": round(model_max, 2),
                "forecast_spread_f": round(model_max - model_min, 2),
                "distance_beyond_all_f": round(distance, 2),
                "history_n": history["n"],
                "historical_raw_bias_f": round(history["raw_mean"], 2),
                "historical_shrunk_bias_f": round(
                    history["shrunk_mean"], 2
                ),
                "historical_lower_residual_f": round(
                    history["adjusted_lower"], 2
                ),
                "historical_upper_residual_f": round(
                    history["adjusted_upper"], 2
                ),
                "current_consensus_low_f": round(current_consensus, 2),
                "bias_corrected_consensus_low_f": round(
                    corrected_consensus, 2
                ),
                "conservative_low_f": round(conservative_low, 2),
                "conservative_high_f": round(conservative_high, 2),
                "historical_interval_clearance_f": round(
                    interval_clearance, 2
                ),
                "nws_hourly_low_f": nws_value,
                "nws_hourly_low_time": nws_time,
                "nws_period_low_f": period_low,
                "forecasts": "; ".join(
                    [
                        *(
                            f"{name}={value:.1f}"
                            for name, value in sorted(deterministic.items())
                        ),
                        *(
                            [f"nws_hourly={nws_value:.1f}"]
                            if nws_value is not None
                            else []
                        ),
                    ]
                ),
                "scanned_at": now_utc.isoformat().replace("+00:00", "Z"),
            }
        )
    return rows, errors


def print_rows(rows: list[dict]) -> None:
    if not rows:
        print("\nNo matching live candidates.")
        return
    print(
        f"\n{'ICAO':<5} {'City':<16} {'Date':<10} {'Hours':>6} "
        f"{'NO':>5} {'YES':>5} {'Dist':>6} {'Spread':>7} "
        f"{'NWS':>6} {'Direction':<14} Bucket"
    )
    print("-" * 130)
    for row in rows:
        nws = (
            f"{row['nws_hourly_low_f']:.1f}"
            if row["nws_hourly_low_f"] is not None
            else "--"
        )
        print(
            f"{row['icao']:<5} {row['city']:<16} {row['target_date']:<10} "
            f"{row['hours_remaining']:>6.1f} {row['no_ask']:>5.1f} "
            f"{row['yes_ask']:>5.1f} {row['distance_beyond_all_f']:>6.1f} "
            f"{row['forecast_spread_f']:>7.1f} {nws:>6} "
            f"{row['direction']:<14} {row['label']}"
        )
        print(f"      {row['forecasts']}")
        print(
            f"      history n={row['history_n']} "
            f"bias={row['historical_shrunk_bias_f']:+.1f}F "
            f"interval={row['conservative_low_f']:.1f}-"
            f"{row['conservative_high_f']:.1f}F "
            f"clearance={row['historical_interval_clearance_f']:.1f}F"
        )


def write_csv(path: Path, rows: list[dict]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0]) if rows else [
        "icao", "city", "target_date", "ticker", "label"
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} candidates to {path}")


def run(args: argparse.Namespace) -> int:
    project_dir = args.project_dir.expanduser().resolve()
    stations, tev = load_project(project_dir)
    history_db = (
        args.history_db.expanduser().resolve()
        if args.history_db
        else project_dir / "weather_track_download.db"
    )
    historical_bias = load_historical_bias(
        history_db,
        args.target_hours,
        args.min_deterministic_models,
        args.bias_prior_strength,
        args.lower_quantile,
        args.upper_quantile,
    )
    now_utc = datetime.now(timezone.utc)
    all_rows = []
    all_errors = []

    print(
        f"Scanning {len(stations)} stations from live sources at "
        f"{now_utc.isoformat().replace('+00:00', 'Z')}"
    )
    print(
        f"Historical calibration: {history_db} "
        f"({len(historical_bias)} stations)"
    )
    print(
        f"Rule: low tails, {args.target_hours:g}h +/- {args.window_hours:g}h, "
        f"NO {args.min_no_ask:g}-{args.max_no_ask:g}c, "
        f"{args.margin:g}F beyond all deterministic forecasts and NWS"
    )

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                scan_station, icao, info, tev, args, now_utc, historical_bias
            ): icao
            for icao, info in stations.items()
        }
        completed = 0
        for future in as_completed(futures):
            icao = futures[future]
            completed += 1
            try:
                rows, errors = future.result()
                all_rows.extend(rows)
                all_errors.extend(errors)
                print(
                    f"  [{completed:>2}/{len(stations)}] {icao}: "
                    f"{len(rows)} candidate(s)"
                )
            except Exception as exc:
                all_errors.append(f"{icao}: {exc}")
                print(f"  [{completed:>2}/{len(stations)}] {icao}: failed")

    all_rows.sort(
        key=lambda row: (
            -row["distance_beyond_all_f"],
            row["forecast_spread_f"],
            row["no_ask"],
        )
    )
    print_rows(all_rows)
    if all_errors:
        print("\nSource warnings:")
        for error in all_errors:
            print(f"  - {error}")
    if args.csv:
        write_csv(args.csv, all_rows)
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except (RuntimeError, ImportError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
