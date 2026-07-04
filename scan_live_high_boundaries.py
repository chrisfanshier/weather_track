#!/usr/bin/env python3
"""Trial scanner for fleeting live-ASOS opportunities in daily-high markets.

The scanner is observational. It does not place orders.

Signals:
  PASSED
      The precise observed high has cleared an interior bucket's upper raw
      rounding boundary by a safety buffer. That bucket should be priced out,
      subject to observation quality and official CLI settlement differences.

  NEAR_BOUNDARY
      The running high is just below a bucket's upper raw boundary, the latest
      observation remains near the high, temperature is rising, and a simple
      forecast projection reaches beyond the boundary.

  CURRENT_HIGH_BUCKET
      The precise running high already lies inside a cheaply priced bucket and
      the simple projection does not require additional warming to reach it.

  PROJECTED_HIGH_WATCH
      A heuristically bias-adjusted forecast peak lands in a bucket whose YES
      ask is below a configurable watch threshold. This is a watch signal, not
      a validated strategy.

Place this script in the weather_track folder beside tracker.py,
dig_station.py, and tier_ev_new.py.

Examples:
  python scan_live_high_boundaries.py
  python scan_live_high_boundaries.py --watch --interval 300
  python scan_live_high_boundaries.py --stations KOKC,KATL,KPHX
"""

from __future__ import annotations

import argparse
import csv
import importlib
import math
import re
import sqlite3
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
USER_AGENT = "weather-track-live-boundary-trial/1.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="Folder containing tracker.py, dig_station.py, tier_ev_new.py",
    )
    parser.add_argument(
        "--stations",
        help="Optional comma-separated ICAO list; default is all tracker stations",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument(
        "--interval",
        type=float,
        default=300,
        help="Seconds between watch scans; minimum 60",
    )
    parser.add_argument(
        "--history-hours",
        type=float,
        default=18,
        help="Maximum observation lookback; local midnight is used when later",
    )
    parser.add_argument(
        "--stale-observation-minutes",
        type=float,
        default=45,
    )
    parser.add_argument(
        "--passed-safety-f",
        type=float,
        default=0.2,
        help="Required clearance above raw bucket ceiling",
    )
    parser.add_argument(
        "--near-boundary-f",
        type=float,
        default=0.5,
    )
    parser.add_argument(
        "--near-high-f",
        type=float,
        default=0.4,
        help="Latest observation must remain this close to running high",
    )
    parser.add_argument(
        "--min-rise-30m-f",
        type=float,
        default=0.3,
    )
    parser.add_argument(
        "--max-passed-no-ask",
        type=float,
        default=98,
        help="Only display PASSED/NEAR contracts at or below this NO ask",
    )
    parser.add_argument(
        "--max-watch-yes-ask",
        type=float,
        default=30,
        help="Maximum YES ask for PROJECTED_HIGH_WATCH",
    )
    parser.add_argument(
        "--projection-residual-weight",
        type=float,
        default=0.60,
        help="Fraction of current hot/cold tracking residual applied to forecast peak",
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="Log database; default PROJECT-DIR/live_high_boundary_trial.db",
    )
    parser.add_argument("--csv", type=Path, help="Optional latest-scan signal CSV")
    return parser.parse_args()


def load_project(project_dir: Path):
    project_dir = project_dir.expanduser().resolve()
    required = [
        project_dir / "tracker.py",
        project_dir / "dig_station.py",
        project_dir / "tier_ev_new.py",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("missing project file(s): " + ", ".join(missing))
    sys.path.insert(0, str(project_dir))
    tracker = importlib.import_module("tracker")
    dig = importlib.import_module("dig_station")
    tev = importlib.import_module("tier_ev_new")
    return tracker.STATIONS, dig, tev


def parse_tgroup_f(raw: str | None) -> float | None:
    match = re.search(r"T(\d{4})(\d{4})", raw or "")
    if not match:
        return None
    block = match.group(1)
    celsius = int(block[1:]) / 10
    if block.startswith("1"):
        celsius *= -1
    return celsius * 9 / 5 + 32


def observation_temperature(raw: str, tmpf: str) -> tuple[float, float, float, str] | None:
    """Return display, lowest possible, highest possible F, and precision type.

    MADISHF five-minute reports transmit a whole-Celsius value derived from an
    internally stored whole-Fahrenheit value. Converting that Celsius value
    back to Fahrenheit creates false decimal precision. For those reports we
    retain every possible internal whole-F value.
    """
    converted = parse_tgroup_f(raw)
    if converted is None and tmpf != "M":
        try:
            converted = float(tmpf)
        except ValueError:
            return None
    if converted is None:
        return None
    if "MADISHF" in (raw or "").upper():
        celsius = (converted - 32) * 5 / 9
        reported_whole_c = round(celsius)
        lower_f = (reported_whole_c - 0.5) * 9 / 5 + 32
        upper_f = (reported_whole_c + 0.5) * 9 / 5 + 32
        possible = [
            value
            for value in range(-100, 141)
            if lower_f <= value < upper_f
        ]
        if possible:
            return converted, float(min(possible)), float(max(possible)), "5min_ambiguous"
    # Hourly/special T-groups preserve enough precision to recover the
    # internally stored whole-Fahrenheit observation.
    recovered = float(round(converted))
    return converted, recovered, recovered, "hourly_precise"


def fetch_day_observations(
    icao: str,
    info: dict,
    history_hours: float,
) -> list[dict]:
    """Fetch precise ASOS/MADISHF observations from local midnight onward."""
    station = icao[1:] if icao.startswith("K") else icao
    tz = ZoneInfo(info["tz"])
    now = datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = max(midnight, now - timedelta(hours=history_hours))
    params = [
        ("station", station),
        ("data", "tmpf"),
        ("data", "metar"),
        ("year1", start.year),
        ("month1", start.month),
        ("day1", start.day),
        ("hour1", start.hour),
        ("minute1", start.minute),
        ("year2", now.year),
        ("month2", now.month),
        ("day2", now.day),
        ("hour2", now.hour),
        ("minute2", now.minute),
        ("tz", info["tz"]),
        ("format", "onlycomma"),
        ("latlon", "no"),
        ("elev", "no"),
        ("missing", "M"),
        ("trace", "T"),
        ("direct", "no"),
        ("report_type", "1"),
        ("report_type", "2"),
        ("report_type", "3"),
    ]
    url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
    last_error = None
    for attempt in range(4):
        try:
            response = requests.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            if response.status_code == 429:
                raise RuntimeError("IEM rate limited")
            response.raise_for_status()
            break
        except Exception as exc:
            last_error = exc
            if attempt == 3:
                raise RuntimeError(f"IEM ASOS failed: {last_error}")
            time.sleep(2 + attempt * 3)

    rows = []
    seen = set()
    for line in response.text.splitlines():
        if not line or line.startswith("station"):
            continue
        parts = line.split(",", 3)
        if len(parts) < 4:
            continue
        _, valid, tmpf, raw = parts
        try:
            observed_at = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(
                tzinfo=tz
            )
        except ValueError:
            continue
        decoded = observation_temperature(raw, tmpf)
        if decoded is None:
            continue
        display_f, low_f, high_f, precision = decoded
        key = (observed_at, round(display_f, 3), raw)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "observed_at": observed_at,
                "temp_f": display_f,
                "temp_low_f": low_f,
                "temp_high_f": high_f,
                "source": precision,
                "raw": raw,
            }
        )
    rows.sort(key=lambda row: row["observed_at"])
    return rows


def value_near_time(rows: list[dict], target: datetime) -> float | None:
    if not rows:
        return None
    row = min(
        rows,
        key=lambda item: abs((item["observed_at"] - target).total_seconds()),
    )
    return row["temp_f"]


def observation_state(rows: list[dict], stale_minutes: float) -> dict:
    if not rows:
        raise RuntimeError("no usable observations")
    latest = rows[-1]
    now = datetime.now(latest["observed_at"].tzinfo)
    age_minutes = (now - latest["observed_at"]).total_seconds() / 60
    confirmed_high_row = max(rows, key=lambda row: row["temp_low_f"])
    possible_high_row = max(rows, key=lambda row: row["temp_high_f"])
    prior_15 = value_near_time(rows, latest["observed_at"] - timedelta(minutes=15))
    prior_30 = value_near_time(rows, latest["observed_at"] - timedelta(minutes=30))
    return {
        "latest_at": latest["observed_at"],
        "latest_temp_f": latest["temp_f"],
        "latest_source": latest["source"],
        "latest_raw": latest["raw"],
        "age_minutes": age_minutes,
        "stale": age_minutes > stale_minutes,
        "high_at": confirmed_high_row["observed_at"],
        "observed_high_f": confirmed_high_row["temp_low_f"],
        "observed_high_possible_f": possible_high_row["temp_high_f"],
        "high_raw": confirmed_high_row["raw"],
        "rise_15m_f": (
            latest["temp_f"] - prior_15 if prior_15 is not None else math.nan
        ),
        "rise_30m_f": (
            latest["temp_f"] - prior_30 if prior_30 is not None else math.nan
        ),
        "observations_n": len(rows),
    }


def nearest_curve_value(curve: list, observed_at: datetime, tz_name: str):
    if not curve:
        return None
    tz = ZoneInfo(tz_name)
    candidates = []
    for first, second in curve:
        # dig_station NWS curves are (value, ISO time); OM curves are reversed.
        if isinstance(first, (int, float)):
            value, text = float(first), second
            dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        else:
            text, value = first, float(second)
            dt = datetime.fromisoformat(str(text)).replace(tzinfo=tz)
        candidates.append((abs((dt - observed_at).total_seconds()), value))
    return min(candidates)[1] if candidates else None


def forecast_context(dig, info: dict, target_date: str, state: dict) -> dict:
    values_daily = []
    values_now = []
    details = []
    try:
        det = dig.fetch_openmeteo_det(info)
        daily = dig.daily_value_from_det(det, target_date, "high")
        curve = dig.hourly_curve_from_det(det, target_date)
        now_value, _ = dig.nearest_forecast_value(
            curve, state["latest_at"], info["tz"]
        )
        if daily is not None:
            values_daily.append(float(daily))
            details.append(f"OMdaily={daily:.1f}")
        if now_value is not None:
            values_now.append(float(now_value))
    except Exception:
        pass
    try:
        families = dig.fetch_model_family_payload(info)
        daily_values = dig.model_family_daily_values(
            families, target_date, "high"
        )
        curves = dig.model_family_hourly_curves(families)
        now_values = dig.nearest_model_family_values(
            curves, state["latest_at"], info["tz"]
        )
        values_daily.extend(float(value) for value in daily_values.values())
        values_now.extend(float(value) for value in now_values.values())
        details.extend(
            f"{dig.MODEL_SHORT_NAMES.get(name, name)}={value:.1f}"
            for name, value in daily_values.items()
        )
    except Exception:
        pass
    try:
        nws_extreme, _, nws_curve = dig.fetch_nws(info, "high", target_date)
        if nws_extreme[0] is not None:
            values_daily.append(float(nws_extreme[0]))
            details.append(f"NWS={nws_extreme[0]:.1f}")
        nws_now, _ = dig.nearest_nws_value(nws_curve, state["latest_at"])
        if nws_now is not None:
            values_now.append(float(nws_now))
    except Exception:
        pass

    daily_consensus = statistics.median(values_daily) if values_daily else None
    now_consensus = statistics.median(values_now) if values_now else None
    residual = (
        state["latest_temp_f"] - now_consensus
        if now_consensus is not None
        else 0.0
    )
    return {
        "forecast_models_n": len(values_daily),
        "forecast_daily_consensus_f": daily_consensus,
        "forecast_now_consensus_f": now_consensus,
        "tracking_residual_f": residual,
        "forecast_details": "; ".join(details),
    }


def raw_interval(low: float, high: float) -> tuple[float, float]:
    return (
        -math.inf if low <= -900 else low - 0.5,
        math.inf if high >= 900 else high + 0.5,
    )


def verify_exact_quote(ticker: str, fallback: dict) -> dict:
    response = requests.get(
        f"{KALSHI_BASE}/markets/{ticker}",
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": USER_AGENT,
        },
        timeout=20,
    )
    response.raise_for_status()
    market = response.json().get("market", {})
    if market.get("ticker") != ticker:
        raise RuntimeError("exact quote returned wrong ticker")

    def cents(name: str) -> float:
        dollars = market.get(f"{name}_dollars")
        if dollars is not None:
            return round(float(dollars) * 100, 4)
        value = market.get(name)
        if value is None:
            return math.nan
        return float(value)

    return {
        **fallback,
        "yes_bid": cents("yes_bid"),
        "yes_ask": cents("yes_ask"),
        "no_bid": cents("no_bid"),
        "no_ask": cents("no_ask"),
        "quote_verified_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def potential_buckets(buckets: list[dict], state: dict, args) -> list[dict]:
    potential = []
    for bucket in buckets:
        low = float(bucket["low"])
        high = float(bucket["high"])
        raw_low, raw_high = raw_interval(low, high)
        if math.isfinite(raw_high):
            clearance = state["observed_high_f"] - raw_high
            gap = raw_high - state["observed_high_f"]
            if clearance >= args.passed_safety_f:
                potential.append(
                    {
                        **bucket,
                        "pre_signal": "PASSED",
                        "boundary_f": raw_high,
                        "boundary_gap_f": -clearance,
                    }
                )
            elif 0 < gap <= args.near_boundary_f:
                potential.append(
                    {
                        **bucket,
                        "pre_signal": "NEAR_BOUNDARY",
                        "boundary_f": raw_high,
                        "boundary_gap_f": gap,
                    }
                )
    return potential


def containing_bucket(buckets: list[dict], value: float) -> dict | None:
    for bucket in buckets:
        raw_low, raw_high = raw_interval(
            float(bucket["low"]), float(bucket["high"])
        )
        if raw_low <= value < raw_high:
            return bucket
    return None


def initialize_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS live_observations (
          id INTEGER PRIMARY KEY,
          scan_at TEXT NOT NULL,
          icao TEXT NOT NULL,
          target_date TEXT NOT NULL,
          latest_at TEXT,
          latest_temp_f REAL,
          observed_high_f REAL,
          high_at TEXT,
          rise_15m_f REAL,
          rise_30m_f REAL,
          age_minutes REAL,
          observations_n INTEGER
        );
        CREATE TABLE IF NOT EXISTS live_quotes (
          id INTEGER PRIMARY KEY,
          scan_at TEXT NOT NULL,
          icao TEXT NOT NULL,
          target_date TEXT NOT NULL,
          ticker TEXT NOT NULL,
          label TEXT,
          yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL,
          quote_verified_at TEXT
        );
        CREATE TABLE IF NOT EXISTS live_signals (
          id INTEGER PRIMARY KEY,
          scan_at TEXT NOT NULL,
          signal TEXT NOT NULL,
          icao TEXT NOT NULL,
          city TEXT,
          target_date TEXT NOT NULL,
          ticker TEXT NOT NULL,
          label TEXT,
          observed_high_f REAL,
          latest_temp_f REAL,
          rise_30m_f REAL,
          boundary_f REAL,
          boundary_gap_f REAL,
          projected_high_f REAL,
          tracking_residual_f REAL,
          yes_ask REAL,
          no_ask REAL,
          notes TEXT
        );
        """
    )
    return conn


def finite_or_none(value):
    return value if value is not None and math.isfinite(value) else None


def log_scan(conn: sqlite3.Connection, scan_at: str, observations, quotes, signals):
    conn.executemany(
        """
        INSERT INTO live_observations
        (scan_at,icao,target_date,latest_at,latest_temp_f,observed_high_f,
         high_at,rise_15m_f,rise_30m_f,age_minutes,observations_n)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                scan_at,
                row["icao"],
                row["target_date"],
                row["latest_at"],
                row["latest_temp_f"],
                row["observed_high_f"],
                row["high_at"],
                finite_or_none(row["rise_15m_f"]),
                finite_or_none(row["rise_30m_f"]),
                row["age_minutes"],
                row["observations_n"],
            )
            for row in observations
        ],
    )
    conn.executemany(
        """
        INSERT INTO live_quotes
        (scan_at,icao,target_date,ticker,label,yes_bid,yes_ask,no_bid,no_ask,
         quote_verified_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                scan_at, row["icao"], row["target_date"], row["ticker"],
                row["label"], row["yes_bid"], row["yes_ask"], row["no_bid"],
                row["no_ask"], row["quote_verified_at"],
            )
            for row in quotes
        ],
    )
    conn.executemany(
        """
        INSERT INTO live_signals
        (scan_at,signal,icao,city,target_date,ticker,label,observed_high_f,
         latest_temp_f,rise_30m_f,boundary_f,boundary_gap_f,projected_high_f,
         tracking_residual_f,yes_ask,no_ask,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                scan_at, row["signal"], row["icao"], row["city"],
                row["target_date"], row["ticker"], row["label"],
                row["observed_high_f"], row["latest_temp_f"],
                finite_or_none(row["rise_30m_f"]),
                finite_or_none(row.get("boundary_f")),
                finite_or_none(row.get("boundary_gap_f")),
                finite_or_none(row.get("projected_high_f")),
                finite_or_none(row.get("tracking_residual_f")),
                row["yes_ask"], row["no_ask"], row["notes"],
            )
            for row in signals
        ],
    )
    conn.commit()


def scan_station(icao, info, dig, tev, args, scan_at):
    target_date = datetime.now(ZoneInfo(info["tz"])).date().isoformat()
    rows = fetch_day_observations(icao, info, args.history_hours)
    state = observation_state(rows, args.stale_observation_minutes)
    observation = {
        "icao": icao,
        "target_date": target_date,
        **state,
        "latest_at": state["latest_at"].isoformat(),
        "high_at": state["high_at"].isoformat(),
    }
    if state["stale"]:
        return observation, [], [], f"stale observation ({state['age_minutes']:.0f}m)"

    buckets = dig.fetch_kalshi(icao, info, "high", target_date)
    if not buckets:
        return observation, [], [], "no high buckets"
    potentials = potential_buckets(buckets, state, args)

    # Forecast calls are the expensive part, so only make them if an observed
    # boundary is nearby or a cheap YES bucket exists.
    cheap_yes = [bucket for bucket in buckets if bucket["yes"] <= args.max_watch_yes_ask]
    context = None
    if potentials or cheap_yes:
        context = forecast_context(dig, info, target_date, state)
    if context is None:
        context = {
            "forecast_models_n": 0,
            "forecast_daily_consensus_f": None,
            "forecast_now_consensus_f": None,
            "tracking_residual_f": 0.0,
            "forecast_details": "",
        }
    projection = None
    if context["forecast_daily_consensus_f"] is not None:
        adjustment = max(
            -3.0,
            min(
                3.0,
                context["tracking_residual_f"]
                * args.projection_residual_weight,
            ),
        )
        projection = max(
            state["observed_high_f"],
            context["forecast_daily_consensus_f"] + adjustment,
        )
        projected_bucket = containing_bucket(buckets, projection)
        if projected_bucket is not None and projected_bucket["yes"] <= args.max_watch_yes_ask:
            watch_signal = (
                "CURRENT_HIGH_BUCKET"
                if abs(projection - state["observed_high_f"]) < 0.05
                else "PROJECTED_HIGH_WATCH"
            )
            potentials.append(
                {
                    **projected_bucket,
                    "pre_signal": watch_signal,
                    "boundary_f": math.nan,
                    "boundary_gap_f": math.nan,
                }
            )

    deduped = {}
    priority = {
        "PASSED": 4,
        "NEAR_BOUNDARY": 3,
        "CURRENT_HIGH_BUCKET": 2,
        "PROJECTED_HIGH_WATCH": 1,
    }
    for item in potentials:
        ticker = item["ticker"]
        if ticker not in deduped or priority[item["pre_signal"]] > priority[deduped[ticker]["pre_signal"]]:
            deduped[ticker] = item

    quotes = []
    signals = []
    for item in deduped.values():
        quote = verify_exact_quote(item["ticker"], item)
        quotes.append({"icao": icao, "target_date": target_date, **quote})
        signal = item["pre_signal"]
        if signal in ("PASSED", "NEAR_BOUNDARY") and quote["no_ask"] > args.max_passed_no_ask:
            continue
        if signal == "NEAR_BOUNDARY":
            rising = (
                math.isfinite(state["rise_30m_f"])
                and state["rise_30m_f"] >= args.min_rise_30m_f
            )
            near_high = (
                state["observed_high_f"] - state["latest_temp_f"]
                <= args.near_high_f
            )
            projection_clears = (
                projection is not None
                and projection >= item["boundary_f"] + args.passed_safety_f
            )
            if not (rising and near_high and projection_clears):
                continue
        if signal in ("CURRENT_HIGH_BUCKET", "PROJECTED_HIGH_WATCH") and quote["yes_ask"] > args.max_watch_yes_ask:
            continue
        notes = (
            f"obs_age={state['age_minutes']:.0f}m; "
            f"high_raw={state['high_raw']}; "
            f"models={context['forecast_models_n']}; "
            f"forecast={context['forecast_daily_consensus_f']}; "
            f"now_residual={context['tracking_residual_f']:+.2f}; "
            f"{context['forecast_details']}"
        )
        signals.append(
            {
                "signal": signal,
                "icao": icao,
                "city": info["name"],
                "target_date": target_date,
                "ticker": item["ticker"],
                "label": quote["label"],
                "observed_high_f": state["observed_high_f"],
                "latest_temp_f": state["latest_temp_f"],
                "rise_30m_f": state["rise_30m_f"],
                "boundary_f": item["boundary_f"],
                "boundary_gap_f": item["boundary_gap_f"],
                "projected_high_f": projection,
                "tracking_residual_f": context["tracking_residual_f"],
                "yes_bid": quote["yes_bid"],
                "yes_ask": quote["yes_ask"],
                "no_bid": quote["no_bid"],
                "no_ask": quote["no_ask"],
                "quote_verified_at": quote["quote_verified_at"],
                "notes": notes,
            }
        )
    return observation, quotes, signals, ""


def print_signals(signals: list[dict]) -> None:
    if not signals:
        print("\nNo trial signals found.")
        return
    order = {
        "PASSED": 0,
        "NEAR_BOUNDARY": 1,
        "CURRENT_HIGH_BUCKET": 2,
        "PROJECTED_HIGH_WATCH": 3,
    }
    signals.sort(key=lambda row: (order[row["signal"]], row["no_ask"]))
    print(
        f"\n{'Signal':<22} {'ICAO':<5} {'City':<16} {'High':>6} "
        f"{'Now':>6} {'30m':>6} {'Bound':>6} {'Gap':>6} {'Proj':>6} "
        f"{'YES':>5} {'NO':>5} Bucket"
    )
    print("-" * 135)
    for row in signals:
        def fmt(value):
            return "--" if value is None or not math.isfinite(value) else f"{value:.1f}"
        print(
            f"{row['signal']:<22} {row['icao']:<5} {row['city']:<16} "
            f"{row['observed_high_f']:>6.1f} {row['latest_temp_f']:>6.1f} "
            f"{fmt(row['rise_30m_f']):>6} {fmt(row['boundary_f']):>6} "
            f"{fmt(row['boundary_gap_f']):>6} "
            f"{fmt(row['projected_high_f']):>6} "
            f"{row['yes_ask']:>5.1f} {row['no_ask']:>5.1f} {row['label']}"
        )
        print(f"      {row['notes']}")


def write_csv(path: Path, rows: list[dict]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0]) if rows else [
        "signal", "icao", "city", "target_date", "ticker", "label"
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def one_scan(stations, dig, tev, args, conn):
    scan_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    print(f"\nScanning {len(stations)} stations at {scan_at}")
    observations, quotes, signals, warnings = [], [], [], []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                scan_station, icao, info, dig, tev, args, scan_at
            ): icao
            for icao, info in stations.items()
        }
        completed = 0
        for future in as_completed(futures):
            completed += 1
            icao = futures[future]
            try:
                observation, station_quotes, station_signals, warning = future.result()
                observations.append(observation)
                quotes.extend(station_quotes)
                signals.extend(station_signals)
                if warning:
                    warnings.append(f"{icao}: {warning}")
                print(
                    f"  [{completed:>2}/{len(stations)}] {icao}: "
                    f"{len(station_signals)} signal(s)"
                )
            except Exception as exc:
                warnings.append(f"{icao}: {exc}")
                print(f"  [{completed:>2}/{len(stations)}] {icao}: failed")
    log_scan(conn, scan_at, observations, quotes, signals)
    print_signals(signals)
    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"  - {warning}")
    if args.csv:
        write_csv(args.csv, signals)
        print(f"Wrote {len(signals)} signals to {args.csv.expanduser().resolve()}")
    print(
        f"\nLogged {len(observations)} observations, {len(quotes)} exact quotes, "
        f"and {len(signals)} signals."
    )


def run(args: argparse.Namespace) -> int:
    project = args.project_dir.expanduser().resolve()
    all_stations, dig, tev = load_project(project)
    if args.stations:
        requested = {
            value.strip().upper()
            for value in args.stations.split(",")
            if value.strip()
        }
        stations = {
            icao: info
            for icao, info in all_stations.items()
            if icao in requested
        }
        missing = sorted(requested - set(stations))
        if missing:
            raise RuntimeError("unknown station(s): " + ", ".join(missing))
    else:
        stations = all_stations
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else project / "live_high_boundary_trial.db"
    )
    with closing(initialize_db(db_path)) as conn:
        while True:
            one_scan(stations, dig, tev, args, conn)
            if not args.watch:
                break
            interval = max(60.0, args.interval)
            print(f"\nWaiting {interval:g} seconds for the next scan...")
            time.sleep(interval)
    print(f"Trial log database: {db_path}")
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except (RuntimeError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
