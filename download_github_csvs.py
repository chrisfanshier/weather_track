#!/usr/bin/env python3
"""Download weather_track/data CSVs from GitHub and import them into SQLite.

Uses only the Python standard library.

Examples:
    python download_github_csvs.py
    python download_github_csvs.py --db C:\\weather\\weather_history.db
    python download_github_csvs.py --db weather.db --csv-dir downloaded_data
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DEFAULT_REPO = "chrisfanshier/weather_track"
DEFAULT_BRANCH = "main"
USER_AGENT = "weather-track-csv-importer/1.0"


@dataclass(frozen=True)
class Dataset:
    prefix: str
    table: str
    columns: tuple[str, ...]
    integer_columns: frozenset[str] = frozenset()
    real_columns: frozenset[str] = frozenset()
    key_columns: tuple[str, ...] = ()


DATASETS = (
    Dataset(
        "data/kalshi/",
        "kalshi_snapshots",
        (
            "run_at", "icao", "target_date", "ticker", "label", "bucket_low",
            "bucket_high", "yes_bid", "yes_ask", "no_bid", "no_ask", "volume",
            "market_type",
        ),
        frozenset({"yes_bid", "yes_ask", "no_bid", "no_ask"}),
        frozenset({"bucket_low", "bucket_high", "volume"}),
        ("run_at", "icao", "ticker"),
    ),
    Dataset(
        "data/nws_hourly/",
        "nws_hourly_snapshots",
        (
            "run_at", "icao", "valid_time", "temp_f", "precip_pct",
            "short_fcst", "wind_speed", "is_daytime",
        ),
        frozenset({"precip_pct", "is_daytime"}),
        frozenset({"temp_f"}),
        ("run_at", "icao", "valid_time"),
    ),
    Dataset(
        "data/nws_periods/",
        "nws_period_snapshots",
        (
            "run_at", "icao", "period_name", "start_time", "is_daytime",
            "temp_f", "short_fcst", "detailed_fcst",
        ),
        frozenset({"is_daytime"}),
        frozenset({"temp_f"}),
        ("run_at", "icao", "period_name", "start_time"),
    ),
    Dataset(
        "data/openmeteo/",
        "openmeteo_snapshots",
        ("run_at", "icao", "forecast_date", "high_f", "low_f"),
        real_columns=frozenset({"high_f", "low_f"}),
        key_columns=("run_at", "icao", "forecast_date"),
    ),
    Dataset(
        "data/openmeteo_ensemble/",
        "openmeteo_ensemble_snapshots",
        (
            "run_at", "icao", "forecast_date", "kind", "n_members", "mean_f",
            "sd_f", "p10_f", "p50_f", "p90_f", "min_f", "max_f",
        ),
        frozenset({"n_members"}),
        frozenset({"mean_f", "sd_f", "p10_f", "p50_f", "p90_f", "min_f", "max_f"}),
        ("run_at", "icao", "forecast_date", "kind"),
    ),
    Dataset(
        "data/model_family/",
        "model_family_snapshots",
        ("run_at", "icao", "forecast_date", "model_name", "high_f", "low_f"),
        real_columns=frozenset({"high_f", "low_f"}),
        key_columns=("run_at", "icao", "forecast_date", "model_name"),
    ),
    Dataset(
        "data/paper/scan_log/",
        "scan_log",
        (
            "run_at", "candidates_n", "consensus_pass_n", "entries_n",
            "exits_n", "skips_n", "notes",
        ),
        frozenset(
            {"candidates_n", "consensus_pass_n", "entries_n", "exits_n", "skips_n"}
        ),
        key_columns=("run_at",),
    ),
    Dataset(
        "data/paper/trade_skip_log/",
        "trade_skip_log",
        ("run_at", "station", "target_date", "ticker", "side", "reason", "detail"),
        key_columns=("run_at", "station", "target_date", "ticker", "side", "reason", "detail"),
    ),
)


def parse_args() -> argparse.Namespace:
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=base / "weather_track_download.db",
        help="SQLite output path (default: next to this script)",
    )
    parser.add_argument(
        "--csv-dir",
        type=Path,
        default=base / "downloaded_data",
        help="Local CSV mirror directory (default: next to this script)",
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub owner/repository")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="Git branch or tag")
    parser.add_argument(
        "--token",
        default=os.getenv("GITHUB_TOKEN"),
        help="Optional GitHub token; defaults to GITHUB_TOKEN",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Download and inspect every CSV even if GitHub reports no change",
    )
    return parser.parse_args()


def github_request(url: str, token: str | None, attempts: int = 4) -> bytes:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for attempt in range(attempts):
        try:
            with urlopen(Request(url, headers=headers), timeout=60) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise
        except URLError:
            if attempt == attempts - 1:
                raise
        time.sleep(2**attempt)
    raise RuntimeError("request retry loop ended unexpectedly")


def list_csvs(repo: str, branch: str, token: str | None) -> list[dict]:
    branch_ref = quote(branch, safe="")
    url = f"https://api.github.com/repos/{repo}/git/trees/{branch_ref}?recursive=1"
    payload = json.loads(github_request(url, token))
    if payload.get("truncated"):
        raise RuntimeError("GitHub truncated the repository tree; use a narrower repository")

    files = []
    for item in payload.get("tree", []):
        path = item.get("path", "")
        if (
            item.get("type") == "blob"
            and path.startswith("data/")
            and path.lower().endswith(".csv")
            and dataset_for_path(path) is not None
        ):
            files.append({"path": path, "sha": item["sha"]})
    return sorted(files, key=lambda item: item["path"])


def dataset_for_path(path: str) -> Dataset | None:
    for dataset in DATASETS:
        if path.startswith(dataset.prefix):
            return dataset
    return None


def sql_type(dataset: Dataset, column: str) -> str:
    if column in dataset.integer_columns:
        return "INTEGER"
    if column in dataset.real_columns:
        return "REAL"
    return "TEXT"


def initialize_database(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    for dataset in DATASETS:
        definitions = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
        definitions.extend(
            f'"{column}" {sql_type(dataset, column)}' for column in dataset.columns
        )
        unique = ", ".join(f'"{column}"' for column in dataset.key_columns)
        if unique:
            definitions.append(f"UNIQUE ({unique})")
        conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{dataset.table}" '
            f"({', '.join(definitions)})"
        )
        if "run_at" in dataset.columns:
            conn.execute(
                f'CREATE INDEX IF NOT EXISTS "idx_{dataset.table}_run_at" '
                f'ON "{dataset.table}" ("run_at")'
            )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS github_csv_imports (
            path        TEXT PRIMARY KEY,
            blob_sha    TEXT NOT NULL,
            content_sha TEXT NOT NULL,
            table_name  TEXT NOT NULL,
            rows_seen   INTEGER NOT NULL,
            rows_added  INTEGER NOT NULL,
            imported_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def local_path(csv_dir: Path, github_path: str) -> Path:
    relative = PurePosixPath(github_path).relative_to("data")
    return csv_dir.joinpath(*relative.parts)


def raw_url(repo: str, branch: str, path: str) -> str:
    encoded_path = "/".join(quote(part, safe="") for part in PurePosixPath(path).parts)
    return (
        f"https://raw.githubusercontent.com/{repo}/"
        f"{quote(branch, safe='')}/{encoded_path}"
    )


def convert(value: str | None, dataset: Dataset, column: str):
    if value is None or value.strip() == "":
        return None
    value = value.strip()
    if column in dataset.integer_columns:
        return int(float(value))
    if column in dataset.real_columns:
        return float(value)
    return value


def row_values(row: dict[str, str], dataset: Dataset) -> tuple:
    missing = [column for column in dataset.columns if column not in row]
    if missing:
        raise ValueError(f"CSV is missing columns: {', '.join(missing)}")
    return tuple(convert(row[column], dataset, column) for column in dataset.columns)


def import_csv(
    conn: sqlite3.Connection,
    path: Path,
    github_path: str,
    blob_sha: str,
    dataset: Dataset,
) -> tuple[int, int]:
    columns = ", ".join(f'"{column}"' for column in dataset.columns)
    placeholders = ", ".join("?" for _ in dataset.columns)
    statement = (
        f'INSERT OR IGNORE INTO "{dataset.table}" ({columns}) '
        f"VALUES ({placeholders})"
    )
    rows_seen = 0
    before = conn.total_changes
    digest = hashlib.sha256()

    with path.open("rb") as binary:
        content = binary.read()
    digest.update(content)

    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise ValueError("CSV has no header")

    batch = []
    for row in reader:
        if not any(value not in (None, "") for value in row.values()):
            continue
        batch.append(row_values(row, dataset))
        rows_seen += 1
        if len(batch) >= 2_000:
            conn.executemany(statement, batch)
            batch.clear()
    if batch:
        conn.executemany(statement, batch)

    rows_added = conn.total_changes - before
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        INSERT INTO github_csv_imports
            (path, blob_sha, content_sha, table_name, rows_seen, rows_added, imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            blob_sha=excluded.blob_sha,
            content_sha=excluded.content_sha,
            table_name=excluded.table_name,
            rows_seen=excluded.rows_seen,
            rows_added=excluded.rows_added,
            imported_at=excluded.imported_at
        """,
        (
            github_path,
            blob_sha,
            digest.hexdigest(),
            dataset.table,
            rows_seen,
            rows_added,
            now,
        ),
    )
    return rows_seen, rows_added


def unchanged(conn: sqlite3.Connection, path: str, blob_sha: str) -> bool:
    row = conn.execute(
        "SELECT blob_sha FROM github_csv_imports WHERE path = ?", (path,)
    ).fetchone()
    return row is not None and row[0] == blob_sha


def run(args: argparse.Namespace) -> int:
    args.db = args.db.expanduser().resolve()
    args.csv_dir = args.csv_dir.expanduser().resolve()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    args.csv_dir.mkdir(parents=True, exist_ok=True)

    print(f"Discovering CSVs in https://github.com/{args.repo}/tree/{args.branch}/data")
    files = list_csvs(args.repo, args.branch, args.token)
    if not files:
        print("No supported CSV files found.", file=sys.stderr)
        return 1

    downloaded = skipped = files_failed = rows_seen = rows_added = 0
    per_table: dict[str, int] = {}

    with closing(sqlite3.connect(args.db)) as conn:
        initialize_database(conn)
        for number, item in enumerate(files, 1):
            github_path = item["path"]
            dataset = dataset_for_path(github_path)
            assert dataset is not None

            if not args.force and unchanged(conn, github_path, item["sha"]):
                skipped += 1
                continue

            destination = local_path(args.csv_dir, github_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                content = github_request(
                    raw_url(args.repo, args.branch, github_path), args.token
                )
                destination.write_bytes(content)
                downloaded += 1
                with conn:
                    seen, added = import_csv(
                        conn, destination, github_path, item["sha"], dataset
                    )
                rows_seen += seen
                rows_added += added
                per_table[dataset.table] = per_table.get(dataset.table, 0) + added
                print(
                    f"[{number:>3}/{len(files)}] {github_path}: "
                    f"{seen:,} rows, {added:,} new"
                )
            except (OSError, ValueError, sqlite3.Error, HTTPError, URLError) as exc:
                files_failed += 1
                print(f"ERROR {github_path}: {exc}", file=sys.stderr)

    print("\nImport complete")
    print(f"  Database:   {args.db}")
    print(f"  CSV mirror: {args.csv_dir}")
    print(f"  Files:      {downloaded} downloaded, {skipped} unchanged, {files_failed} failed")
    print(f"  Rows:       {rows_seen:,} read, {rows_added:,} new")
    for table, count in sorted(per_table.items()):
        print(f"    {table}: {count:,} new")
    return 1 if files_failed else 0


def main() -> int:
    try:
        return run(parse_args())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (HTTPError, URLError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
