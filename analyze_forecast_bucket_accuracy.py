#!/usr/bin/env python3
"""Measure how well weather forecasts identify winning Kalshi buckets.

This is a forecast-signal analysis, not a trading backtest. At fixed horizons
before the NWS CLI climate day ends, it selects the latest available forecast,
maps that forecast to one of the event's six Kalshi buckets, and compares it
with the official CLI temperature and winning bucket.

Outputs:
  forecast_bucket_accuracy_detail.csv
  forecast_bucket_accuracy_summary.csv

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import sqlite3
import statistics
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable


DEFAULT_HORIZONS = (36, 24, 18, 12, 8, 6, 4, 2)
MAX_SNAPSHOT_AGE_HOURS = 4.0

# Standard-time offsets are intentional. NWS CLI days run midnight-to-midnight
# local standard time, including during daylight-saving time.
STANDARD_UTC_OFFSETS = {
    "KATL": -5, "KAUS": -6, "KBOS": -5, "KMDW": -6, "KDFW": -6,
    "KDEN": -7, "KIAH": -6, "KLAS": -8, "KLAX": -8, "KMIA": -5,
    "KMSP": -6, "KMSY": -6, "KNYC": -5, "KOKC": -6, "KPHL": -5,
    "KPHX": -7, "KSAT": -6, "KSFO": -8, "KSEA": -8, "KDCA": -5,
}

MODEL_NAMES = (
    "ecmwf_ifs025",
    "gem_global",
    "gfs_seamless",
    "icon_global",
    "ukmo_global_deterministic_10km",
)


@dataclass(frozen=True)
class Bucket:
    ticker: str
    label: str
    low: float
    high: float
    index: int


@dataclass(frozen=True)
class Event:
    icao: str
    target_date: str
    market_type: str
    actual: float
    winner_ticker: str
    buckets: tuple[Bucket, ...]


@dataclass(frozen=True)
class Forecast:
    run_at: datetime
    value: float


def parse_args() -> argparse.Namespace:
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=base / "weather_track_download.db",
        help="Input SQLite database",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=base,
        help="Directory for result CSVs",
    )
    parser.add_argument(
        "--horizons",
        default=",".join(map(str, DEFAULT_HORIZONS)),
        help="Comma-separated hours before climate-day end",
    )
    parser.add_argument(
        "--max-age",
        type=float,
        default=MAX_SNAPSHOT_AGE_HOURS,
        help="Maximum age of forecast snapshot at a horizon, in hours",
    )
    return parser.parse_args()


def utc_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def climate_day_end_utc(icao: str, target_date: str) -> datetime:
    end_local_standard = datetime.combine(
        date.fromisoformat(target_date) + timedelta(days=1), time.min
    )
    offset = STANDARD_UTC_OFFSETS[icao]
    return (end_local_standard - timedelta(hours=offset)).replace(tzinfo=timezone.utc)


def climate_date_for_valid_time(icao: str, valid_time: str) -> str:
    valid_utc = utc_datetime(valid_time)
    standard_local = valid_utc + timedelta(hours=STANDARD_UTC_OFFSETS[icao])
    return standard_local.date().isoformat()


def round_cli_temperature(value: float) -> int:
    # Temperatures in this dataset are positive. Half-up mirrors conventional
    # whole-degree reporting better than Python's banker's rounding.
    return math.floor(value + 0.5)


def bucket_for_temperature(buckets: tuple[Bucket, ...], value: float) -> Bucket | None:
    rounded = round_cli_temperature(value)
    matches = [bucket for bucket in buckets if bucket.low <= rounded <= bucket.high]
    return matches[0] if len(matches) == 1 else None


def boundary_margin(bucket: Bucket, forecast: float) -> float:
    boundaries = []
    if bucket.low > -900:
        boundaries.append(bucket.low - 0.5)
    if bucket.high < 900:
        boundaries.append(bucket.high + 0.5)
    return min(abs(forecast - boundary) for boundary in boundaries) if boundaries else math.inf


def load_events(conn: sqlite3.Connection) -> list[Event]:
    outcome_rows = conn.execute(
        """
        SELECT icao, target_date, market_type, ticker, label, bucket_low,
               bucket_high, actual_temp_f, yes_won
        FROM contract_outcomes
        ORDER BY icao, target_date, market_type, bucket_low, bucket_high
        """
    ).fetchall()
    grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in outcome_rows:
        grouped[(row["icao"], row["target_date"], row["market_type"])].append(row)

    events = []
    for (icao, target_date, market_type), rows in grouped.items():
        buckets = tuple(
            Bucket(
                ticker=row["ticker"],
                label=row["label"],
                low=float(row["bucket_low"]),
                high=float(row["bucket_high"]),
                index=index,
            )
            for index, row in enumerate(rows)
        )
        winners = [row["ticker"] for row in rows if row["yes_won"] == 1]
        if len(buckets) != 6 or len(winners) != 1:
            continue
        events.append(
            Event(
                icao=icao,
                target_date=target_date,
                market_type=market_type,
                actual=float(rows[0]["actual_temp_f"]),
                winner_ticker=winners[0],
                buckets=buckets,
            )
        )
    return events


ForecastMap = dict[tuple[str, str, str, str], list[Forecast]]


def add_forecast(
    result: ForecastMap,
    source: str,
    icao: str,
    target_date: str,
    market_type: str,
    run_at: str,
    value,
) -> None:
    if value is None:
        return
    result[(source, icao, target_date, market_type)].append(
        Forecast(utc_datetime(run_at), float(value))
    )


def load_daily_forecasts(conn: sqlite3.Connection) -> ForecastMap:
    result: ForecastMap = defaultdict(list)

    for row in conn.execute(
        "SELECT run_at, icao, forecast_date, high_f, low_f FROM openmeteo_snapshots"
    ):
        add_forecast(
            result, "openmeteo_daily", row["icao"], row["forecast_date"],
            "high", row["run_at"], row["high_f"],
        )
        add_forecast(
            result, "openmeteo_daily", row["icao"], row["forecast_date"],
            "low", row["run_at"], row["low_f"],
        )

    for row in conn.execute(
        """
        SELECT run_at, icao, forecast_date, model_name, high_f, low_f
        FROM model_family_snapshots
        """
    ):
        add_forecast(
            result, row["model_name"], row["icao"], row["forecast_date"],
            "high", row["run_at"], row["high_f"],
        )
        add_forecast(
            result, row["model_name"], row["icao"], row["forecast_date"],
            "low", row["run_at"], row["low_f"],
        )

    for row in conn.execute(
        """
        SELECT run_at, icao, forecast_date, kind, mean_f, p50_f
        FROM openmeteo_ensemble_snapshots
        """
    ):
        add_forecast(
            result, "ensemble_mean", row["icao"], row["forecast_date"],
            row["kind"], row["run_at"], row["mean_f"],
        )
        add_forecast(
            result, "ensemble_median", row["icao"], row["forecast_date"],
            row["kind"], row["run_at"], row["p50_f"],
        )

    for forecasts in result.values():
        forecasts.sort(key=lambda item: item.run_at)
    return result


def load_nws_hourly_forecasts(conn: sqlite3.Connection) -> ForecastMap:
    # Reduce ~2.3M hourly rows to one predicted climate-day extreme per
    # station/run/date. Iterating once is much faster than repeated SQL scans.
    extremes: dict[tuple[str, str, str], list] = {}
    rows = conn.execute(
        """
        SELECT run_at, icao, valid_time, temp_f
        FROM nws_hourly_snapshots
        WHERE temp_f IS NOT NULL
        ORDER BY run_at, icao
        """
    )
    for row in rows:
        icao = row["icao"]
        target_date = climate_date_for_valid_time(icao, row["valid_time"])
        key = (row["run_at"], icao, target_date)
        value = float(row["temp_f"])
        valid_at = utc_datetime(row["valid_time"])
        if key not in extremes:
            extremes[key] = [value, value, valid_at, valid_at]
        else:
            extremes[key][0] = max(extremes[key][0], value)
            extremes[key][1] = min(extremes[key][1], value)
            extremes[key][2] = min(extremes[key][2], valid_at)
            extremes[key][3] = max(extremes[key][3], valid_at)

    result: ForecastMap = defaultdict(list)
    for (run_at, icao, target_date), (high, low, first_valid, last_valid) in extremes.items():
        end = climate_day_end_utc(icao, target_date)
        start = end - timedelta(hours=24)
        # Once part of the climate day has elapsed, the NWS hourly endpoint no
        # longer contains those past hours. Such a partial curve cannot produce
        # a valid full-day high or low forecast by itself.
        if first_valid > start or last_valid < end - timedelta(hours=1):
            continue
        add_forecast(result, "nws_hourly_extreme", icao, target_date, "high", run_at, high)
        add_forecast(result, "nws_hourly_extreme", icao, target_date, "low", run_at, low)
    for forecasts in result.values():
        forecasts.sort(key=lambda item: item.run_at)
    return result


def asof(
    forecasts: list[Forecast] | None,
    cutoff: datetime,
    max_age_hours: float,
) -> Forecast | None:
    if not forecasts:
        return None
    times = [item.run_at for item in forecasts]
    index = bisect.bisect_right(times, cutoff) - 1
    if index < 0:
        return None
    selected = forecasts[index]
    age = (cutoff - selected.run_at).total_seconds() / 3600
    return selected if age <= max_age_hours else None


def source_forecasts_at_cutoff(
    event: Event,
    cutoff: datetime,
    forecast_map: ForecastMap,
    max_age_hours: float,
) -> dict[str, Forecast]:
    sources = (
        "openmeteo_daily",
        "ensemble_mean",
        "ensemble_median",
        "nws_hourly_extreme",
        *MODEL_NAMES,
    )
    selected = {}
    for source in sources:
        forecast = asof(
            forecast_map.get((source, event.icao, event.target_date, event.market_type)),
            cutoff,
            max_age_hours,
        )
        if forecast:
            selected[source] = forecast

    family = [selected[name] for name in MODEL_NAMES if name in selected]
    if len(family) >= 3:
        selected["model_family_median"] = Forecast(
            max(item.run_at for item in family),
            statistics.median(item.value for item in family),
        )

    deterministic_names = ("openmeteo_daily", "nws_hourly_extreme", *MODEL_NAMES)
    deterministic = [selected[name] for name in deterministic_names if name in selected]
    if len(deterministic) >= 4:
        selected["all_deterministic_median"] = Forecast(
            max(item.run_at for item in deterministic),
            statistics.median(item.value for item in deterministic),
        )
    return selected


DETAIL_COLUMNS = (
    "icao", "target_date", "market_type", "horizon_hours", "cutoff_utc",
    "source", "forecast_run_at", "snapshot_age_hours", "forecast_f",
    "forecast_rounded_f", "actual_f", "temp_error_f", "abs_error_f",
    "predicted_ticker", "predicted_label", "winning_ticker", "exact_bucket",
    "bucket_distance", "adjacent_or_exact", "boundary_margin_f",
)


def build_detail(
    events: Iterable[Event],
    horizons: tuple[float, ...],
    forecast_map: ForecastMap,
    max_age_hours: float,
) -> list[dict]:
    detail = []
    for event in events:
        winner = next(bucket for bucket in event.buckets if bucket.ticker == event.winner_ticker)
        end = climate_day_end_utc(event.icao, event.target_date)
        for horizon in horizons:
            cutoff = end - timedelta(hours=horizon)
            selected = source_forecasts_at_cutoff(
                event, cutoff, forecast_map, max_age_hours
            )
            for source, forecast in selected.items():
                predicted = bucket_for_temperature(event.buckets, forecast.value)
                if predicted is None:
                    continue
                distance = abs(predicted.index - winner.index)
                detail.append(
                    {
                        "icao": event.icao,
                        "target_date": event.target_date,
                        "market_type": event.market_type,
                        "horizon_hours": horizon,
                        "cutoff_utc": cutoff.isoformat().replace("+00:00", "Z"),
                        "source": source,
                        "forecast_run_at": forecast.run_at.isoformat().replace("+00:00", "Z"),
                        "snapshot_age_hours": round(
                            (cutoff - forecast.run_at).total_seconds() / 3600, 3
                        ),
                        "forecast_f": round(forecast.value, 3),
                        "forecast_rounded_f": round_cli_temperature(forecast.value),
                        "actual_f": event.actual,
                        "temp_error_f": round(forecast.value - event.actual, 3),
                        "abs_error_f": round(abs(forecast.value - event.actual), 3),
                        "predicted_ticker": predicted.ticker,
                        "predicted_label": predicted.label,
                        "winning_ticker": winner.ticker,
                        "exact_bucket": int(distance == 0),
                        "bucket_distance": distance,
                        "adjacent_or_exact": int(distance <= 1),
                        "boundary_margin_f": round(
                            boundary_margin(predicted, forecast.value), 3
                        ),
                    }
                )
    return detail


SUMMARY_COLUMNS = (
    "station", "market_type", "source", "horizon_hours", "n",
    "exact_bucket_rate", "adjacent_or_exact_rate", "mae_f", "bias_f",
    "median_abs_error_f", "mean_bucket_distance", "mean_boundary_margin_f",
)


def mean(values: Iterable[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals)


def summarize(detail: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str, str, float], list[dict]] = defaultdict(list)
    for row in detail:
        # Overall, high/low-specific, and station-specific views.
        groups[("ALL", "all", row["source"], row["horizon_hours"])].append(row)
        groups[("ALL", row["market_type"], row["source"], row["horizon_hours"])].append(row)
        groups[(row["icao"], row["market_type"], row["source"], row["horizon_hours"])].append(row)

    result = []
    for (station, market_type, source, horizon), rows in sorted(groups.items()):
        result.append(
            {
                "station": station,
                "market_type": market_type,
                "source": source,
                "horizon_hours": horizon,
                "n": len(rows),
                "exact_bucket_rate": round(mean(r["exact_bucket"] for r in rows), 4),
                "adjacent_or_exact_rate": round(
                    mean(r["adjacent_or_exact"] for r in rows), 4
                ),
                "mae_f": round(mean(r["abs_error_f"] for r in rows), 3),
                "bias_f": round(mean(r["temp_error_f"] for r in rows), 3),
                "median_abs_error_f": round(
                    statistics.median(r["abs_error_f"] for r in rows), 3
                ),
                "mean_bucket_distance": round(
                    mean(r["bucket_distance"] for r in rows), 3
                ),
                "mean_boundary_margin_f": round(
                    mean(r["boundary_margin_f"] for r in rows), 3
                ),
            }
        )
    return result


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def print_overall(summary: list[dict]) -> None:
    rows = [
        row for row in summary
        if row["station"] == "ALL" and row["market_type"] == "all"
    ]
    print("\nOverall forecast accuracy")
    print(
        f"{'Source':<30} {'Hr':>4} {'N':>5} {'Exact':>8} "
        f"{'Adjacent':>9} {'MAE':>7} {'Bias':>7}"
    )
    print("-" * 78)
    for row in sorted(rows, key=lambda r: (r["horizon_hours"], -r["exact_bucket_rate"])):
        print(
            f"{row['source']:<30} {row['horizon_hours']:>4g} {row['n']:>5} "
            f"{row['exact_bucket_rate']:>7.1%} "
            f"{row['adjacent_or_exact_rate']:>8.1%} "
            f"{row['mae_f']:>6.2f}F {row['bias_f']:>+6.2f}F"
        )


def run(args: argparse.Namespace) -> int:
    db = args.db.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    horizons = tuple(sorted({float(value) for value in args.horizons.split(",")}, reverse=True))

    print(f"Reading {db}")
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        events = load_events(conn)
        print(f"Loaded {len(events):,} settled events.")
        forecast_map = load_daily_forecasts(conn)
        print("Loaded daily, ensemble, and model-family forecasts.")
        nws = load_nws_hourly_forecasts(conn)
        forecast_map.update(nws)
        print("Reduced NWS hourly forecasts into climate-day highs/lows.")

    detail = build_detail(events, horizons, forecast_map, args.max_age)
    summary = summarize(detail)
    detail_path = output_dir / "forecast_bucket_accuracy_detail.csv"
    summary_path = output_dir / "forecast_bucket_accuracy_summary.csv"
    write_csv(detail_path, DETAIL_COLUMNS, detail)
    write_csv(summary_path, SUMMARY_COLUMNS, summary)
    print_overall(summary)
    print(f"\nWrote {len(detail):,} forecast evaluations to {detail_path}")
    print(f"Wrote {len(summary):,} summary rows to {summary_path}")
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
