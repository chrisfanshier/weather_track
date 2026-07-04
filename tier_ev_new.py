from __future__ import annotations

import math
import re
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

TIER1_MAX_FAMILY_SPREAD = 3.0
TIER2_MAX_FAMILY_SPREAD = 5.0

REQUEST_SLEEP = 0.15

MODEL_FAMILIES = [
    "gfs_seamless",
    "icon_global",
    "gem_global",
    "ecmwf_ifs025",
    "ukmo_global_deterministic_10km",
]

MODEL_SHORT_NAMES = {
    "gfs_seamless": "GFS",
    "icon_global": "ICON",
    "gem_global": "GEM",
    "ecmwf_ifs025": "ECMWF",
    "ukmo_global_deterministic_10km": "UKMO",
}


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

def _get_with_retry(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 30,
    max_retries: int = 3,
    backoff: float = 2.0,
) -> requests.Response:
    """GET with exponential-backoff retry on transient 5xx errors (e.g. 502)."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code < 500:
                r.raise_for_status()
                return r
            last_exc = requests.HTTPError(f"HTTP {r.status_code}", response=r)
        except requests.exceptions.Timeout as exc:
            last_exc = exc
        if attempt < max_retries:
            time.sleep(backoff * (2 ** attempt))
    raise last_exc  # type: ignore[misc]


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

    r = _get_with_retry(url, params=params, timeout=30)
    return r.json()


def om_daily_value(payload: dict | None, target_date: str, kind: str) -> float | None:
    if payload is None:
        return None
    daily = payload.get("daily", {})
    dates = daily.get("time", [])

    key = "temperature_2m_max" if kind == "high" else "temperature_2m_min"
    vals = daily.get(key, [])

    for d, v in zip(dates, vals):
        if d == target_date and v is not None:
            return float(v)

    return None


def om_hourly_extreme(payload: dict | None, target_date: str, kind: str) -> tuple[float | None, str | None]:
    if payload is None:
        return None, None
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

    r = _get_with_retry(url, params=params, timeout=30)
    return r.json()


def ensemble_member_extremes(payload: dict | None, target_date: str, kind: str) -> list[float]:
    if payload is None:
        return []
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
            extremes.append(max(day_vals) if kind == "high" else min(day_vals))

    return extremes


def ensemble_bucket_summary(
    member_vals: list[float],
    bucket_low: float,
    bucket_high: float,
) -> dict:
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
# MODEL FAMILY FORECASTS
# =============================

def fetch_openmeteo_model_families(lat: float, lon: float, tz: str) -> dict:
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": tz,
        "temperature_unit": "fahrenheit",
        "forecast_days": 3,
        "daily": "temperature_2m_max,temperature_2m_min",
        "models": ",".join(MODEL_FAMILIES),
    }

    r = _get_with_retry(url, params=params, timeout=30)
    return r.json()


def model_family_values(payload: dict | None, target_date: str, kind: str) -> dict[str, float]:
    if payload is None:
        return {}
    daily = payload.get("daily", {})
    dates = daily.get("time", [])

    out: dict[str, float] = {}

    for model in MODEL_FAMILIES:
        key = (
            f"temperature_2m_max_{model}"
            if kind == "high"
            else f"temperature_2m_min_{model}"
        )

        vals = daily.get(key)
        if not vals:
            continue

        for d, v in zip(dates, vals):
            if d == target_date and v is not None:
                out[model] = float(v)
                break

    return out


def model_family_bucket_summary(
    values: dict[str, float],
    low: float,
    high: float,
) -> dict:
    if not values:
        return {
            "family_n": 0,
            "family_min": math.nan,
            "family_max": math.nan,
            "family_mean": math.nan,
            "family_spread": math.nan,
            "family_inside": 0,
            "family_inside_names": "",
            "family_compact": "",
        }

    raw_low, raw_high = raw_interval_for_cli_bucket(low, high)
    vals = list(values.values())

    inside = {
        name: val
        for name, val in values.items()
        if raw_low <= val < raw_high
    }

    compact = " ".join(
        f"{MODEL_SHORT_NAMES.get(name, name)}:{val:.1f}"
        for name, val in values.items()
    )

    return {
        "family_n": len(vals),
        "family_min": min(vals),
        "family_max": max(vals),
        "family_mean": statistics.mean(vals),
        "family_spread": max(vals) - min(vals),
        "family_inside": len(inside),
        "family_inside_names": ",".join(
            MODEL_SHORT_NAMES.get(name, name)
            for name in inside.keys()
        ),
        "family_compact": compact,
    }


# =============================
# NWS
# =============================

def fetch_nws_urls(lat: float, lon: float) -> dict:
    url = f"https://api.weather.gov/points/{lat},{lon}"

    r = _get_with_retry(
        url,
        headers={"User-Agent": "tiered-ev-scan/1.0"},
        timeout=30,
        max_retries=2,
    )
    return r.json()["properties"]


def fetch_nws_hourly_extremes(
    lat: float,
    lon: float,
    tz: str,
) -> dict[str, dict[str, tuple[float, str]]]:
    props = fetch_nws_urls(lat, lon)
    hourly_url = props["forecastHourly"]

    r = _get_with_retry(
        hourly_url,
        headers={"User-Agent": "tiered-ev-scan/1.0"},
        timeout=30,
        max_retries=2,
    )

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

    r = _get_with_retry(
        forecast_url,
        headers={"User-Agent": "tiered-ev-scan/1.0"},
        timeout=30,
        max_retries=2,
    )

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


def parse_kalshi_date_from_text(text: str) -> str | None:
    """
    Parse Kalshi ticker dates like:
      KXHIGHNY-26JUN02-B79.5
      KXHIGHNY-26JUN03-B80.5

    Returns YYYY-MM-DD.
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


def infer_event_key_from_ticker(ticker: str) -> str:
    """
    Example:
      KXHIGHNY-26JUN02-B79.5
    becomes:
      KXHIGHNY-26JUN02
    """
    if not ticker:
        return ""

    m = re.search(r"^(.*?\d{2}[A-Z]{3}\d{2})", ticker.upper())
    if m:
        return m.group(1)

    parts = ticker.split("-")
    if len(parts) <= 1:
        return ticker

    return "-".join(parts[:-1])


def target_dates_for_station(tz: str) -> list[str]:
    now_local = datetime.now(ZoneInfo(tz)).date()

    return [
        (now_local + timedelta(days=i)).isoformat()
        for i in range(TARGET_DAYS)
    ]


def fetch_station_buckets(info: dict, target_dates: list[str], market_type: str) -> list[dict]:
    """
    Pull Kalshi contracts, but verify true event date from ticker.

    Important:
    tracker.fetch_kalshi_markets() may return today/tomorrow groups together
    and its c["target_date"] field may be unreliable. The ticker date is safer.

    Example:
      KXHIGHNY-26JUN02-B79.5 -> 2026-06-02
      KXHIGHNY-26JUN03-B80.5 -> 2026-06-03
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
        ticker = c.get("ticker", "")
        event_key = c.get("event_ticker") or infer_event_key_from_ticker(ticker)

        event_date = (
            parse_kalshi_date_from_text(event_key)
            or parse_kalshi_date_from_text(ticker)
        )

        if event_date not in target_dates:
            continue

        if ticker in seen_tickers:
            continue

        seen_tickers.add(ticker)

        try:
            bucket_low = float(c["low"])
            bucket_high = float(c["high"])
            raw_low, raw_high = raw_interval_for_cli_bucket(bucket_low, bucket_high)

            out.append(
                {
                    "target_date": event_date,
                    "event_key": event_key,
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

    out.sort(
        key=lambda r: (
            r["target_date"],
            r["bucket_low"],
            r["bucket_high"],
            r["ticker"],
        )
    )

    return out


# =============================
# SCAN
# =============================

def classify_tier(
    src_spread: float,
    ens_sd: float,
    source_vote: dict,
    family_summary: dict,
) -> str | None:
    if math.isnan(src_spread) or math.isnan(ens_sd):
        return None

    n_sources_inside = source_vote.get("n_sources_inside", 0)

    family_n = family_summary.get("family_n", 0)
    family_inside = family_summary.get("family_inside", 0)
    family_spread = family_summary.get("family_spread", math.nan)

    # family_n == 0 means model-family data was unavailable — degrade gracefully
    # by skipping family filter rather than blocking all results.
    family_ok_t1 = family_n == 0 or (
        not math.isnan(family_spread)
        and family_inside == 0
        and family_spread <= TIER1_MAX_FAMILY_SPREAD
    )
    family_ok_t2 = family_n == 0 or (
        not math.isnan(family_spread)
        and family_inside <= 1
        and family_spread <= TIER2_MAX_FAMILY_SPREAD
    )

    if (
        src_spread <= TIER1_MAX_SOURCE_SPREAD
        and ens_sd <= MAX_ENSEMBLE_SD_TIER1
        and n_sources_inside == 0
        and family_ok_t1
    ):
        return "TIER1"

    if (
        src_spread <= TIER2_MAX_SOURCE_SPREAD
        and ens_sd <= MAX_ENSEMBLE_SD_TIER2
        and n_sources_inside <= 1
        and family_ok_t2
    ):
        return "TIER2"

    return None


def scan_station(icao: str, info: dict) -> list[dict]:
    rows = []
    tz = info["tz"]
    target_dates = target_dates_for_station(tz)

    om_det = None
    try:
        om_det = fetch_openmeteo_deterministic(info["lat"], info["lon"], tz)
        time.sleep(REQUEST_SLEEP)
    except Exception as e:
        print(f"  [{icao}] OM deterministic unavailable: {e}")

    om_ens = None
    try:
        om_ens = fetch_openmeteo_ensemble(info["lat"], info["lon"], tz)
        time.sleep(REQUEST_SLEEP)
    except Exception as e:
        print(f"  [{icao}] OM ensemble unavailable: {e}")

    om_family = None
    try:
        om_family = fetch_openmeteo_model_families(info["lat"], info["lon"], tz)
        time.sleep(REQUEST_SLEEP)
    except Exception as e:
        print(f"  [{icao}] OM model families unavailable: {e}")

    nws_hourly: dict = {}
    try:
        nws_hourly = fetch_nws_hourly_extremes(info["lat"], info["lon"], tz)
        time.sleep(REQUEST_SLEEP)
    except Exception as e:
        print(f"  [{icao}] NWS hourly unavailable: {e}")

    nws_period: dict = {}
    try:
        nws_period = fetch_nws_period_extremes(info["lat"], info["lon"], tz)
        time.sleep(REQUEST_SLEEP)
    except Exception as e:
        print(f"  [{icao}] NWS periods unavailable: {e}")

    if (om_det is None and om_ens is None and om_family is None
            and not nws_hourly and not nws_period):
        print(f"  [{icao}] All forecast sources failed — skipping station")
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

            family_values = model_family_values(om_family, d, market_type)
            family = model_family_bucket_summary(
                family_values,
                b["bucket_low"],
                b["bucket_high"],
            )

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

            tier = classify_tier(src_sprd, ens["sd"], src_vote, family)

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
                "event_key": b["event_key"],
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
                "family_n": family["family_n"],
                "family_min": family["family_min"],
                "family_max": family["family_max"],
                "family_mean": family["family_mean"],
                "family_spread": family["family_spread"],
                "family_inside": family["family_inside"],
                "family_inside_names": family["family_inside_names"],
                "family_compact": family["family_compact"],
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
            r["date"],
            -r["ev_no"],
            r["source_spread"],
            r["family_spread"],
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
    print("=" * 380)

    if not subset:
        print("None")
        return

    print(
        f"{'ICAO':<5} {'City':<16} {'Date':<10} {'T':<4} "
        f"{'Bucket':<16} {'RawInt':<15} "
        f"{'YES':>5} {'NO':>5} {'ensYES':>7} {'EV_N':>7} "
        f"{'mu':>6} {'sd':>5} {'p10-p90':>15} "
        f"{'NWShr':>6} {'NWSper':>6} {'OMd':>6} {'OMh':>6} "
        f"{'srcSprd':>7} {'srcIn':>5} "
        f"{'FamMin':>7} {'FamMax':>7} {'FamSprd':>8} {'FamIn':>5} "
        f"{'nIn':>4} {'Ticker':<42} Summary"
    )

    print("-" * 380)

    for r in subset[:top_n]:
        p10p90 = f"{fmt(r['ens_p10'])}-{fmt(r['ens_p90'])}"
        raw_int = fmt_interval(r["raw_low"], r["raw_high"])
        src_inside = r["sources_inside"] if r["sources_inside"] else "-"
        fam_inside = r["family_inside_names"] if r["family_inside_names"] else "-"

        summary = (
            f"{r['icao']} {r['type']} {r['label']} "
            f"(raw {raw_int}) | "
            f"NWS {fmt(r['nws_hourly'])}/{fmt(r['nws_period'])} "
            f"OM {fmt(r['om_daily'])}/{fmt(r['om_hourly'])} "
            f"Ens μ{fmt(r['ens_mean'])} σ{fmt(r['ens_sd'])} "
            f"p10–p90 {p10p90} | "
            f"Fam {r['family_compact']} | "
            f"ensYES {100 * r['ens_yes']:.0f}% ensNO {100 * r['ens_no']:.0f}% | "
            f"mkt YES {r['yes']:.0f}¢ NO {r['no']:.0f}¢ | "
            f"EV_N {r['ev_no']:+.1f}¢ | "
            f"srcSprd {r['source_spread']:.1f} | "
            f"srcInside {src_inside} | "
            f"famInside {fam_inside} | "
            f"event {r['event_key']}"
        )

        print(
            f"{r['icao']:<5} {r['city']:<16} {r['date']:<10} {r['type']:<4} "
            f"{r['label']:<16} {raw_int:<15} "
            f"{r['yes']:>5.0f} {r['no']:>5.0f} "
            f"{100 * r['ens_yes']:>6.1f}% {r['ev_no']:>+7.1f} "
            f"{r['ens_mean']:>6.1f} {r['ens_sd']:>5.1f} {p10p90:>15} "
            f"{fmt(r['nws_hourly']):>6} {fmt(r['nws_period']):>6} "
            f"{fmt(r['om_daily']):>6} {fmt(r['om_hourly']):>6} "
            f"{r['source_spread']:>7.1f} {r['n_sources_inside']:>5} "
            f"{fmt(r['family_min']):>7} {fmt(r['family_max']):>7} "
            f"{fmt(r['family_spread']):>8} {r['family_inside']:>5} "
            f"{r['n_inside']:>4} "
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