from __future__ import annotations

import math
import statistics
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from tracker import STATIONS, fetch_kalshi_markets


# =============================
# CONFIG
# =============================

TARGET_DAYS = 2  # today + tomorrow
TOP_N_PER_TIER = 40

MIN_YES_PRICE = 10.0
MAX_YES_PRICE = 55.0

MIN_EV_NO_CENTS = 5.0

TIER1_MAX_SOURCE_SPREAD = 2.0
TIER2_MAX_SOURCE_SPREAD = 4.0

MAX_ENSEMBLE_SD_TIER1 = 2.5
MAX_ENSEMBLE_SD_TIER2 = 4.0

REQUEST_SLEEP = 0.15


# =============================
# BASIC HELPERS
# =============================

def fmt(x, digits=1) -> str:
    if x is None:
        return "NA"
    if isinstance(x, float) and math.isnan(x):
        return "NA"
    return f"{x:.{digits}f}"


def fmt_interval(low: float, high: float) -> str:
    if low == -math.inf:
        return f"<{fmt(high)}"
    if high == math.inf:
        return f">={fmt(low)}"
    return f"{fmt(low)}-{fmt(high)}"


def percentile(vals: list[float], p: float) -> float:
    vals = sorted(vals)
    if not vals:
        return math.nan
    if len(vals) == 1:
        return vals[0]

    k = (len(vals) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)

    if lo == hi:
        return vals[int(k)]

    return vals[lo] * (hi - k) + vals[hi] * (k - lo)


def raw_interval_for_cli_bucket(low: float, high: float) -> tuple[float, float]:
    """
    Convert integer Kalshi/CLI outcome bucket into raw-temperature interval.

    Examples:
      54-55      => 53.5 <= raw < 55.5
      52-53      => 51.5 <= raw < 53.5
      <=47       => raw < 47.5
      >=56       => raw >= 55.5
    """
    raw_low = -math.inf if low == -999 else low - 0.5
    raw_high = math.inf if high == 999 else high + 0.5
    return raw_low, raw_high


def distance_to_cli_bucket(value: float | None, low: float, high: float) -> float:
    """
    Distance from raw value to CLI-rounded bucket interval.
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return math.nan

    raw_low, raw_high = raw_interval_for_cli_bucket(low, high)

    if raw_low <= value < raw_high:
        return 0.0

    if value < raw_low:
        return raw_low - value

    return value - raw_high


def source_spread(vals: list[float | None]) -> float:
    clean = [
        float(x)
        for x in vals
        if x is not None and not (isinstance(x, float) and math.isnan(x))
    ]

    if len(clean) < 2:
        return math.nan

    return max(clean) - min(clean)


def ev_yes_cents(p_yes: float, yes_price: float) -> float:
    return 100.0 * p_yes - yes_price


def ev_no_cents(p_no: float, no_price: float) -> float:
    return 100.0 * p_no - no_price


def source_votes_for_bucket(values: dict[str, float | None], low: float, high: float) -> dict:
    """
    Count whether point forecasts fall inside the CLI-rounded bucket interval.

    This catches cases where ensemble-only EV says NO, but NWS/OMdet
    directly support the bucket.
    """
    raw_low, raw_high = raw_interval_for_cli_bucket(low, high)

    votes = []

    for name, value in values.items():
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue

        v = float(value)

        votes.append(
            {
                "source": name,
                "value": v,
                "inside": raw_low <= v < raw_high,
                "distance": distance_to_cli_bucket(v, low, high),
            }
        )

    n = len(votes)
    n_inside = sum(1 for v in votes if v["inside"])

    return {
        "n_sources": n,
        "n_sources_inside": n_inside,
        "sources_inside": ",".join(v["source"] for v in votes if v["inside"]),
        "votes": votes,
    }


# =============================
# OPEN-METEO
# =============================

def fetch_openmeteo_deterministic(lat: float, lon: float, tz: str) -> dict:
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": tz,
        "temperature_unit": "fahrenheit",
        "forecast_days": 3,
        "daily": "temperature_2m_max,temperature_2m_min",
        "hourly": "temperature_2m",
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def om_daily_value(payload: dict, target_date: str, kind: str) -> float | None:
    daily = payload.get("daily", {})
    dates = daily.get("time", [])

    key = "temperature_2m_max" if kind == "high" else "temperature_2m_min"
    vals = daily.get(key, [])

    for d, v in zip(dates, vals):
        if d == target_date and v is not None:
            return float(v)

    return None


def om_hourly_extreme(payload: dict, target_date: str, kind: str) -> tuple[float | None, str | None]:
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])

    vals = []

    for t, temp in zip(times, temps):
        if temp is None:
            continue

        if str(t).startswith(target_date):
            vals.append((float(temp), t))

    if not vals:
        return None, None

    if kind == "high":
        temp, t = max(vals, key=lambda x: x[0])
    else:
        temp, t = min(vals, key=lambda x: x[0])

    return temp, t


def fetch_openmeteo_ensemble(lat: float, lon: float, tz: str) -> dict:
    url = "https://ensemble-api.open-meteo.com/v1/ensemble"

    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": tz,
        "temperature_unit": "fahrenheit",
        "forecast_days": 3,
        "hourly": "temperature_2m",
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def ensemble_member_extremes(payload: dict, target_date: str, kind: str) -> list[float]:
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])

    member_keys = [
        k for k in hourly.keys()
        if k.startswith("temperature_2m_member")
    ]

    extremes = []

    for key in member_keys:
        vals = hourly.get(key, [])
        day_vals = []

        for t, v in zip(times, vals):
            if v is None:
                continue

            if str(t).startswith(target_date):
                day_vals.append(float(v))

        if day_vals:
            if kind == "high":
                extremes.append(max(day_vals))
            else:
                extremes.append(min(day_vals))

    return extremes


def ensemble_bucket_summary(
    member_vals: list[float],
    bucket_low: float,
    bucket_high: float,
) -> dict:
    """
    Ensemble probability using CLI-rounded bucket intervals.
    """
    n = len(member_vals)
    raw_low, raw_high = raw_interval_for_cli_bucket(bucket_low, bucket_high)

    inside = [x for x in member_vals if raw_low <= x < raw_high]
    below = [x for x in member_vals if x < raw_low]
    above = [x for x in member_vals if x >= raw_high]

    mean = statistics.mean(member_vals) if n else math.nan
    sd = statistics.pstdev(member_vals) if n > 1 else 0.0 if n == 1 else math.nan

    p_yes = len(inside) / n if n else math.nan
    p_no = 1.0 - p_yes if n else math.nan

    return {
        "n_members": n,
        "n_inside": len(inside),
        "p_yes": p_yes,
        "p_no": p_no,
        "n_below": len(below),
        "p_below": len(below) / n if n else math.nan,
        "n_above": len(above),
        "p_above": len(above) / n if n else math.nan,
        "mean": mean,
        "sd": sd,
        "min": min(member_vals) if n else math.nan,
        "max": max(member_vals) if n else math.nan,
        "p10": percentile(member_vals, 0.10),
        "p50": percentile(member_vals, 0.50),
        "p90": percentile(member_vals, 0.90),
        "dist_mean_to_bucket": distance_to_cli_bucket(mean, bucket_low, bucket_high),
        "raw_low": raw_low,
        "raw_high": raw_high,
    }


# =============================
# NWS
# =============================

def fetch_nws_urls(lat: float, lon: float) -> dict:
    url = f"https://api.weather.gov/points/{lat},{lon}"

    r = requests.get(
        url,
        headers={"User-Agent": "tiered-ev-scan/1.0"},
        timeout=30,
    )
    r.raise_for_status()

    return r.json()["properties"]


def fetch_nws_hourly_extremes(
    lat: float,
    lon: float,
    tz: str,
) -> dict[str, dict[str, tuple[float, str]]]:
    props = fetch_nws_urls(lat, lon)
    hourly_url = props["forecastHourly"]

    r = requests.get(
        hourly_url,
        headers={"User-Agent": "tiered-ev-scan/1.0"},
        timeout=30,
    )
    r.raise_for_status()

    periods = r.json()["properties"]["periods"]

    by_date: dict[str, list[tuple[float, str]]] = {}

    for p in periods:
        start = p.get("startTime")
        temp = p.get("temperature")

        if start is None or temp is None:
            continue

        d = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(
            ZoneInfo(tz)
        ).date().isoformat()

        by_date.setdefault(d, []).append((float(temp), start))

    out = {}

    for d, vals in by_date.items():
        high_temp, high_time = max(vals, key=lambda x: x[0])
        low_temp, low_time = min(vals, key=lambda x: x[0])

        out[d] = {
            "high": (high_temp, high_time),
            "low": (low_temp, low_time),
        }

    return out


def fetch_nws_period_extremes(
    lat: float,
    lon: float,
    tz: str,
) -> dict[str, dict[str, float | None]]:
    props = fetch_nws_urls(lat, lon)
    forecast_url = props["forecast"]

    r = requests.get(
        forecast_url,
        headers={"User-Agent": "tiered-ev-scan/1.0"},
        timeout=30,
    )
    r.raise_for_status()

    periods = r.json()["properties"]["periods"]

    highs: dict[str, list[float]] = {}
    lows: dict[str, list[float]] = {}

    for p in periods:
        start = p.get("startTime")
        temp = p.get("temperature")
        is_daytime = p.get("isDaytime")

        if start is None or temp is None:
            continue

        d = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(
            ZoneInfo(tz)
        ).date().isoformat()

        if is_daytime:
            highs.setdefault(d, []).append(float(temp))
        else:
            lows.setdefault(d, []).append(float(temp))

    dates = set(highs) | set(lows)

    out = {}

    for d in dates:
        out[d] = {
            "high": max(highs[d]) if d in highs and highs[d] else None,
            "low": min(lows[d]) if d in lows and lows[d] else None,
        }

    return out


# =============================
# KALSHI
# =============================

def target_dates_for_station(tz: str) -> list[str]:
    now_local = datetime.now(ZoneInfo(tz)).date()

    return [
        (now_local + timedelta(days=i)).isoformat()
        for i in range(TARGET_DAYS)
    ]


def fetch_station_buckets(info: dict, target_dates: list[str], market_type: str) -> list[dict]:
    """
    Pull Kalshi contracts exactly through tracker.fetch_kalshi_markets(),
    same as simple_dislocation_scan.py.

    Dedupe only exact same ticker. Do not dedupe same label/price because that
    can hide a parsing/date/series problem.
    """
    key = "kalshi_high" if market_type == "high" else "kalshi_low"
    series = info.get(key, [])

    if not series:
        return []

    try:
        contracts = fetch_kalshi_markets(series, target_dates)
    except Exception as e:
        print(f"Kalshi fetch failed for {info.get('name', '')} {market_type}: {e}")
        return []

    out = []
    seen_tickers = set()

    for c in contracts:
        d = c.get("target_date")
        if d not in target_dates:
            continue

        ticker = c.get("ticker", "")

        if ticker in seen_tickers:
            continue

        seen_tickers.add(ticker)

        try:
            bucket_low = float(c["low"])
            bucket_high = float(c["high"])
            raw_low, raw_high = raw_interval_for_cli_bucket(bucket_low, bucket_high)

            out.append(
                {
                    "target_date": d,
                    "type": market_type,
                    "ticker": ticker,
                    "label": c["label"],
                    "bucket_low": bucket_low,
                    "bucket_high": bucket_high,
                    "raw_low": raw_low,
                    "raw_high": raw_high,
                    "yes": float(c["yes_ask"]),
                    "no": float(c["no_ask"]),
                }
            )

        except Exception as e:
            print(f"Skipping malformed Kalshi contract: {ticker} {e}")
            continue

    return out


# =============================
# SCAN
# =============================

def classify_tier(
    src_spread: float,
    ens_sd: float,
    source_vote: dict,
) -> str | None:
    if math.isnan(src_spread) or math.isnan(ens_sd):
        return None

    n_sources_inside = source_vote.get("n_sources_inside", 0)

    # Tier 1: tight sources, tight ensemble, and no point source lands in bucket.
    if (
        src_spread <= TIER1_MAX_SOURCE_SPREAD
        and ens_sd <= MAX_ENSEMBLE_SD_TIER1
        and n_sources_inside == 0
    ):
        return "TIER1"

    # Tier 2: still reasonably tight, but allow one point source in bucket.
    if (
        src_spread <= TIER2_MAX_SOURCE_SPREAD
        and ens_sd <= MAX_ENSEMBLE_SD_TIER2
        and n_sources_inside <= 1
    ):
        return "TIER2"

    return None


def scan_station(icao: str, info: dict) -> list[dict]:
    rows = []
    tz = info["tz"]
    target_dates = target_dates_for_station(tz)

    try:
        om_det = fetch_openmeteo_deterministic(info["lat"], info["lon"], tz)
        time.sleep(REQUEST_SLEEP)

        om_ens = fetch_openmeteo_ensemble(info["lat"], info["lon"], tz)
        time.sleep(REQUEST_SLEEP)

        nws_hourly = fetch_nws_hourly_extremes(info["lat"], info["lon"], tz)
        time.sleep(REQUEST_SLEEP)

        nws_period = fetch_nws_period_extremes(info["lat"], info["lon"], tz)
        time.sleep(REQUEST_SLEEP)

    except Exception as e:
        print(f"Forecast fetch failed for {icao} {info.get('name', '')}: {e}")
        return rows

    for market_type in ("high", "low"):
        buckets = fetch_station_buckets(info, target_dates, market_type)

        for b in buckets:
            yes = b["yes"]
            no = b["no"]

            if yes < MIN_YES_PRICE or yes > MAX_YES_PRICE:
                continue

            d = b["target_date"]

            om_daily = om_daily_value(om_det, d, market_type)
            om_hourly, om_hourly_time = om_hourly_extreme(om_det, d, market_type)

            nwsh_val = None
            nwsh_time = None

            if d in nws_hourly and market_type in nws_hourly[d]:
                nwsh_val, nwsh_time = nws_hourly[d][market_type]

            nwsp_val = None

            if d in nws_period:
                nwsp_val = nws_period[d].get(market_type)

            ens_vals = ensemble_member_extremes(om_ens, d, market_type)
            ens = ensemble_bucket_summary(ens_vals, b["bucket_low"], b["bucket_high"])

            if ens["n_members"] == 0:
                continue

            p_yes = ens["p_yes"]
            p_no = ens["p_no"]

            ev_y = ev_yes_cents(p_yes, yes)
            ev_n = ev_no_cents(p_no, no)

            if ev_n < MIN_EV_NO_CENTS:
                continue

            point_values = {
                "NWShr": nwsh_val,
                "NWSper": nwsp_val,
                "OMd": om_daily,
                "OMh": om_hourly,
                "EnsMu": ens["mean"],
            }

            src_sprd = source_spread(list(point_values.values()))

            src_vote = source_votes_for_bucket(
                point_values,
                b["bucket_low"],
                b["bucket_high"],
            )

            tier = classify_tier(src_sprd, ens["sd"], src_vote)

            if not tier:
                continue

            row = {
                "tier": tier,
                "icao": icao,
                "city": info["name"],
                "date": d,
                "type": market_type,
                "label": b["label"],
                "ticker": b["ticker"],
                "yes": yes,
                "no": no,
                "ev_no": ev_n,
                "ev_yes": ev_y,
                "ens_yes": p_yes,
                "ens_no": p_no,
                "ens_mean": ens["mean"],
                "ens_sd": ens["sd"],
                "ens_p10": ens["p10"],
                "ens_p90": ens["p90"],
                "n_members": ens["n_members"],
                "n_inside": ens["n_inside"],
                "n_below": ens["n_below"],
                "n_above": ens["n_above"],
                "raw_low": b["raw_low"],
                "raw_high": b["raw_high"],
                "nws_hourly": nwsh_val,
                "nws_period": nwsp_val,
                "om_daily": om_daily,
                "om_hourly": om_hourly,
                "source_spread": src_sprd,
                "n_sources_inside": src_vote["n_sources_inside"],
                "sources_inside": src_vote["sources_inside"],
            }

            rows.append(row)

    return rows


def scan_all() -> list[dict]:
    all_rows = []

    for icao, info in STATIONS.items():
        print(f"Scanning {icao} {info['name']}...")
        rows = scan_station(icao, info)
        all_rows.extend(rows)

    all_rows.sort(
        key=lambda r: (
            0 if r["tier"] == "TIER1" else 1,
            -r["ev_no"],
            r["source_spread"],
            r["icao"],
            r["label"],
        )
    )

    return all_rows


# =============================
# OUTPUT
# =============================

def print_tier(rows: list[dict], tier: str, top_n: int) -> None:
    subset = [r for r in rows if r["tier"] == tier]

    print()
    print(f"{tier} opportunities - top {top_n}")
    print("=" * 285)

    if not subset:
        print("None")
        return

    print(
        f"{'ICAO':<5} {'City':<16} {'Date':<10} {'T':<4} "
        f"{'Bucket':<16} {'RawInt':<15} "
        f"{'YES':>5} {'NO':>5} {'ensYES':>7} {'EV_N':>7} "
        f"{'mu':>6} {'sd':>5} {'p10-p90':>15} "
        f"{'NWShr':>6} {'NWSper':>6} {'OMd':>6} {'OMh':>6} "
        f"{'srcSprd':>7} {'srcIn':>5} {'nIn':>4} {'Ticker':<42} Summary"
    )

    print("-" * 285)

    for r in subset[:top_n]:
        p10p90 = f"{fmt(r['ens_p10'])}-{fmt(r['ens_p90'])}"
        raw_int = fmt_interval(r["raw_low"], r["raw_high"])
        src_inside = r["sources_inside"] if r["sources_inside"] else "-"

        summary = (
            f"{r['icao']} {r['type']} {r['label']} "
            f"(raw {raw_int}) | "
            f"NWS {fmt(r['nws_hourly'])}/{fmt(r['nws_period'])} "
            f"OM {fmt(r['om_daily'])}/{fmt(r['om_hourly'])} "
            f"Ens μ{fmt(r['ens_mean'])} σ{fmt(r['ens_sd'])} "
            f"p10–p90 {p10p90} | "
            f"ensYES {100 * r['ens_yes']:.0f}% ensNO {100 * r['ens_no']:.0f}% | "
            f"mkt YES {r['yes']:.0f}¢ NO {r['no']:.0f}¢ | "
            f"EV_N {r['ev_no']:+.1f}¢ | "
            f"srcSprd {r['source_spread']:.1f} | "
            f"srcInside {src_inside}"
        )

        print(
            f"{r['icao']:<5} {r['city']:<16} {r['date']:<10} {r['type']:<4} "
            f"{r['label']:<16} {raw_int:<15} "
            f"{r['yes']:>5.0f} {r['no']:>5.0f} "
            f"{100 * r['ens_yes']:>6.1f}% {r['ev_no']:>+7.1f} "
            f"{r['ens_mean']:>6.1f} {r['ens_sd']:>5.1f} {p10p90:>15} "
            f"{fmt(r['nws_hourly']):>6} {fmt(r['nws_period']):>6} "
            f"{fmt(r['om_daily']):>6} {fmt(r['om_hourly']):>6} "
            f"{r['source_spread']:>7.1f} {r['n_sources_inside']:>5} {r['n_inside']:>4} "
            f"{r['ticker']:<42} "
            f"{summary}"
        )


def main() -> None:
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"run_at={run_at}")
    print()

    rows = scan_all()

    print_tier(rows, "TIER1", TOP_N_PER_TIER)
    print_tier(rows, "TIER2", TOP_N_PER_TIER)


if __name__ == "__main__":
    main()