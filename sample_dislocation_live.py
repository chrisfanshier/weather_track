"""
simple_dislocation_scan.py - Rule-based model-vs-market dislocation scanner.

This version:
- pulls Kalshi open markets directly from the public API
- does not use tracker.fetch_kalshi_markets()
- does not cache Kalshi results
- filters by actual event/market ticker date, not close_time
- includes NO ROI column

Usage:
    python simple_dislocation_scan.py
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from tracker import (
    STATIONS,
    fetch_nws_hourly,
    fetch_nws_periods,
    fetch_openmeteo,
)

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"

TOP_N = 50

# Your stated target zone: things still priced around 10–20 cents.
MIN_YES_PCT = 10.0
MAX_YES_PCT = 20.0

MIN_MODEL_COUNT = 2
CONTRADICTION_MARGIN_F = 1.0

MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def to_local_date(iso_ts: str, tz_name: str) -> str:
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(tz_name)).date().isoformat()


def parse_event_date(text: str) -> str | None:
    """
    Parse Kalshi-style date fragments like:
      26MAY27
      KXHIGHSEA-26MAY27
      KXHIGHSEA-26MAY27-T70
    """
    if not text:
        return None

    m = re.search(r"(\d{2})([A-Z]{3})(\d{2})", text.upper())
    if not m:
        return None

    yy = int(m.group(1))
    mon = MONTHS.get(m.group(2))
    dd = int(m.group(3))

    if mon is None:
        return None

    return f"{2000 + yy:04d}-{mon:02d}-{dd:02d}"


def cents_from_market(m: dict, key: str) -> float | None:
    """
    Kalshi usually returns yes_ask/no_ask in cents.
    Some payloads may also include *_dollars.
    """
    val = m.get(key)
    if val is not None:
        try:
            return float(val)
        except Exception:
            pass

    dollars_key = f"{key}_dollars"
    val = m.get(dollars_key)
    if val is not None:
        try:
            return float(val) * 100.0
        except Exception:
            pass

    return None


def label_for_market(m: dict) -> str:
    for key in ("yes_sub_title", "title", "subtitle"):
        val = m.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return m.get("ticker", "")


def parse_bucket_label(label: str) -> tuple[float, float] | None:
    """
    Parse displayed Kalshi labels like:
      67° or below
      68° to 69°
      92° or above
    """
    if not label:
        return None

    s = label.replace("º", "°").strip()
    nums = [int(x) for x in re.findall(r"-?\d+", s)]

    if len(nums) >= 2:
        return float(nums[0]), float(nums[1])

    if len(nums) == 1:
        n = float(nums[0])

        if re.search(r"below|less|under", s, re.I):
            return -999.0, n

        if re.search(r"above|more|over|greater", s, re.I):
            return n, 999.0

    return None


def range_from_market(m: dict) -> tuple[float, float, str] | None:
    """
    Use actual market fields when possible. Fall back to parsing label.
    """
    label = label_for_market(m)

    # Some Kalshi markets expose explicit strike fields.
    low = None
    high = None

    for k in ("floor_strike", "floor_strike_dollars"):
        if m.get(k) is not None:
            try:
                low = float(m[k])
                break
            except Exception:
                pass

    for k in ("cap_strike", "cap_strike_dollars"):
        if m.get(k) is not None:
            try:
                high = float(m[k])
                break
            except Exception:
                pass

    if low is not None or high is not None:
        return (
            -999.0 if low is None else low,
            999.0 if high is None else high,
            label,
        )

    parsed = parse_bucket_label(label)
    if parsed is None:
        return None

    return parsed[0], parsed[1], label


def fresh_kalshi_markets_for_series(series_ticker: str) -> list[dict]:
    """
    Fresh open-market pull from Kalshi.

    No tracker cache.
    No synthetic buckets.
    """
    out: list[dict] = []
    cursor = None

    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "simple-dislocation-scan/1.0",
        }
    )

    while True:
        params = {
            "series_ticker": series_ticker,
            "status": "open",
            "limit": 1000,
            "mve_filter": "exclude",
            "_": str(int(time.time() * 1000)),
        }

        if cursor:
            params["cursor"] = cursor

        r = session.get(f"{KALSHI_BASE}/markets", params=params, timeout=20)
        r.raise_for_status()

        payload = r.json()
        out.extend(payload.get("markets", []))

        cursor = payload.get("cursor")
        if not cursor:
            break

    return out


def fetch_live_kalshi_contracts(series_tickers: list[str], target_dates: list[str]) -> list[dict]:
    """
    Normalize only real listed Kalshi markets.
    """
    contracts: list[dict] = []

    for series_ticker in series_tickers:
        try:
            markets = fresh_kalshi_markets_for_series(series_ticker)
        except Exception as e:
            print(f"Kalshi fetch failed for {series_ticker}: {e}")
            continue

        for m in markets:
            ticker = m.get("ticker", "")
            event_ticker = m.get("event_ticker", "")

            # Important: do NOT infer event date from close_time.
            # That was how yesterday's markets could leak into today.
            target_date = parse_event_date(event_ticker) or parse_event_date(ticker)
            if target_date not in target_dates:
                continue

            rng = range_from_market(m)
            if rng is None:
                continue

            low, high, label = rng

            yes_ask = cents_from_market(m, "yes_ask")
            no_ask = cents_from_market(m, "no_ask")

            if yes_ask is None or no_ask is None:
                continue

            contracts.append(
                {
                    "ticker": ticker,
                    "event_ticker": event_ticker,
                    "series_ticker": series_ticker,
                    "target_date": target_date,
                    "label": label,
                    "low": low,
                    "high": high,
                    "yes_ask": yes_ask,
                    "no_ask": no_ask,
                }
            )

    return contracts


def build_model_ranges(info: dict) -> dict[str, dict[str, list[float]]]:
    by_date: dict[str, dict[str, list]] = {}

    def ensure(d: str) -> dict[str, list]:
        if d not in by_date:
            by_date[d] = {
                "highs": [],
                "lows": [],
                "high_sources": [],
                "low_sources": [],
            }
        return by_date[d]

    try:
        om_rows = fetch_openmeteo(info["lat"], info["lon"], info["tz"])
    except Exception:
        om_rows = []

    for r in om_rows:
        d = r["forecast_date"]
        slot = ensure(d)

        if r.get("high_f") is not None:
            slot["highs"].append(float(r["high_f"]))
            slot["high_sources"].append("openmeteo_daily_high")

        if r.get("low_f") is not None:
            slot["lows"].append(float(r["low_f"]))
            slot["low_sources"].append("openmeteo_daily_low")

    try:
        hourly = fetch_nws_hourly(info["nws_grid"])
    except Exception:
        hourly = []

    hourly_by_date: dict[str, list[float]] = {}

    for r in hourly:
        if r.get("temp_f") is None:
            continue

        d = to_local_date(r["valid_time"], info["tz"])
        hourly_by_date.setdefault(d, []).append(float(r["temp_f"]))

    for d, temps in hourly_by_date.items():
        slot = ensure(d)

        slot["highs"].append(max(temps))
        slot["high_sources"].append("nws_hourly_high")

        slot["lows"].append(min(temps))
        slot["low_sources"].append("nws_hourly_low")

    try:
        periods = fetch_nws_periods(info["nws_grid"])
    except Exception:
        periods = []

    period_highs: dict[str, list[float]] = {}
    period_lows: dict[str, list[float]] = {}

    for p in periods:
        if p.get("temp_f") is None or not p.get("start_time"):
            continue

        d = to_local_date(p["start_time"], info["tz"])
        t = float(p["temp_f"])

        if int(p.get("is_daytime") or 0) == 1:
            period_highs.setdefault(d, []).append(t)
        else:
            period_lows.setdefault(d, []).append(t)

    for d, vals in period_highs.items():
        slot = ensure(d)
        slot["highs"].append(max(vals))
        slot["high_sources"].append("nws_period_high")

    for d, vals in period_lows.items():
        slot = ensure(d)
        slot["lows"].append(min(vals))
        slot["low_sources"].append("nws_period_low")

    return by_date


def contradiction_for_no(
    contract: dict,
    market_type: str,
    highs: list[float],
    lows: list[float],
) -> tuple[bool, float, str]:
    low = float(contract["low"])
    high = float(contract["high"])

    if market_type == "high" and highs:
        max_high = max(highs)
        min_high = min(highs)

        # Bucket is hotter than all model highs.
        if low != -999 and low > (max_high + CONTRADICTION_MARGIN_F):
            return True, low - max_high, "models_below_high_bucket"

        # Bucket is cooler than all model highs.
        if high != 999 and high < (min_high - CONTRADICTION_MARGIN_F):
            return True, min_high - high, "models_above_high_bucket"

    if market_type == "low" and lows:
        max_low = max(lows)
        min_low = min(lows)

        # Bucket is colder than all model lows.
        if high != 999 and high < (min_low - CONTRADICTION_MARGIN_F):
            return True, min_low - high, "models_above_low_bucket"

        # Bucket is warmer than all model lows.
        if low != -999 and low > (max_low + CONTRADICTION_MARGIN_F):
            return True, low - max_low, "models_below_low_bucket"

    return False, 0.0, ""


def scan() -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    target_dates = [today, tomorrow]

    rows = []

    for icao, info in STATIONS.items():
        model_ranges = build_model_ranges(info)

        for market_type, key in (("high", "kalshi_high"), ("low", "kalshi_low")):
            series = info.get(key, [])
            if not series:
                continue

            contracts = fetch_live_kalshi_contracts(series, target_dates)

            for c in contracts:
                d = c.get("target_date")
                slot = model_ranges.get(d)

                if not slot:
                    continue

                highs = [float(x) for x in slot["highs"]]
                lows = [float(x) for x in slot["lows"]]

                model_count = len(highs) if market_type == "high" else len(lows)
                if model_count < MIN_MODEL_COUNT:
                    continue

                yes_pct = float(c["yes_ask"])
                no_pct = float(c["no_ask"])

                if yes_pct < MIN_YES_PCT or yes_pct > MAX_YES_PCT:
                    continue

                bad, distance_f, reason = contradiction_for_no(
                    c,
                    market_type,
                    highs,
                    lows,
                )

                if not bad:
                    continue

                model_vals = highs if market_type == "high" else lows
                model_min = min(model_vals)
                model_max = max(model_vals)
                spread = model_max - model_min

                no_roi_pct = ((100.0 - no_pct) / no_pct * 100.0) if no_pct > 0 else 0.0

                rows.append(
                    {
                        "icao": icao,
                        "city": info["name"],
                        "date": d,
                        "type": market_type,
                        "label": c["label"],
                        "ticker": c["ticker"],
                        "event_ticker": c["event_ticker"],
                        "yes_pct": yes_pct,
                        "no_pct": no_pct,
                        "model_min": round(model_min, 1),
                        "model_max": round(model_max, 1),
                        "model_spread": round(spread, 2),
                        "distance_f": round(distance_f, 2),
                        "models_n": model_count,
                        "reason": reason,
                        "lean": "NO",
                        "no_roi_if_right_pct": round(no_roi_pct, 2),
                    }
                )

    rows.sort(key=lambda r: r["distance_f"], reverse=True)
    return rows


def print_rows(rows: list[dict], top_n: int = TOP_N) -> None:
    print(f"Rule-Based Dislocation Scan - top {top_n} NO candidates")
    print("=" * 200)

    print(
        f"{'ICAO':<5} {'City':<16} {'Date':<10} {'T':<4} "
        f"{'YES%':>6} {'NO%':>6} "
        f"{'ModelMin':>8} {'ModelMax':>8} {'Sprd':>6} {'DistF':>6} "
        f"{'N':>3} {'NO ROI%':>8} {'Reason':<28} {'Ticker':<35} Label"
    )

    print("-" * 200)

    for r in rows[:top_n]:
        print(
            f"{r['icao']:<5} {r['city']:<16} {r['date']:<10} {r['type']:<4} "
            f"{r['yes_pct']:>6.1f} {r['no_pct']:>6.1f} "
            f"{r['model_min']:>8.1f} {r['model_max']:>8.1f} "
            f"{r['model_spread']:>6.2f} {r['distance_f']:>6.2f} "
            f"{r['models_n']:>3} {r['no_roi_if_right_pct']:>8.2f} "
            f"{r['reason']:<28} {r['ticker']:<35} {r['label']}"
        )


if __name__ == "__main__":
    result = scan()
    print_rows(result, TOP_N)