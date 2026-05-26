"""weather_trade_agent.py - Paper trading engine with 3-of-4 family agreement filter.

Phase-1 scope:
- paper-first execution only
- strict exact-bucket agreement (3 of 4 families)
- post-consensus filters (price/confidence/dollar-edge/exposure)
- lifecycle management with 70% early-exit check
- SQLite logging for scans, votes, skips, orders, and positions

Usage:
    python weather_trade_agent.py
"""

from __future__ import annotations

import csv
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable

import requests

from tracker import STATIONS, fetch_kalshi_markets


# --- Constants ----------------------------------------------------------------

DB_PATH = Path(__file__).parent / "weather_paper.db"
DATA_DIR = Path(__file__).parent / "data" / "paper"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

TARGET_CONTRACTS = 1
MIN_PRICE_CENTS = 40
MIN_CONFIDENCE_PCT = 55.0
MIN_DOLLAR_EDGE = 0.50
MAX_OPEN_PER_STATION = 2
EARLY_EXIT_CAPTURE = 0.70

FAMILY_SIGMA = {
    "gfs": 2.8,
    "aigefs": 2.4,
    "ifs": 2.6,
    "aifs_ens": 2.2,
}


def should_export_csv() -> bool:
    override = os.getenv("ENABLE_CSV_EXPORT", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class Family(str, Enum):
    GFS = "gfs"
    AIGEFS = "aigefs"
    IFS = "ifs"
    AIFS_ENS = "aifs_ens"


@dataclass(frozen=True)
class MarketContract:
    station: str
    target_date: str
    ticker: str
    label: str
    low: float
    high: float
    yes_bid: int
    yes_ask: int
    no_bid: int
    no_ask: int
    market_type: str


@dataclass(frozen=True)
class FamilyVote:
    station: str
    target_date: str
    family: Family
    ticker: str
    side: Side
    confidence_pct: float
    model_prob: float
    market_price_prob: float
    edge_per_contract: float


@dataclass(frozen=True)
class TradeDecision:
    eligible: bool
    reason: str
    station: str
    target_date: str
    ticker: str
    side: Side
    confidence_pct: float
    model_prob: float
    market_price_prob: float
    edge_per_contract: float
    contracts: int


@dataclass
class OpenPosition:
    position_id: int
    station: str
    target_date: str
    ticker: str
    side: Side
    entry_price_cents: int
    contracts: int
    opened_at: str


class SqliteLedger:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.init_db()

    def close(self) -> None:
        self.conn.close()

    def init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scan_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL,
                candidates_n INTEGER NOT NULL,
                consensus_pass_n INTEGER NOT NULL,
                entries_n INTEGER NOT NULL,
                exits_n INTEGER NOT NULL,
                skips_n INTEGER NOT NULL,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS family_vote_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL,
                station TEXT NOT NULL,
                target_date TEXT NOT NULL,
                family TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                confidence_pct REAL NOT NULL,
                model_prob REAL NOT NULL,
                market_price_prob REAL NOT NULL,
                edge_per_contract REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trade_skip_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL,
                station TEXT NOT NULL,
                target_date TEXT NOT NULL,
                ticker TEXT,
                side TEXT,
                reason TEXT NOT NULL,
                detail TEXT
            );

            CREATE TABLE IF NOT EXISTS paper_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL,
                station TEXT NOT NULL,
                target_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                price_cents INTEGER NOT NULL,
                contracts INTEGER NOT NULL,
                reason TEXT
            );

            CREATE TABLE IF NOT EXISTS paper_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station TEXT NOT NULL,
                target_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                contracts INTEGER NOT NULL,
                entry_price_cents INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL,
                closed_at TEXT,
                exit_price_cents INTEGER,
                realized_pnl_cents REAL
            );

            CREATE INDEX IF NOT EXISTS idx_positions_open_station
                ON paper_positions(status, station);
            CREATE INDEX IF NOT EXISTS idx_orders_run
                ON paper_orders(run_at);
            """
        )
        self.conn.commit()

    def log_family_votes(self, run_at: str, votes: Iterable[FamilyVote]) -> None:
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO family_vote_log (
                    run_at, station, target_date, family, ticker, side,
                    confidence_pct, model_prob, market_price_prob, edge_per_contract
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        run_at,
                        v.station,
                        v.target_date,
                        v.family.value,
                        v.ticker,
                        v.side.value,
                        v.confidence_pct,
                        v.model_prob,
                        v.market_price_prob,
                        v.edge_per_contract,
                    )
                    for v in votes
                ],
            )

    def log_skip(self, run_at: str, station: str, target_date: str, ticker: str | None,
                 side: Side | None, reason: str, detail: str = "") -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO trade_skip_log
                    (run_at, station, target_date, ticker, side, reason, detail)
                VALUES (?,?,?,?,?,?,?)
                """,
                (run_at, station, target_date, ticker, side.value if side else None, reason, detail),
            )

    def open_positions_for_station(self, station: str) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE status='open' AND station=?",
            (station,),
        )
        return int(cur.fetchone()[0])

    def get_open_positions(self) -> list[OpenPosition]:
        cur = self.conn.execute(
            """
            SELECT id, station, target_date, ticker, side, entry_price_cents, contracts, opened_at
            FROM paper_positions
            WHERE status='open'
            """
        )
        rows = cur.fetchall()
        return [
            OpenPosition(
                position_id=r[0],
                station=r[1],
                target_date=r[2],
                ticker=r[3],
                side=Side(r[4]),
                entry_price_cents=r[5],
                contracts=r[6],
                opened_at=r[7],
            )
            for r in rows
        ]

    def place_paper_order(self, run_at: str, decision: TradeDecision, reason: str = "entry") -> None:
        entry_price = int(round(decision.market_price_prob * 100))
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO paper_orders
                    (run_at, station, target_date, ticker, side, order_type, price_cents, contracts, reason)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_at,
                    decision.station,
                    decision.target_date,
                    decision.ticker,
                    decision.side.value,
                    "entry",
                    entry_price,
                    decision.contracts,
                    reason,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO paper_positions
                    (station, target_date, ticker, side, contracts, entry_price_cents, opened_at, status)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    decision.station,
                    decision.target_date,
                    decision.ticker,
                    decision.side.value,
                    decision.contracts,
                    entry_price,
                    run_at,
                    "open",
                ),
            )

    def close_position(self, run_at: str, position: OpenPosition, exit_price_cents: int, reason: str) -> None:
        pnl_cents = position.contracts * (exit_price_cents - position.entry_price_cents)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO paper_orders
                    (run_at, station, target_date, ticker, side, order_type, price_cents, contracts, reason)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_at,
                    position.station,
                    position.target_date,
                    position.ticker,
                    position.side.value,
                    "exit",
                    exit_price_cents,
                    position.contracts,
                    reason,
                ),
            )
            self.conn.execute(
                """
                UPDATE paper_positions
                SET status='closed', closed_at=?, exit_price_cents=?, realized_pnl_cents=?
                WHERE id=?
                """,
                (run_at, exit_price_cents, pnl_cents, position.position_id),
            )

    def log_scan_summary(self, run_at: str, candidates_n: int, consensus_pass_n: int,
                         entries_n: int, exits_n: int, skips_n: int, notes: str = "") -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO scan_log
                    (run_at, candidates_n, consensus_pass_n, entries_n, exits_n, skips_n, notes)
                VALUES (?,?,?,?,?,?,?)
                """,
                (run_at, candidates_n, consensus_pass_n, entries_n, exits_n, skips_n, notes),
            )


def export_cycle_to_csv(conn: sqlite3.Connection, run_at: str) -> dict[str, int]:
    date_str = run_at[:10]
    specs = [
        (
            "scan_log",
            "SELECT * FROM scan_log WHERE run_at = ?",
            (run_at,),
            DATA_DIR / "scan_log" / f"{date_str}.csv",
        ),
        (
            "family_vote_log",
            "SELECT * FROM family_vote_log WHERE run_at = ?",
            (run_at,),
            DATA_DIR / "family_vote_log" / f"{date_str}.csv",
        ),
        (
            "trade_skip_log",
            "SELECT * FROM trade_skip_log WHERE run_at = ?",
            (run_at,),
            DATA_DIR / "trade_skip_log" / f"{date_str}.csv",
        ),
        (
            "paper_orders",
            "SELECT * FROM paper_orders WHERE run_at = ?",
            (run_at,),
            DATA_DIR / "paper_orders" / f"{date_str}.csv",
        ),
        (
            "paper_positions_opened",
            "SELECT * FROM paper_positions WHERE opened_at = ?",
            (run_at,),
            DATA_DIR / "paper_positions_opened" / f"{date_str}.csv",
        ),
        (
            "paper_positions_closed",
            "SELECT * FROM paper_positions WHERE closed_at = ?",
            (run_at,),
            DATA_DIR / "paper_positions_closed" / f"{date_str}.csv",
        ),
    ]
    counts: dict[str, int] = {}
    for label, sql, params, out_path in specs:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        counts[label] = len(rows)
        if not rows:
            continue
        write_header = not out_path.exists()
        with open(out_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow([d[0] for d in cur.description])
            writer.writerows(rows)
    return counts


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 0.5 if x >= mu else 0.0
    z = (x - mu) / (sigma * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def bucket_probability(low: float, high: float, mu: float, sigma: float) -> float:
    lo = -999.0 if low == -999 else low - 0.5
    hi = 999.0 if high == 999 else high + 0.5
    return normal_cdf(hi, mu, sigma) - normal_cdf(lo, mu, sigma)


def fetch_openmeteo_daily_high(lat: float, lon: float, tz_name: str, target_date: str) -> float:
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": tz_name,
        "start_date": target_date,
        "end_date": target_date,
        "forecast_days": 7,
    }
    resp = requests.get(OPEN_METEO_BASE, params=params, timeout=15)
    resp.raise_for_status()
    daily = resp.json().get("daily", {})
    dates = daily.get("time", [])
    highs = daily.get("temperature_2m_max", [])
    for d, h in zip(dates, highs):
        if d == target_date:
            return float(h)
    raise RuntimeError(f"No Open-Meteo daily high for {target_date}")


def build_family_mu(openmeteo_high: float) -> dict[Family, float]:
    # Phase-1 approximation: derive four family centers from Open-Meteo baseline.
    # Real adapters for GFS/AIGEFS/IFS/AIFS-ENS can replace this later.
    return {
        Family.GFS: openmeteo_high - 0.4,
        Family.AIGEFS: openmeteo_high + 0.1,
        Family.IFS: openmeteo_high - 0.2,
        Family.AIFS_ENS: openmeteo_high + 0.2,
    }


def market_price_for_side(contract: MarketContract, side: Side) -> float:
    if side == Side.YES:
        return contract.yes_ask / 100.0
    return contract.no_ask / 100.0


def family_vote_for_contracts(station: str, target_date: str, family: Family, mu: float,
                              contracts: list[MarketContract]) -> FamilyVote | None:
    sigma = FAMILY_SIGMA[family.value]
    best_vote: FamilyVote | None = None
    for c in contracts:
        p_bucket = bucket_probability(c.low, c.high, mu, sigma)
        yes_edge = p_bucket - (c.yes_ask / 100.0)
        no_edge = (1.0 - p_bucket) - (c.no_ask / 100.0)

        if yes_edge >= no_edge:
            side = Side.YES
            model_prob = p_bucket
            market_prob = c.yes_ask / 100.0
            edge = yes_edge
        else:
            side = Side.NO
            model_prob = 1.0 - p_bucket
            market_prob = c.no_ask / 100.0
            edge = no_edge

        vote = FamilyVote(
            station=station,
            target_date=target_date,
            family=family,
            ticker=c.ticker,
            side=side,
            confidence_pct=model_prob * 100.0,
            model_prob=model_prob,
            market_price_prob=market_prob,
            edge_per_contract=edge,
        )
        if best_vote is None or vote.edge_per_contract > best_vote.edge_per_contract:
            best_vote = vote

    return best_vote


def three_of_four_consensus(votes: list[FamilyVote]) -> tuple[bool, TradeDecision | None, str]:
    if len(votes) < 4:
        return False, None, "missing_family_votes"

    key_to_votes: dict[tuple[str, Side], list[FamilyVote]] = {}
    for v in votes:
        key = (v.ticker, v.side)
        key_to_votes.setdefault(key, []).append(v)

    winner_key = None
    winner_votes: list[FamilyVote] = []
    for key, grouped in key_to_votes.items():
        if len(grouped) > len(winner_votes):
            winner_key = key
            winner_votes = grouped

    if len(winner_votes) < 3 or winner_key is None:
        return False, None, "family_disagreement"

    ref = winner_votes[0]
    avg_conf = sum(v.confidence_pct for v in winner_votes) / len(winner_votes)
    avg_model_prob = sum(v.model_prob for v in winner_votes) / len(winner_votes)
    avg_mkt_prob = sum(v.market_price_prob for v in winner_votes) / len(winner_votes)
    avg_edge = sum(v.edge_per_contract for v in winner_votes) / len(winner_votes)

    decision = TradeDecision(
        eligible=True,
        reason="consensus_pass",
        station=ref.station,
        target_date=ref.target_date,
        ticker=ref.ticker,
        side=ref.side,
        confidence_pct=avg_conf,
        model_prob=avg_model_prob,
        market_price_prob=avg_mkt_prob,
        edge_per_contract=avg_edge,
        contracts=TARGET_CONTRACTS,
    )
    return True, decision, "consensus_pass"


def run_post_consensus_filters(decision: TradeDecision, open_station_positions: int) -> TradeDecision:
    price_cents = round(decision.market_price_prob * 100)
    if price_cents < MIN_PRICE_CENTS:
        return TradeDecision(False, "min_price", **{**decision.__dict__})

    if decision.confidence_pct < MIN_CONFIDENCE_PCT:
        return TradeDecision(False, "min_confidence", **{**decision.__dict__})

    dollar_edge = abs(decision.model_prob - decision.market_price_prob) * decision.contracts
    if dollar_edge < MIN_DOLLAR_EDGE:
        return TradeDecision(False, "dollar_edge", **{**decision.__dict__})

    if open_station_positions >= MAX_OPEN_PER_STATION:
        return TradeDecision(False, "exposure_cap", **{**decision.__dict__})

    return decision


def current_exit_price_cents(position: OpenPosition, contract: MarketContract | None) -> int | None:
    if contract is None:
        return None
    if position.side == Side.YES:
        return contract.yes_bid
    return contract.no_bid


def should_early_exit(position: OpenPosition, exit_price_cents: int) -> bool:
    max_profit = 100 - position.entry_price_cents
    if max_profit <= 0:
        return False
    captured = (exit_price_cents - position.entry_price_cents) / max_profit
    return captured >= EARLY_EXIT_CAPTURE


def map_contracts_by_ticker(contracts: Iterable[MarketContract]) -> dict[str, MarketContract]:
    return {c.ticker: c for c in contracts}


def build_station_contracts(station_id: str, info: dict, target_dates: list[str]) -> list[MarketContract]:
    out: list[MarketContract] = []
    for market_type, key in (("high", "kalshi_high"), ("low", "kalshi_low")):
        series = info.get(key, [])
        if not series:
            continue
        rows = fetch_kalshi_markets(series, target_dates)
        for r in rows:
            out.append(
                MarketContract(
                    station=station_id,
                    target_date=r.get("target_date", target_dates[0]),
                    ticker=r["ticker"],
                    label=r["label"],
                    low=r["low"],
                    high=r["high"],
                    yes_bid=r["yes_bid"],
                    yes_ask=r["yes_ask"],
                    no_bid=r["no_bid"],
                    no_ask=r["no_ask"],
                    market_type=market_type,
                )
            )
    return out


def run_cycle() -> None:
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    target_dates = [today, tomorrow]

    ledger = SqliteLedger(DB_PATH)

    candidates_n = 0
    consensus_pass_n = 0
    entries_n = 0
    exits_n = 0
    skips_n = 0
    csv_counts: dict[str, int] = {}

    try:
        all_contracts: list[MarketContract] = []
        for station_id, info in STATIONS.items():
            all_contracts.extend(build_station_contracts(station_id, info, target_dates))

        contracts_by_ticker = map_contracts_by_ticker(all_contracts)

        # Lifecycle management first: evaluate 70% early-exit rule for open positions.
        for pos in ledger.get_open_positions():
            contract = contracts_by_ticker.get(pos.ticker)
            px = current_exit_price_cents(pos, contract)
            if px is None:
                continue
            if should_early_exit(pos, px):
                ledger.close_position(run_at, pos, px, "early_exit_70pct")
                exits_n += 1

        # Entry scan for each station/day.
        for station_id, info in STATIONS.items():
            station_contracts = [c for c in all_contracts if c.station == station_id]
            if not station_contracts:
                continue

            for target_date in target_dates:
                day_contracts = [c for c in station_contracts if c.target_date == target_date]
                if not day_contracts:
                    continue

                candidates_n += len(day_contracts)

                try:
                    om_high = fetch_openmeteo_daily_high(info["lat"], info["lon"], info["tz"], target_date)
                except Exception as exc:
                    ledger.log_skip(
                        run_at,
                        station_id,
                        target_date,
                        None,
                        None,
                        "openmeteo_fetch",
                        str(exc),
                    )
                    skips_n += 1
                    continue

                family_mu = build_family_mu(om_high)
                votes: list[FamilyVote] = []
                for fam in Family:
                    vote = family_vote_for_contracts(station_id, target_date, fam, family_mu[fam], day_contracts)
                    if vote is not None:
                        votes.append(vote)

                ledger.log_family_votes(run_at, votes)
                passed, decision, consensus_reason = three_of_four_consensus(votes)
                if not passed or decision is None:
                    ledger.log_skip(run_at, station_id, target_date, None, None, consensus_reason)
                    skips_n += 1
                    continue

                consensus_pass_n += 1
                gated = run_post_consensus_filters(decision, ledger.open_positions_for_station(station_id))
                if not gated.eligible:
                    ledger.log_skip(
                        run_at,
                        station_id,
                        target_date,
                        gated.ticker,
                        gated.side,
                        gated.reason,
                        (
                            f"conf={gated.confidence_pct:.1f} price={gated.market_price_prob:.2f} "
                            f"edge={gated.edge_per_contract:.4f}"
                        ),
                    )
                    skips_n += 1
                    continue

                ledger.place_paper_order(run_at, gated)
                entries_n += 1

        ledger.log_scan_summary(
            run_at,
            candidates_n=candidates_n,
            consensus_pass_n=consensus_pass_n,
            entries_n=entries_n,
            exits_n=exits_n,
            skips_n=skips_n,
        )
        if should_export_csv():
            csv_counts = export_cycle_to_csv(ledger.conn, run_at)

        print(f"paper cycle {run_at}")
        print(
            "  "
            f"candidates:{candidates_n} consensus_pass:{consensus_pass_n} "
            f"entries:{entries_n} exits:{exits_n} skips:{skips_n}"
        )
        if csv_counts:
            print(
                "  "
                f"csv scan:{csv_counts.get('scan_log', 0)} votes:{csv_counts.get('family_vote_log', 0)} "
                f"skips:{csv_counts.get('trade_skip_log', 0)} orders:{csv_counts.get('paper_orders', 0)}"
            )
        else:
            print("  csv export skipped (local run). Set ENABLE_CSV_EXPORT=1 to force export.")
    finally:
        ledger.close()


if __name__ == "__main__":
    run_cycle()
