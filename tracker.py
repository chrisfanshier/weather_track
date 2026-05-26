"""tracker.py — Multi-station Kalshi + NWS weather snapshot tracker

Polls 20 US cities and stores:
  - Kalshi market odds (for cities with active series)
  - NWS hourly forecast (~49 periods)
  - NWS text period forecast (~14 named periods)

Each run appends one timestamped snapshot per station to weather_track.db
and exports the new rows to date-partitioned CSV files under data/:
  data/kalshi/YYYY-MM-DD.csv
  data/nws_hourly/YYYY-MM-DD.csv
  data/nws_periods/YYYY-MM-DD.csv

Settlement source: NWS Daily Climatological Report (CLI) per Kalshi rulebooks.

Usage:
    python tracker.py

Runs automatically every 30 minutes via GitHub Actions.
"""

import csv
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ── Constants ─────────────────────────────────────────────────────────────────

DB_PATH            = Path(__file__).parent / "weather_track.db"
DATA_DIR           = Path(__file__).parent / "data"
KALSHI_BASE        = "https://external-api.kalshi.com/trade-api/v2"
NWS_GRIDPOINTS_BASE = "https://api.weather.gov/gridpoints"
NWS_HEADERS        = {"User-Agent": "(weather-tracker, research)"}
STATION_POLL_DELAY = 0.5   # seconds between stations (NWS rate limiting)

# ── Station config ─────────────────────────────────────────────────────────────
# Key: ICAO code used by NWS CLI report (Kalshi settlement source)
# Kalshi notes:
#   - Chicago resolves at Midway (KMDW), NOT O'Hare (KORD)
#   - NYC resolves at Central Park (KNYC), NOT JFK or LGA
#   - CLI day = 1:00 AM–12:59 AM local standard time (even during DST)
#   - Kalshi series guesses follow KXHIGH<city> pattern; returns [] if not active

STATIONS = {
    "KATL": {
        "name": "Atlanta",       "ghcnd": "USW00013874",
        "lat": 33.6367,  "lon": -84.4281,  "tz": "America/New_York",
        "nws_grid": "FFC/50,82",
        "kalshi_high": ["KXHIGHTATL"],   "kalshi_low": ["KXLOWTATL"],
    },
    "KAUS": {
        "name": "Austin",        "ghcnd": "USW00013904",
        "lat": 30.1945,  "lon": -97.6699,  "tz": "America/Chicago",
        "nws_grid": "EWX/159,88",
        "kalshi_high": ["KXHIGHAUS"],    "kalshi_low": ["KXLOWTAUS"],
    },
    "KBOS": {
        "name": "Boston",        "ghcnd": "USW00014739",
        "lat": 42.3656,  "lon": -71.0096,  "tz": "America/New_York",
        "nws_grid": "BOX/73,102",
        "kalshi_high": ["KXHIGHTBOS"],   "kalshi_low": ["KXLOWTBOS"],
    },
    "KMDW": {
        "name": "Chicago",       "ghcnd": "USW00014819",
        "lat": 41.7860,  "lon": -87.7522,  "tz": "America/Chicago",
        "nws_grid": "LOT/72,69",
        "kalshi_high": ["KXHIGHCHI"],    "kalshi_low": ["KXLOWTCHI"],
    },
    "KDFW": {
        "name": "Dallas",        "ghcnd": "USW00003927",
        "lat": 32.8998,  "lon": -97.0403,  "tz": "America/Chicago",
        "nws_grid": "FWD/80,109",
        "kalshi_high": ["KXHIGHTDAL"],   "kalshi_low": ["KXLOWTDAL"],
    },
    "KDEN": {
        "name": "Denver",        "ghcnd": "USW00093016",
        "lat": 39.8561,  "lon": -104.6737, "tz": "America/Denver",
        "nws_grid": "BOU/74,66",
        "kalshi_high": ["KXHIGHDEN"],    "kalshi_low": ["KXLOWTDEN"],
    },
    "KIAH": {
        "name": "Houston",       "ghcnd": "USW00012924",
        "lat": 29.9902,  "lon": -95.3368,  "tz": "America/Chicago",
        "nws_grid": "HGX/64,105",
        "kalshi_high": ["KXHIGHTHOU"],   "kalshi_low": ["KXLOWTHOU"],
    },
    "KLAS": {
        "name": "Las Vegas",     "ghcnd": "USW00023169",
        "lat": 36.0840,  "lon": -115.1537, "tz": "America/Los_Angeles",
        "nws_grid": "VEF/122,94",
        "kalshi_high": ["KXHIGHTLV"],    "kalshi_low": ["KXLOWTLV"],
    },
    "KLAX": {
        "name": "Los Angeles",   "ghcnd": "USW00023174",
        "lat": 33.9425,  "lon": -118.4081, "tz": "America/Los_Angeles",
        "nws_grid": "LOX/148,41",
        "kalshi_high": ["KXHIGHLAX"],    "kalshi_low": ["KXLOWTLAX"],
    },
    "KMIA": {
        "name": "Miami",         "ghcnd": "USW00012839",
        "lat": 25.7959,  "lon": -80.3187,  "tz": "America/New_York",
        "nws_grid": "MFL/105,51",
        "kalshi_high": ["KXHIGHMIA"],    "kalshi_low": ["KXLOWTMIA"],
    },
    "KMSP": {
        "name": "Minneapolis",   "ghcnd": "USW00014922",
        "lat": 44.8848,  "lon": -93.2223,  "tz": "America/Chicago",
        "nws_grid": "MPX/110,68",
        "kalshi_high": ["KXHIGHTMIN"],   "kalshi_low": ["KXLOWTMIN"],
    },
    "KMSY": {
        "name": "New Orleans",   "ghcnd": "USW00012916",
        "lat": 29.9934,  "lon": -90.2580,  "tz": "America/Chicago",
        "nws_grid": "LIX/60,90",
        "kalshi_high": ["KXHIGHTNOLA"],  "kalshi_low": ["KXLOWTNOLA"],
    },
    "KNYC": {
        # Central Park Zoo station — NOT JFK/LGA
        "name": "New York City", "ghcnd": "USW00094728",
        "lat": 40.7789,  "lon": -73.9692,  "tz": "America/New_York",
        "nws_grid": "OKX/34,45",
        "kalshi_high": ["KXHIGHNY"],     "kalshi_low": ["KXLOWTNYC"],
    },
    "KOKC": {
        "name": "Oklahoma City", "ghcnd": "USW00013967",
        "lat": 35.3931,  "lon": -97.6008,  "tz": "America/Chicago",
        "nws_grid": "OUN/94,90",
        "kalshi_high": ["KXHIGHTOKC"],   "kalshi_low": ["KXLOWTOKC"],
    },
    "KPHL": {
        "name": "Philadelphia",  "ghcnd": "USW00014741",
        "lat": 39.8719,  "lon": -75.2411,  "tz": "America/New_York",
        "nws_grid": "PHI/48,75",
        "kalshi_high": ["KXHIGHPHIL"],   "kalshi_low": ["KXLOWTPHIL"],
    },
    "KPHX": {
        # America/Phoenix: no DST — UTC-7 year-round
        "name": "Phoenix",       "ghcnd": "USW00023183",
        "lat": 33.4373,  "lon": -112.0078, "tz": "America/Phoenix",
        "nws_grid": "PSR/161,57",
        "kalshi_high": ["KXHIGHTPHX"],   "kalshi_low": ["KXLOWTPHX"],
    },
    "KSAT": {
        "name": "San Antonio",   "ghcnd": "USW00012921",
        "lat": 29.5337,  "lon": -98.4698,  "tz": "America/Chicago",
        "nws_grid": "EWX/127,59",
        "kalshi_high": ["KXHIGHTSATX"],  "kalshi_low": ["KXLOWTSATX"],
    },
    "KSFO": {
        "name": "San Francisco", "ghcnd": "USW00023234",
        "lat": 37.6213,  "lon": -122.3790, "tz": "America/Los_Angeles",
        "nws_grid": "MTR/85,98",
        "kalshi_high": ["KXHIGHTSFO"],   "kalshi_low": ["KXLOWTSFO"],
    },
    "KSEA": {
        "name": "Seattle",       "ghcnd": "USW00024217",
        "lat": 47.4502,  "lon": -122.3088, "tz": "America/Los_Angeles",
        "nws_grid": "SEW/124,61",
        "kalshi_high": ["KXHIGHTSEA"],   "kalshi_low": ["KXLOWTSEA"],
    },
    "KDCA": {
        "name": "Washington DC", "ghcnd": "USW00013743",
        "lat": 38.8512,  "lon": -77.0402,  "tz": "America/New_York",
        "nws_grid": "LWX/97,69",
        "kalshi_high": ["KXHIGHTDC"],    "kalshi_low": ["KXLOWTDC"],
    },
}

# ── Database ───────────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kalshi_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at      TEXT    NOT NULL,
            icao        TEXT    NOT NULL,
            target_date TEXT    NOT NULL,
            ticker      TEXT    NOT NULL,
            label       TEXT    NOT NULL,
            bucket_low  REAL,
            bucket_high REAL,
            yes_bid     INTEGER,
            yes_ask     INTEGER,
            no_bid      INTEGER,
            no_ask      INTEGER,
            volume      REAL,
            market_type TEXT    NOT NULL DEFAULT 'high'
        );
        CREATE INDEX IF NOT EXISTS idx_kalshi_run_icao
            ON kalshi_snapshots(run_at, icao);

        CREATE TABLE IF NOT EXISTS nws_hourly_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at      TEXT    NOT NULL,
            icao        TEXT    NOT NULL,
            valid_time  TEXT    NOT NULL,
            temp_f      REAL,
            precip_pct  INTEGER,
            short_fcst  TEXT,
            wind_speed  TEXT,
            is_daytime  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_nws_hourly_run_icao
            ON nws_hourly_snapshots(run_at, icao);

        CREATE TABLE IF NOT EXISTS nws_period_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at        TEXT    NOT NULL,
            icao          TEXT    NOT NULL,
            period_name   TEXT    NOT NULL,
            start_time    TEXT    NOT NULL,
            is_daytime    INTEGER,
            temp_f        REAL,
            short_fcst    TEXT,
            detailed_fcst TEXT
        );

        CREATE TABLE IF NOT EXISTS run_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at       TEXT    NOT NULL,
            duration_s   REAL,
            stations_ok  INTEGER,
            stations_err INTEGER,
            notes        TEXT
        );
    """)
    conn.commit()
    # Migrate: add market_type to existing DBs that predate this column
    try:
        conn.execute("ALTER TABLE kalshi_snapshots ADD COLUMN market_type TEXT NOT NULL DEFAULT 'high'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


# ── Kalshi helpers (adapted from mia_ev.py) ───────────────────────────────────

def parse_temp_range(text: str) -> tuple:
    t = text.lower()
    m = re.search(r'(\d+)[°\s]*or[°\s]*below', t)
    if m:
        return (-999.0, float(m.group(1)))
    m = re.search(r'(\d+)[°\s]*or[°\s]*above', t)
    if m:
        return (float(m.group(1)), 999.0)
    m = re.search(r'(\d+)[°\s]*(?:to|-)[°\s]*(\d+)', t)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    nums = [float(x) for x in re.findall(r'\d+\.?\d*', t)]
    if len(nums) >= 2:
        return (nums[0], nums[1])
    return (0, 0)


def parse_bucket(m: dict) -> dict | None:
    subtitle = m.get("subtitle") or m.get("yes_sub_title") or m.get("title") or ""

    def to_cents(field):
        val = m.get(field)
        if val is None:
            return 0
        try:
            return round(float(val) * 100)
        except (TypeError, ValueError):
            return 0

    yes_bid = to_cents("yes_bid_dollars")
    no_bid  = to_cents("no_bid_dollars")
    yes_ask = to_cents("yes_ask_dollars") or (100 - no_bid)
    no_ask  = to_cents("no_ask_dollars")  or (100 - yes_bid)
    volume  = float(m.get("volume_fp") or 0)
    ticker  = m.get("ticker", "")

    strike_type  = m.get("strike_type", "")
    floor_strike = m.get("floor_strike")

    if strike_type == "greater" and floor_strike is not None:
        low, high = float(floor_strike) + 1, 999.0
    elif strike_type == "less" and floor_strike is not None:
        low, high = -999.0, float(floor_strike) - 1
    else:
        low, high = parse_temp_range(subtitle)
        if low == 0 and high == 0:
            return None

    return {
        "ticker":  ticker,
        "label":   subtitle.strip(),
        "low":     low,
        "high":    high,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid":  no_bid,
        "no_ask":  no_ask,
        "volume":  volume,
    }


def fetch_kalshi_markets(series_list: list, target_dates: list) -> list:
    """
    Try each series in series_list. For the first series that returns open
    markets matching our target dates, parse and return bucket dicts tagged
    with target_date. Returns [] gracefully if no series is active.
    """
    date_tags = {
        datetime.strptime(d, "%Y-%m-%d").strftime("%d%b%y").upper(): d
        for d in target_dates
    }

    for series in series_list:
        url    = f"{KALSHI_BASE}/markets"
        params = {"series_ticker": series, "status": "open", "limit": 100}
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            raw = r.json().get("markets", [])
        except requests.exceptions.RequestException:
            continue

        if not raw:
            continue

        # Filter to markets whose event_ticker contains one of our date tags.
        # Fall back to all open markets in the series if none match (handles
        # series that embed dates differently in their tickers).
        dated = [m for m in raw if any(tag in m.get("event_ticker", "") for tag in date_tags)]
        if not dated:
            dated = raw

        buckets = []
        for market in dated:
            b = parse_bucket(market)
            if b is None:
                continue
            # Identify target_date from event_ticker; default to today
            event_ticker = market.get("event_ticker", "")
            b["target_date"] = next(
                (d for tag, d in date_tags.items() if tag in event_ticker),
                target_dates[0],
            )
            buckets.append(b)

        if buckets:
            return buckets

    return []


# ── NWS helpers ───────────────────────────────────────────────────────────────

def _nws_get(url: str, retries: int = 2, retry_delay: float = 2.0) -> requests.Response:
    """GET with simple retry for transient NWS 5xx errors."""
    for attempt in range(retries + 1):
        r = requests.get(url, headers=NWS_HEADERS, timeout=20)
        if r.status_code < 500:
            r.raise_for_status()
            return r
        if attempt < retries:
            time.sleep(retry_delay)
    r.raise_for_status()
    return r


def fetch_nws_hourly(nws_grid: str) -> list:
    """
    Fetch NWS hourly forecast. Returns list of dicts:
    valid_time, temp_f, precip_pct, short_fcst, wind_speed, is_daytime.
    """
    url = f"{NWS_GRIDPOINTS_BASE}/{nws_grid}/forecast/hourly"
    r   = _nws_get(url)
    rows = []
    for p in r.json()["properties"]["periods"]:
        temp = float(p["temperature"])
        if p.get("temperatureUnit") == "C":
            temp = temp * 9 / 5 + 32

        precip = None
        pop = p.get("probabilityOfPrecipitation")
        if pop and pop.get("value") is not None:
            precip = int(pop["value"])

        rows.append({
            "valid_time": p["startTime"],
            "temp_f":     round(temp, 1),
            "precip_pct": precip,
            "short_fcst": p.get("shortForecast", ""),
            "wind_speed": p.get("windSpeed", ""),
            "is_daytime": 1 if p.get("isDaytime") else 0,
        })
    return rows


def fetch_nws_periods(nws_grid: str) -> list:
    """
    Fetch NWS text period forecast (~14 named periods). Returns list of dicts:
    period_name, start_time, is_daytime, temp_f, short_fcst, detailed_fcst.
    """
    url = f"{NWS_GRIDPOINTS_BASE}/{nws_grid}/forecast"
    r   = _nws_get(url)
    rows = []
    for p in r.json()["properties"]["periods"]:
        temp = float(p["temperature"])
        if p.get("temperatureUnit") == "C":
            temp = temp * 9 / 5 + 32

        rows.append({
            "period_name":   p.get("name", ""),
            "start_time":    p.get("startTime", ""),
            "is_daytime":    1 if p.get("isDaytime") else 0,
            "temp_f":        round(temp, 1),
            "short_fcst":    p.get("shortForecast", ""),
            "detailed_fcst": p.get("detailedForecast", ""),
        })
    return rows


# ── Storage ───────────────────────────────────────────────────────────────────

def store_snapshot(
    conn: sqlite3.Connection,
    run_at: str,
    icao: str,
    kalshi_rows: list,
    hourly_rows: list,
    period_rows: list,
) -> None:
    with conn:
        if kalshi_rows:
            conn.executemany(
                """INSERT INTO kalshi_snapshots
                     (run_at, icao, target_date, ticker, label,
                      bucket_low, bucket_high, yes_bid, yes_ask,
                      no_bid, no_ask, volume, market_type)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (run_at, icao, r["target_date"], r["ticker"], r["label"],
                     r["low"], r["high"], r["yes_bid"], r["yes_ask"],
                     r["no_bid"], r["no_ask"], r["volume"], r.get("market_type", "high"))
                    for r in kalshi_rows
                ],
            )

        if hourly_rows:
            conn.executemany(
                """INSERT INTO nws_hourly_snapshots
                     (run_at, icao, valid_time, temp_f, precip_pct,
                      short_fcst, wind_speed, is_daytime)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [
                    (run_at, icao, r["valid_time"], r["temp_f"], r["precip_pct"],
                     r["short_fcst"], r["wind_speed"], r["is_daytime"])
                    for r in hourly_rows
                ],
            )

        if period_rows:
            conn.executemany(
                """INSERT INTO nws_period_snapshots
                     (run_at, icao, period_name, start_time, is_daytime,
                      temp_f, short_fcst, detailed_fcst)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [
                    (run_at, icao, r["period_name"], r["start_time"], r["is_daytime"],
                     r["temp_f"], r["short_fcst"], r["detailed_fcst"])
                    for r in period_rows
                ],
            )


# ── Per-station poll ───────────────────────────────────────────────────────────

def run_station(
    conn: sqlite3.Connection,
    run_at: str,
    icao: str,
    info: dict,
) -> dict:
    """
    Poll Kalshi + NWS for one station and persist results.
    Per-station failures are caught and surfaced in the returned status dict
    so that a single bad station does not abort the whole run.
    """
    status = {
        "icao":      icao,
        "name":      info["name"],
        "kalshi_n":  0,
        "hourly_n":  0,
        "periods_n": 0,
        "error":     None,
    }
    errors = []

    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    target_dates = [today, tomorrow]

    # --- Kalshi (high + low) ---
    kalshi_rows = []
    for market_type, series_key in [("high", "kalshi_high"), ("low", "kalshi_low")]:
        series = info.get(series_key, [])
        if not series:
            continue
        try:
            rows = fetch_kalshi_markets(series, target_dates)
            for r in rows:
                r["market_type"] = market_type
            kalshi_rows.extend(rows)
        except Exception as e:
            errors.append(f"Kalshi {market_type}: {e}")

    # --- NWS hourly ---
    hourly_rows = []
    try:
        hourly_rows = fetch_nws_hourly(info["nws_grid"])
    except Exception as e:
        errors.append(f"NWS hourly: {e}")

    # --- NWS periods ---
    period_rows = []
    try:
        period_rows = fetch_nws_periods(info["nws_grid"])
    except Exception as e:
        errors.append(f"NWS periods: {e}")

    # --- Persist whatever we collected ---
    try:
        store_snapshot(conn, run_at, icao, kalshi_rows, hourly_rows, period_rows)
    except Exception as e:
        errors.append(f"DB: {e}")

    status["kalshi_n"]  = len(kalshi_rows)
    status["hourly_n"]  = len(hourly_rows)
    status["periods_n"] = len(period_rows)
    if errors:
        status["error"] = " | ".join(errors)
    return status


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    run_at  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_start = time.time()

    print(f"tracker.py — {run_at}")
    print("─" * 68)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    results = []
    for icao, info in STATIONS.items():
        status = run_station(conn, run_at, icao, info)
        results.append(status)

        flag       = "✗" if status["error"] else "✓"
        kalshi_str = f"Kalshi:{status['kalshi_n']:>3}" if status["kalshi_n"] else "Kalshi: --"
        err_note   = f"  ERR: {status['error'][:55]}" if status["error"] else ""
        print(
            f"  {icao:<5}  {info['name']:<18}  {kalshi_str}  "
            f"Hrly:{status['hourly_n']:>3}  Per:{status['periods_n']:>2}  {flag}{err_note}"
        )

        time.sleep(STATION_POLL_DELAY)

    conn.close()
    duration = time.time() - t_start
    ok  = sum(1 for r in results if not r["error"])
    err = sum(1 for r in results if r["error"])

    print("─" * 68)
    print(f"Done. {ok}/{len(results)} OK, {err} errors.  "
          f"Elapsed: {duration:.1f}s  DB: {DB_PATH.name}")

    # Log this run and export CSVs
    conn2 = sqlite3.connect(DB_PATH)
    with conn2:
        conn2.execute(
            "INSERT INTO run_log (run_at, duration_s, stations_ok, stations_err) VALUES (?,?,?,?)",
            (run_at, round(duration, 2), ok, err),
        )
    csv_counts = export_run_to_csv(conn2, run_at)
    conn2.close()

    print(
        f"CSV rows appended — kalshi:{csv_counts.get('kalshi_snapshots', 0)}  "
        f"nws_hourly:{csv_counts.get('nws_hourly_snapshots', 0)}  "
        f"nws_periods:{csv_counts.get('nws_period_snapshots', 0)}"
    )


def export_run_to_csv(conn: sqlite3.Connection, run_at: str) -> dict:
    """Append this run's rows to date-partitioned CSV files under data/."""
    date_str = run_at[:10]
    tables = [
        ("kalshi_snapshots",     DATA_DIR / "kalshi"     / f"{date_str}.csv"),
        ("nws_hourly_snapshots", DATA_DIR / "nws_hourly" / f"{date_str}.csv"),
        ("nws_period_snapshots", DATA_DIR / "nws_periods"/ f"{date_str}.csv"),
    ]
    counts = {}
    for table, path in tables:
        path.parent.mkdir(parents=True, exist_ok=True)
        cur = conn.execute(
            f"SELECT * FROM {table} WHERE run_at = ?", (run_at,)  # noqa: S608
        )
        rows = cur.fetchall()
        counts[table] = len(rows)
        if not rows:
            continue
        col_names = [d[0] for d in cur.description]
        write_header = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(col_names)
            writer.writerows(rows)
    return counts


if __name__ == "__main__":
    main()
