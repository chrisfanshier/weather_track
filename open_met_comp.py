from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests


# Test station: Seattle / KSEA
ICAO = "KSEA"
LAT = 47.4447
LON = -122.3138
TZ = "America/Los_Angeles"

# Example Kalshi-style bucket to test against
BUCKET_LOW = 62.0
BUCKET_HIGH = 63.0

# Placeholder market values; later these come from Kalshi
KALSHI_YES = 18.0
KALSHI_NO = 83.0


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def distance_to_bucket(value: float, low: float, high: float) -> float:
    if value < low:
        return low - value
    if value > high:
        return value - high
    return 0.0


def percentile(vals: list[float], p: float) -> float:
    if not vals:
        return math.nan
    vals = sorted(vals)
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return vals[int(k)]
    return vals[lo] * (hi - k) + vals[hi] * (k - lo)


def flatten_numeric_series(value):
    """
    Open-Meteo ensemble responses may come back as:
      - hourly.temperature_2m_member01 style fields
      - or arrays under named variables depending model/endpoint.

    This helper just extracts numeric arrays from JSON recursively.
    For the first test, we print keys and use likely member fields.
    """
    out = []

    if isinstance(value, list):
        if all(isinstance(x, (int, float)) or x is None for x in value):
            nums = [float(x) for x in value if x is not None]
            if nums:
                out.append(nums)
        else:
            for x in value:
                out.extend(flatten_numeric_series(x))

    elif isinstance(value, dict):
        for x in value.values():
            out.extend(flatten_numeric_series(x))

    return out


def fetch_openmeteo_ensemble_hourly(lat: float, lon: float, tz: str) -> dict:
    """
    Pull individual ensemble members.

    We request hourly temperature_2m members. Then we compute daily highs
    per member locally.

    If this endpoint/variable naming changes, the diagnostic key print below
    will show what came back.
    """
    url = "https://ensemble-api.open-meteo.com/v1/ensemble"

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": tz,
        "forecast_days": 3,
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def extract_member_daily_highs(payload: dict, target_date: str) -> list[float]:
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])

    if not times:
        return []

    # Candidate member fields. We exclude non-temperature metadata.
    member_keys = [
        k for k in hourly.keys()
        if k != "time" and "temperature_2m" in k
    ]

    print("\nReturned hourly keys:")
    for k in hourly.keys():
        print("  ", k)

    print("\nTemperature member-like keys:")
    for k in member_keys:
        print("  ", k)

    highs = []

    for key in member_keys:
        vals = hourly.get(key)
        if not isinstance(vals, list):
            continue

        day_vals = []
        for t, v in zip(times, vals):
            if v is None:
                continue
            if str(t).startswith(target_date):
                day_vals.append(float(v))

        if day_vals:
            highs.append(max(day_vals))

    return highs


def summarize_bucket(member_highs: list[float], bucket_low: float, bucket_high: float) -> dict:
    n = len(member_highs)

    inside = [x for x in member_highs if bucket_low <= x <= bucket_high]
    below = [x for x in member_highs if x < bucket_low]
    above = [x for x in member_highs if x > bucket_high]

    mean = statistics.mean(member_highs) if n else math.nan
    std = statistics.pstdev(member_highs) if n > 1 else 0.0 if n == 1 else math.nan

    return {
        "bucket_low": bucket_low,
        "bucket_high": bucket_high,
        "kalshi_yes": KALSHI_YES,
        "kalshi_no": KALSHI_NO,

        "n_members": n,
        "n_inside_bucket": len(inside),
        "p_inside_bucket": len(inside) / n if n else math.nan,
        "n_below_bucket": len(below),
        "p_below_bucket": len(below) / n if n else math.nan,
        "n_above_bucket": len(above),
        "p_above_bucket": len(above) / n if n else math.nan,

        "ensemble_mean": mean,
        "ensemble_std": std,
        "ensemble_min": min(member_highs) if n else math.nan,
        "ensemble_max": max(member_highs) if n else math.nan,
        "ensemble_p10": percentile(member_highs, 0.10),
        "ensemble_p50": percentile(member_highs, 0.50),
        "ensemble_p90": percentile(member_highs, 0.90),

        # Placeholder until NWS and deterministic Open-Meteo are wired in
        "nws_high": math.nan,
        "openmeteo_deterministic_high": math.nan,
        "distance_nws_to_bucket": math.nan,
        "distance_ensemble_mean_to_bucket": distance_to_bucket(mean, bucket_low, bucket_high)
        if n else math.nan,
    }


def main():
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Test target: today in station local time.
    target_date = datetime.now(ZoneInfo(TZ)).date().isoformat()

    print(f"run_at={run_at}")
    print(f"station={ICAO}")
    print(f"target_date={target_date}")
    print(f"bucket={BUCKET_LOW}-{BUCKET_HIGH}")

    payload = fetch_openmeteo_ensemble_hourly(LAT, LON, TZ)

    member_highs = extract_member_daily_highs(payload, target_date)

    print("\nMember daily highs:")
    for i, x in enumerate(member_highs, start=1):
        print(f"member_{i:02d}: {x:.2f} F")

    summary = summarize_bucket(member_highs, BUCKET_LOW, BUCKET_HIGH)

    print("\nBucket / ensemble summary:")
    for k, v in summary.items():
        if isinstance(v, float):
            if math.isnan(v):
                print(f"{k}:")
            elif k.startswith("p_"):
                print(f"{k}: {v:.3f}")
            else:
                print(f"{k}: {v:.2f}")
        else:
            print(f"{k}: {v}")


if __name__ == "__main__":
    main()