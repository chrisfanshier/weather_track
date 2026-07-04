from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from tracker import (
    STATIONS,
    fetch_kalshi_markets,
)


# =============================
# TEST CONFIG
# =============================

ICAO = "KSEA"
INFO = STATIONS[ICAO]

STATION = {
    "icao": ICAO,
    "name": INFO["name"],
    "lat": INFO["lat"],
    "lon": INFO["lon"],
    "tz": INFO["tz"],
    "kalshi_high_series": INFO["kalshi_high"],
}

TARGET_DATE = datetime.now(ZoneInfo(STATION["tz"])).date().isoformat()


# =============================
# HELPERS
# =============================

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


def distance_to_bucket(value: float | None, low: float, high: float) -> float:
    if value is None or math.isnan(value):
        return math.nan

    if value < low:
        return low - value

    if value > high:
        return value - high

    return 0.0


def fmt(x, digits: int = 1) -> str:
    if x is None:
        return "NA"

    if isinstance(x, float) and math.isnan(x):
        return "NA"

    return f"{x:.{digits}f}"


def ev_yes_cents(model_prob_yes: float, yes_price: float) -> float:
    return 100.0 * model_prob_yes - yes_price


def ev_no_cents(model_prob_no: float, no_price: float) -> float:
    return 100.0 * model_prob_no - no_price


# =============================
# KALSHI VIA YOUR TRACKER
# =============================

def fetch_kalshi_buckets_for_station(station: dict, target_date: str) -> list[dict]:
    contracts = fetch_kalshi_markets(station["kalshi_high_series"], [target_date])

    buckets = []

    for c in contracts:
        if c.get("target_date") != target_date:
            continue

        buckets.append(
            {
                "ticker": c["ticker"],
                "label": c["label"],
                "bucket_low": float(c["low"]),
                "bucket_high": float(c["high"]),
                "kalshi_yes": float(c["yes_ask"]),
                "kalshi_no": float(c["no_ask"]),
            }
        )

    buckets.sort(key=lambda x: (x["bucket_low"], x["bucket_high"]))

    return buckets


# =============================
# OPEN-METEO DETERMINISTIC
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


def openmeteo_daily_high(payload: dict, target_date: str) -> float | None:
    daily = payload.get("daily", {})
    dates = daily.get("time", [])
    highs = daily.get("temperature_2m_max", [])

    for d, h in zip(dates, highs):
        if d == target_date and h is not None:
            return float(h)

    return None


def openmeteo_hourly_max(payload: dict, target_date: str) -> tuple[float | None, str | None]:
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

    temp, t = max(vals, key=lambda x: x[0])

    return temp, t


# =============================
# OPEN-METEO ENSEMBLE
# =============================

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


def ensemble_member_highs(payload: dict, target_date: str) -> list[float]:
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])

    # Explicit ensemble members only. This excludes the base/control `temperature_2m`.
    member_keys = [
        k for k in hourly.keys()
        if k.startswith("temperature_2m_member")
    ]

    highs = []

    for key in member_keys:
        vals = hourly.get(key, [])
        day_vals = []

        for t, v in zip(times, vals):
            if v is None:
                continue

            if str(t).startswith(target_date):
                day_vals.append(float(v))

        if day_vals:
            highs.append(max(day_vals))

    return highs


def ensemble_summary(member_highs: list[float], bucket_low: float, bucket_high: float) -> dict:
    n = len(member_highs)

    inside = [x for x in member_highs if bucket_low <= x <= bucket_high]
    below = [x for x in member_highs if x < bucket_low]
    above = [x for x in member_highs if x > bucket_high]

    mean = statistics.mean(member_highs) if n else math.nan
    std = statistics.pstdev(member_highs) if n > 1 else 0.0 if n == 1 else math.nan

    return {
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
        "distance_ensemble_mean_to_bucket": distance_to_bucket(mean, bucket_low, bucket_high),
    }


# =============================
# NWS
# =============================

def fetch_nws_urls(lat: float, lon: float) -> dict:
    url = f"https://api.weather.gov/points/{lat},{lon}"

    r = requests.get(
        url,
        headers={"User-Agent": "forecast-predict-test/1.0"},
        timeout=30,
    )
    r.raise_for_status()

    return r.json()["properties"]


def fetch_nws_hourly_max(lat: float, lon: float, tz: str, target_date: str) -> tuple[float | None, str | None]:
    props = fetch_nws_urls(lat, lon)
    hourly_url = props["forecastHourly"]

    r = requests.get(
        hourly_url,
        headers={"User-Agent": "forecast-predict-test/1.0"},
        timeout=30,
    )
    r.raise_for_status()

    periods = r.json()["properties"]["periods"]

    vals = []

    for p in periods:
        start = p.get("startTime")
        temp = p.get("temperature")

        if start is None or temp is None:
            continue

        local_date = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(
            ZoneInfo(tz)
        ).date().isoformat()

        if local_date == target_date:
            vals.append((float(temp), start))

    if not vals:
        return None, None

    temp, t = max(vals, key=lambda x: x[0])

    return temp, t


def fetch_nws_period_high(lat: float, lon: float, tz: str, target_date: str) -> float | None:
    props = fetch_nws_urls(lat, lon)
    forecast_url = props["forecast"]

    r = requests.get(
        forecast_url,
        headers={"User-Agent": "forecast-predict-test/1.0"},
        timeout=30,
    )
    r.raise_for_status()

    periods = r.json()["properties"]["periods"]

    vals = []

    for p in periods:
        start = p.get("startTime")
        temp = p.get("temperature")
        is_daytime = p.get("isDaytime")

        if start is None or temp is None or not is_daytime:
            continue

        local_date = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(
            ZoneInfo(tz)
        ).date().isoformat()

        if local_date == target_date:
            vals.append(float(temp))

    return max(vals) if vals else None


# =============================
# MAIN
# =============================

def main() -> None:
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print(f"run_at={run_at}")
    print(f"station={STATION['icao']} {STATION['name']}")
    print(f"target_date={TARGET_DATE}")
    print()

    print("Pulling Kalshi...")
    buckets = fetch_kalshi_buckets_for_station(STATION, TARGET_DATE)
    print(f"Kalshi buckets found for {TARGET_DATE}: {len(buckets)}")

    print("Pulling Open-Meteo deterministic...")
    om_det = fetch_openmeteo_deterministic(
        STATION["lat"],
        STATION["lon"],
        STATION["tz"],
    )
    om_daily = openmeteo_daily_high(om_det, TARGET_DATE)
    om_hourly, om_hourly_time = openmeteo_hourly_max(om_det, TARGET_DATE)

    print("Pulling Open-Meteo ensemble...")
    om_ens = fetch_openmeteo_ensemble(
        STATION["lat"],
        STATION["lon"],
        STATION["tz"],
    )
    member_highs = ensemble_member_highs(om_ens, TARGET_DATE)

    print("Pulling NWS...")
    nws_hourly, nws_hourly_time = fetch_nws_hourly_max(
        STATION["lat"],
        STATION["lon"],
        STATION["tz"],
        TARGET_DATE,
    )
    nws_period = fetch_nws_period_high(
        STATION["lat"],
        STATION["lon"],
        STATION["tz"],
        TARGET_DATE,
    )

    print()
    print(f"OM daily high:   {fmt(om_daily)}")
    print(f"OM hourly max:   {fmt(om_hourly)} at {om_hourly_time or 'NA'}")
    print(f"NWS hourly max:  {fmt(nws_hourly)} at {nws_hourly_time or 'NA'}")
    print(f"NWS period high: {fmt(nws_period)}")
    print(f"Ensemble members: {len(member_highs)}")
    print()

    if not buckets:
        print("No Kalshi buckets found. Forecast pulls worked, but Kalshi returned no normalized buckets.")
        return

    print(
        f"{'Bucket':<16} {'YES':>5} {'NO':>5} "
        f"{'ensYES':>7} {'ensNO':>7} "
        f"{'mu':>6} {'sd':>5} {'p10-p90':>15} "
        f"{'NWS':>5} {'OMdet':>6} {'EV_Y':>7} {'EV_N':>7} Summary"
    )
    print("-" * 160)

    for b in buckets:
        low = b["bucket_low"]
        high = b["bucket_high"]

        s = ensemble_summary(member_highs, low, high)

        p_yes = s["p_inside_bucket"]
        p_no = 1.0 - p_yes if not math.isnan(p_yes) else math.nan

        yes_price = b["kalshi_yes"]
        no_price = b["kalshi_no"]

        ev_y = ev_yes_cents(p_yes, yes_price) if not math.isnan(p_yes) else math.nan
        ev_n = ev_no_cents(p_no, no_price) if not math.isnan(p_no) else math.nan

        p10_p90 = f"{fmt(s['ensemble_p10'])}-{fmt(s['ensemble_p90'])}"

        summary = (
            f"{STATION['icao']} high {b['label']} | "
            f"NWS {fmt(nws_hourly)} / OMdet {fmt(om_daily)} / "
            f"OMens μ{fmt(s['ensemble_mean'])} σ{fmt(s['ensemble_std'])} "
            f"p10–p90 {p10_p90} | "
            f"ens YES {100 * p_yes:.0f}% NO {100 * p_no:.0f}% | "
            f"mkt YES {yes_price:.0f}¢ NO {no_price:.0f}¢ | "
            f"EV_Y {ev_y:+.1f}¢ EV_N {ev_n:+.1f}¢"
        )

        print(
            f"{b['label']:<16} {yes_price:>5.0f} {no_price:>5.0f} "
            f"{100 * p_yes:>6.1f}% {100 * p_no:>6.1f}% "
            f"{s['ensemble_mean']:>6.1f} {s['ensemble_std']:>5.1f} "
            f"{p10_p90:>15} "
            f"{fmt(nws_hourly):>5} {fmt(om_daily):>6} "
            f"{ev_y:>+7.1f} {ev_n:>+7.1f} {summary}"
        )


if __name__ == "__main__":
    main()