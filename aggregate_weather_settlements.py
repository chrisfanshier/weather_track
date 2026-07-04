#!/usr/bin/env python3
"""Add official CLI temperatures and Kalshi outcomes to a weather_track SQLite DB.

The input database must contain tracker.py's ``kalshi_snapshots`` table. This
script fetches parsed NWS Daily Climate Report (CLI) data from the Iowa
Environmental Mesonet archive, stores final daily highs/lows, identifies each
event's winning temperature bucket, and marks every contract YES/NO.

Only completed local calendar days are resolved. Rerunning is safe: rows are
updated in place and newly completed dates are added.

Examples:
    python aggregate_weather_settlements.py
    python aggregate_weather_settlements.py --db weather_track_download.db
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


IEM_CLI_URL = "https://mesonet.agron.iastate.edu/json/cli.py"
USER_AGENT = "weather-track-settlement-aggregator/1.0"


@dataclass(frozen=True)
class Station:
    name: str
    timezone: str


STATIONS = {
    "KATL": Station("Atlanta", "America/New_York"),
    "KAUS": Station("Austin", "America/Chicago"),
    "KBOS": Station("Boston", "America/New_York"),
    "KMDW": Station("Chicago", "America/Chicago"),
    "KDFW": Station("Dallas", "America/Chicago"),
    "KDEN": Station("Denver", "America/Denver"),
    "KIAH": Station("Houston", "America/Chicago"),
    "KLAS": Station("Las Vegas", "America/Los_Angeles"),
    "KLAX": Station("Los Angeles", "America/Los_Angeles"),
    "KMIA": Station("Miami", "America/New_York"),
    "KMSP": Station("Minneapolis", "America/Chicago"),
    "KMSY": Station("New Orleans", "America/Chicago"),
    "KNYC": Station("New York City", "America/New_York"),
    "KOKC": Station("Oklahoma City", "America/Chicago"),
    "KPHL": Station("Philadelphia", "America/New_York"),
    "KPHX": Station("Phoenix", "America/Phoenix"),
    "KSAT": Station("San Antonio", "America/Chicago"),
    "KSFO": Station("San Francisco", "America/Los_Angeles"),
    "KSEA": Station("Seattle", "America/Los_Angeles"),
    "KDCA": Station("Washington DC", "America/New_York"),
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS cli_actual_temperatures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    icao            TEXT NOT NULL,
    target_date     TEXT NOT NULL,
    station_name    TEXT,
    high_f          REAL,
    low_f           REAL,
    high_time       TEXT,
    low_time        TEXT,
    nws_product_id  TEXT,
    nws_product_url TEXT,
    source          TEXT NOT NULL DEFAULT 'NWS CLI via IEM',
    fetched_at      TEXT NOT NULL,
    UNIQUE (icao, target_date)
);

CREATE INDEX IF NOT EXISTS idx_cli_actual_date
    ON cli_actual_temperatures(target_date, icao);

CREATE TABLE IF NOT EXISTS market_resolutions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    icao                TEXT NOT NULL,
    target_date         TEXT NOT NULL,
    market_type         TEXT NOT NULL,
    actual_temp_f       REAL,
    winning_ticker      TEXT,
    winning_label       TEXT,
    winning_bucket_low  REAL,
    winning_bucket_high REAL,
    contracts_n         INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL,
    detail              TEXT,
    resolved_at         TEXT NOT NULL,
    UNIQUE (icao, target_date, market_type)
);

CREATE INDEX IF NOT EXISTS idx_market_resolution_status
    ON market_resolutions(status, target_date, icao);

CREATE TABLE IF NOT EXISTS contract_outcomes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    icao            TEXT NOT NULL,
    target_date     TEXT NOT NULL,
    market_type     TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    label           TEXT,
    bucket_low      REAL,
    bucket_high     REAL,
    actual_temp_f   REAL NOT NULL,
    yes_won         INTEGER NOT NULL CHECK (yes_won IN (0, 1)),
    resolved_at     TEXT NOT NULL,
    UNIQUE (ticker)
);

CREATE INDEX IF NOT EXISTS idx_contract_outcome_event
    ON contract_outcomes(target_date, icao, market_type);
"""


def parse_args() -> argparse.Namespace:
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=base / "weather_track_download.db",
        help="SQLite DB containing kalshi_snapshots (default: next to script)",
    )
    parser.add_argument(
        "--start-date",
        help="Optional earliest target date, YYYY-MM-DD",
    )
    parser.add_argument(
        "--end-date",
        help="Optional latest target date, YYYY-MM-DD; incomplete days are still skipped",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.15,
        help="Seconds between IEM requests (default: 0.15)",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Clear and regenerate only the three settlement-derived tables",
    )
    return parser.parse_args()


def fetch_json(url: str, attempts: int = 4) -> dict:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=45) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise
        except URLError:
            if attempt == attempts - 1:
                raise
        time.sleep(2**attempt)
    raise RuntimeError("request retry loop ended unexpectedly")


def cli_rows(icao: str, year: int) -> list[dict]:
    url = f"{IEM_CLI_URL}?{urlencode({'station': icao, 'year': year})}"
    payload = fetch_json(url)
    rows = payload.get("results", payload.get("data", []))
    if not isinstance(rows, list):
        raise ValueError(f"unexpected IEM response for {icao} {year}")
    return rows


def valid_temperature(value) -> float | None:
    if value in (None, "", "M", "T"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if -100 <= number <= 150 else None


def completed_local_day(target_date: str, timezone_name: str) -> bool:
    local_today = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
    return target_date < local_today


TICKER_DATE_RE = re.compile(
    r"-(?P<year>\d{2})(?P<month>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
    r"(?P<day>\d{2})-",
    re.IGNORECASE,
)
MONTHS = {
    name: number
    for number, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
         "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        1,
    )
}


def date_from_ticker(ticker: str) -> str | None:
    """Return the contract event date, which is authoritative over CSV target_date."""
    match = TICKER_DATE_RE.search(ticker or "")
    if not match:
        return None
    year = 2000 + int(match.group("year"))
    month = MONTHS[match.group("month").upper()]
    day = int(match.group("day"))
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def ticker_date_token(target_date: str) -> str:
    date = datetime.strptime(target_date, "%Y-%m-%d")
    return date.strftime("-%y%b%d-").upper()


def wanted_dates(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, set[str]]:
    rows = conn.execute(
        """
        SELECT DISTINCT icao, ticker
        FROM kalshi_snapshots
        ORDER BY icao, ticker
        """
    )
    result: dict[str, set[str]] = {}
    for icao, ticker in rows:
        target_date = date_from_ticker(ticker)
        if target_date is None or icao not in STATIONS:
            continue
        if args.start_date and target_date < args.start_date:
            continue
        if args.end_date and target_date > args.end_date:
            continue
        if completed_local_day(target_date, STATIONS[icao].timezone):
            result.setdefault(icao, set()).add(target_date)
    return result


def save_cli_actual(
    conn: sqlite3.Connection,
    icao: str,
    row: dict,
    fetched_at: str,
) -> bool:
    high = valid_temperature(row.get("high"))
    low = valid_temperature(row.get("low"))
    if high is None and low is None:
        return False
    product_link = row.get("link")
    if product_link and product_link.startswith("/"):
        product_link = f"https://mesonet.agron.iastate.edu{product_link}"
    conn.execute(
        """
        INSERT INTO cli_actual_temperatures (
            icao, target_date, station_name, high_f, low_f, high_time, low_time,
            nws_product_id, nws_product_url, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(icao, target_date) DO UPDATE SET
            station_name=excluded.station_name,
            high_f=excluded.high_f,
            low_f=excluded.low_f,
            high_time=excluded.high_time,
            low_time=excluded.low_time,
            nws_product_id=excluded.nws_product_id,
            nws_product_url=excluded.nws_product_url,
            fetched_at=excluded.fetched_at
        """,
        (
            icao,
            row["valid"],
            row.get("name"),
            high,
            low,
            row.get("high_time"),
            row.get("low_time"),
            row.get("product"),
            product_link,
            fetched_at,
        ),
    )
    return True


def finite_bound(value, fallback: float) -> float:
    if value is None:
        return fallback
    number = float(value)
    if math.isnan(number):
        return fallback
    return number


def contains_temperature(low, high, actual: float) -> bool:
    # tracker.py stores open-ended buckets as -999/999. Null is treated as
    # unbounded as a defensive fallback for other CSV producers.
    lower = finite_bound(low, -math.inf)
    upper = finite_bound(high, math.inf)
    return lower <= actual <= upper


def event_contracts(
    conn: sqlite3.Connection,
    icao: str,
    target_date: str,
    market_type: str,
) -> list[sqlite3.Row]:
    # CSV target_date is not reliable: tracker exports can label adjacent-day
    # contracts with the polling date. The ticker's YYMONDD token is the actual
    # event date. Bucket definitions do not change between snapshots.
    token = f"%{ticker_date_token(target_date)}%"
    return conn.execute(
        """
        SELECT k.ticker, k.label, k.bucket_low, k.bucket_high
        FROM kalshi_snapshots AS k
        JOIN (
            SELECT ticker, MAX(id) AS newest_id
            FROM kalshi_snapshots
            WHERE icao=? AND market_type=? AND UPPER(ticker) LIKE ?
            GROUP BY ticker
        ) AS latest ON latest.newest_id = k.id
        ORDER BY k.bucket_low, k.bucket_high, k.ticker
        """,
        (icao, market_type, token),
    ).fetchall()


def resolve_event(
    conn: sqlite3.Connection,
    icao: str,
    target_date: str,
    market_type: str,
    actual: float,
    resolved_at: str,
) -> tuple[str, int]:
    contracts = event_contracts(conn, icao, target_date, market_type)
    winners = [
        row
        for row in contracts
        if contains_temperature(row["bucket_low"], row["bucket_high"], actual)
    ]

    if len(winners) == 1:
        status = "resolved"
        detail = None
        winner = winners[0]
    elif not contracts:
        status = "no_contracts"
        detail = "No contract rows found"
        winner = None
    elif not winners:
        status = "no_matching_bucket"
        detail = f"No bucket contains actual temperature {actual:g}F"
        winner = None
    else:
        status = "ambiguous"
        detail = f"{len(winners)} overlapping buckets contain {actual:g}F"
        winner = None

    conn.execute(
        """
        INSERT INTO market_resolutions (
            icao, target_date, market_type, actual_temp_f, winning_ticker,
            winning_label, winning_bucket_low, winning_bucket_high,
            contracts_n, status, detail, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(icao, target_date, market_type) DO UPDATE SET
            actual_temp_f=excluded.actual_temp_f,
            winning_ticker=excluded.winning_ticker,
            winning_label=excluded.winning_label,
            winning_bucket_low=excluded.winning_bucket_low,
            winning_bucket_high=excluded.winning_bucket_high,
            contracts_n=excluded.contracts_n,
            status=excluded.status,
            detail=excluded.detail,
            resolved_at=excluded.resolved_at
        """,
        (
            icao,
            target_date,
            market_type,
            actual,
            winner["ticker"] if winner else None,
            winner["label"] if winner else None,
            winner["bucket_low"] if winner else None,
            winner["bucket_high"] if winner else None,
            len(contracts),
            status,
            detail,
            resolved_at,
        ),
    )

    # Contract outcomes are trustworthy only when exactly one bucket wins.
    if status == "resolved":
        for contract in contracts:
            yes_won = int(contract["ticker"] == winner["ticker"])
            conn.execute(
                """
                INSERT INTO contract_outcomes (
                    icao, target_date, market_type, ticker, label, bucket_low,
                    bucket_high, actual_temp_f, yes_won, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    icao=excluded.icao,
                    target_date=excluded.target_date,
                    market_type=excluded.market_type,
                    label=excluded.label,
                    bucket_low=excluded.bucket_low,
                    bucket_high=excluded.bucket_high,
                    actual_temp_f=excluded.actual_temp_f,
                    yes_won=excluded.yes_won,
                    resolved_at=excluded.resolved_at
                """,
                (
                    icao,
                    target_date,
                    market_type,
                    contract["ticker"],
                    contract["label"],
                    contract["bucket_low"],
                    contract["bucket_high"],
                    actual,
                    yes_won,
                    resolved_at,
                ),
            )
    return status, len(contracts)


def check_input(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kalshi_snapshots'"
    ).fetchone()
    if not table:
        raise RuntimeError("database does not contain a kalshi_snapshots table")
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(kalshi_snapshots)")
    }
    required = {
        "id", "icao", "target_date", "ticker", "label",
        "bucket_low", "bucket_high", "market_type",
    }
    missing = required - columns
    if missing:
        raise RuntimeError(
            "kalshi_snapshots is missing required columns: " + ", ".join(sorted(missing))
        )


def run(args: argparse.Namespace) -> int:
    db_path = args.db.expanduser().resolve()
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    actuals_saved = events_resolved = events_problem = contracts_scored = 0

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        check_input(conn)
        conn.executescript(SCHEMA)
        if args.rebuild:
            with conn:
                conn.execute("DELETE FROM contract_outcomes")
                conn.execute("DELETE FROM market_resolutions")
                conn.execute("DELETE FROM cli_actual_temperatures")
            print("Cleared existing settlement-derived rows for a clean rebuild.")
        dates_by_station = wanted_dates(conn, args)
        if not dates_by_station:
            print("No completed Kalshi target dates found.")
            return 0

        print(
            f"Found {sum(map(len, dates_by_station.values()))} completed "
            f"station-dates across {len(dates_by_station)} stations."
        )

        for icao, target_dates in sorted(dates_by_station.items()):
            years = sorted({int(date[:4]) for date in target_dates})
            archive: dict[str, dict] = {}
            try:
                for year in years:
                    for row in cli_rows(icao, year):
                        valid = row.get("valid")
                        if valid in target_dates:
                            archive[valid] = row
                    time.sleep(max(args.pause, 0))
            except (HTTPError, URLError, ValueError, json.JSONDecodeError) as exc:
                print(f"  {icao} {STATIONS[icao].name}: fetch failed: {exc}")
                continue

            station_actuals = 0
            station_resolved = 0
            with conn:
                for target_date in sorted(target_dates):
                    row = archive.get(target_date)
                    if not row:
                        continue
                    if save_cli_actual(conn, icao, row, fetched_at):
                        actuals_saved += 1
                        station_actuals += 1

                    for market_type, field in (("high", "high"), ("low", "low")):
                        actual = valid_temperature(row.get(field))
                        if actual is None:
                            continue
                        status, contract_count = resolve_event(
                            conn,
                            icao,
                            target_date,
                            market_type,
                            actual,
                            fetched_at,
                        )
                        if status == "resolved":
                            events_resolved += 1
                            station_resolved += 1
                            contracts_scored += contract_count
                        else:
                            events_problem += 1

            print(
                f"  {icao} {STATIONS[icao].name:<18} "
                f"actuals:{station_actuals:>3}  events resolved:{station_resolved:>3}"
            )

        counts = {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in (
                "cli_actual_temperatures",
                "market_resolutions",
                "contract_outcomes",
            )
        }

    print("\nSettlement aggregation complete")
    print(f"  Database:             {db_path}")
    print(f"  CLI actuals updated:  {actuals_saved:,}")
    print(f"  Events resolved:      {events_resolved:,}")
    print(f"  Events needing review:{events_problem:>7,}")
    print(f"  Contracts scored:     {contracts_scored:,}")
    print("  Total table rows:")
    for table, count in counts.items():
        print(f"    {table}: {count:,}")
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (sqlite3.Error, RuntimeError) as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
