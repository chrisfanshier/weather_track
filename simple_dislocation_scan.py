"""simple_dislocation_scan.py - Rule-based model-vs-market dislocation scanner.

No probabilistic model is used here.
This script looks for hard contradictions like:
- High-temp bucket priced with meaningful YES odds while all model highs are below the bucket.
- Low-temp bucket priced with meaningful YES odds while all model lows are outside the bucket.

Use case: identify cleaner NO candidates where model ranges and market pricing diverge.

Usage:
    python simple_dislocation_scan.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tracker import (
    STATIONS,
    fetch_kalshi_markets,
    fetch_nws_hourly,
    fetch_nws_periods,
    fetch_openmeteo,
)

TOP_N = 50
MIN_YES_PCT = 12.0
MAX_YES_PCT = 55.0
MIN_MODEL_COUNT = 2
CONTRADICTION_MARGIN_F = 1.0


def to_local_date(iso_ts: str, tz_name: str) -> str:
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(tz_name)).date().isoformat()


def build_model_ranges(info: dict) -> dict[str, dict[str, list[float]]]:
    """Return model highs/lows per target date for one station.

    Shape:
      {
        'YYYY-MM-DD': {
          'highs': [..],
          'lows': [..],
          'high_sources': [...],
          'low_sources': [...]
        }
      }
    """
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

    # Open-Meteo daily highs/lows
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

    # NWS hourly: daytime high + full-day low
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

    # NWS period forecast: daytime highs and nighttime lows
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


def contradiction_for_no(contract: dict, market_type: str, highs: list[float], lows: list[float]) -> tuple[bool, float, str]:
    """Return (is_contradiction, distance_f, reason) for NO-side dislocation."""
    low = float(contract["low"])
    high = float(contract["high"])

    if market_type == "high" and highs:
        max_high = max(highs)
        min_high = min(highs)

        # Bucket is hotter than all model highs
        if low != -999 and low > (max_high + CONTRADICTION_MARGIN_F):
            return True, low - max_high, "models_below_high_bucket"

        # Bucket is cooler than all model highs
        if high != 999 and high < (min_high - CONTRADICTION_MARGIN_F):
            return True, min_high - high, "models_above_high_bucket"

    if market_type == "low" and lows:
        max_low = max(lows)
        min_low = min(lows)

        # Bucket is colder than all model lows
        if high != 999 and high < (min_low - CONTRADICTION_MARGIN_F):
            return True, min_low - high, "models_above_low_bucket"

        # Bucket is warmer than all model lows
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
            try:
                contracts = fetch_kalshi_markets(series, target_dates)
            except Exception:
                contracts = []

            for c in contracts:
                d = c.get("target_date", today)
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

                bad, distance_f, reason = contradiction_for_no(c, market_type, highs, lows)
                if not bad:
                    continue

                model_vals = highs if market_type == "high" else lows
                model_min = min(model_vals)
                model_max = max(model_vals)
                spread = model_max - model_min

                # If NO is right, ROI on stake is (100 - no_price) / no_price.
                no_roi_pct = ((100.0 - no_pct) / no_pct * 100.0) if no_pct > 0 else 0.0

                # Rank by contradiction strength, model agreement tightness, and practical pricing.
                score = distance_f * (yes_pct / 100.0) * (1.0 / (1.0 + spread)) * model_count

                rows.append(
                    {
                        "icao": icao,
                        "city": info["name"],
                        "date": d,
                        "type": market_type,
                        "label": c["label"],
                        "ticker": c["ticker"],
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
                        "score": round(score, 4),
                    }
                )

    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def print_rows(rows: list[dict], top_n: int = TOP_N) -> None:
    print(f"Rule-Based Dislocation Scan - top {top_n} NO candidates")
    print("=" * 170)
    print(
        f"{'ICAO':<5} {'City':<16} {'Date':<10} {'T':<4} {'YES%':>6} {'NO%':>6} "
        f"{'ModelMin':>8} {'ModelMax':>8} {'Sprd':>6} {'DistF':>6} {'N':>3} {'NO ROI%':>8} {'Reason':<28} Label"
    )
    print("-" * 170)
    for r in rows[:top_n]:
        print(
            f"{r['icao']:<5} {r['city']:<16} {r['date']:<10} {r['type']:<4} "
            f"{r['yes_pct']:>6.1f} {r['no_pct']:>6.1f} {r['model_min']:>8.1f} {r['model_max']:>8.1f} "
            f"{r['model_spread']:>6.2f} {r['distance_f']:>6.2f} {r['models_n']:>3} {r['no_roi_if_right_pct']:>8.2f} "
            f"{r['reason']:<28} {r['label']}"
        )


if __name__ == "__main__":
    result = scan()
    print_rows(result, TOP_N)
