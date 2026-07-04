"""
live_hot_cold.py

Scan all tracker.py stations and report whether each station is currently
running hot/cold versus NWS hourly, Open-Meteo deterministic hourly, and
Open-Meteo named model-family hourly forecasts.

Outputs one row per station:

ICAO | City | ObsTime | Obs | NWS | OM | FamMean | FamMin | FamMax | dNWS | dOM | dFam | AvgD | Status | Error

Usage:
    python live_hot_cold.py
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from tracker import STATIONS


NWS_HEADERS = {"User-Agent": "live-hot-cold-scan/1.0"}

# IEM will rate-limit if hammered.
REQUEST_SLEEP = 2.0
IEM_HOURS = 3

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

def fmt(x, digits=1):
    if x is None:
        return "NA"
    if isinstance(x, float) and math.isnan(x):
        return "NA"
    return f"{x:.{digits}f}"


def parse_tgroup_f(raw: str | None) -> float | None:
    """
    Parse precise METAR T group:
      T02500117 -> temp 25.0C, dewpoint 11.7C
      T02400120 -> temp 24.0C, dewpoint 12.0C

    Handles negative C per METAR convention:
      first digit 0 = positive
      first digit 1 = negative
    """
    m = re.search(r"T(\d{4})(\d{4})", raw or "")
    if not m:
        return None

    block = m.group(1)

    if block.startswith("1"):
        c = -1 * (int(block[1:]) / 10)
    else:
        c = int(block[1:]) / 10

    return c * 9 / 5 + 32


def safe_mean(vals: list[float]) -> float:
    clean = [
        float(x)
        for x in vals
        if x is not None and not (isinstance(x, float) and math.isnan(x))
    ]
    if not clean:
        return math.nan
    return sum(clean) / len(clean)


def classify(delta_nws: float, delta_om: float, delta_family: float) -> str:
    """
    Classification uses all available deltas.

    HOT means station is materially above the forecast path.
    COLD means station is materially below the forecast path.
    """
    vals = [
        x
        for x in [delta_nws, delta_om, delta_family]
        if x is not None and not (isinstance(x, float) and math.isnan(x))
    ]

    if not vals:
        return "UNKNOWN"

    avg = sum(vals) / len(vals)

    if avg >= 3.0:
        return "HOT"
    if avg >= 1.5:
        return "SLIGHT_HOT"
    if avg <= -3.0:
        return "COLD"
    if avg <= -1.5:
        return "SLIGHT_COLD"

    return "NEAR"


# =============================
# OBSERVATIONS
# =============================

def fetch_recent_hf_asos(icao: str, info: dict, hours: int = IEM_HOURS) -> str:
    """
    Pull recent ASOS/MADISHF rows from IEM.
    Uses tmpf + raw metar text. T-group is preferred when present.
    Retries softly on 429 rate limits.
    """
    station = icao[1:] if icao.startswith("K") else icao

    now = datetime.now(ZoneInfo(info["tz"]))
    start = now - timedelta(hours=hours)

    url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

    params = {
        "station": station,
        "data": ["tmpf", "metar"],
        "year1": start.year,
        "month1": start.month,
        "day1": start.day,
        "hour1": start.hour,
        "minute1": start.minute,
        "year2": now.year,
        "month2": now.month,
        "day2": now.day,
        "hour2": now.hour,
        "minute2": now.minute,
        "tz": info["tz"],
        "format": "onlycomma",
        "latlon": "no",
        "elev": "no",
        "missing": "M",
        "trace": "T",
        "direct": "no",
        "report_type": ["1", "2", "3"],
    }

    last_error = None

    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=25)

            if r.status_code == 429:
                wait = 5 + attempt * 5
                last_error = RuntimeError(f"429 Too Many Requests; waited {wait}s")
                time.sleep(wait)
                continue

            r.raise_for_status()
            return r.text

        except Exception as e:
            last_error = e
            time.sleep(3 + attempt * 3)

    raise RuntimeError(f"IEM ASOS failed for {icao}: {last_error}")


def parse_hf_rows(txt: str, info: dict) -> list[dict]:
    rows = []
    tz = ZoneInfo(info["tz"])

    for line in txt.splitlines():
        if not line or line.startswith("station"):
            continue

        parts = line.split(",", 3)
        if len(parts) < 4:
            continue

        station, valid, tmpf, raw = parts

        # Prefer precise T-group if present; fall back to tmpf.
        val = parse_tgroup_f(raw)

        if val is None and tmpf != "M":
            try:
                val = float(tmpf)
            except Exception:
                val = None

        try:
            dt = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        except Exception:
            dt = None

        if val is not None:
            rows.append(
                {
                    "valid": valid,
                    "dt": dt,
                    "temp_f": val,
                    "tmpf": tmpf,
                    "raw": raw,
                }
            )

    return rows


def latest_obs(rows: list[dict]) -> dict | None:
    if not rows:
        return None

    dated = [r for r in rows if r["dt"] is not None]

    if dated:
        return max(dated, key=lambda x: x["dt"])

    return rows[-1]


# =============================
# NWS HOURLY
# =============================

def fetch_nws_hourly_curve(info: dict) -> list[tuple[float, str]]:
    """
    Returns list of (temp_f, startTime).
    """
    url = f"https://api.weather.gov/gridpoints/{info['nws_grid']}/forecast/hourly"

    r = requests.get(url, headers=NWS_HEADERS, timeout=25)
    r.raise_for_status()

    out = []

    for p in r.json()["properties"]["periods"]:
        temp = p.get("temperature")
        start = p.get("startTime")

        if temp is None or start is None:
            continue

        temp_f = float(temp)

        if p.get("temperatureUnit") == "C":
            temp_f = temp_f * 9 / 5 + 32

        out.append((temp_f, start))

    return out


def nearest_nws_value(nws_curve: list[tuple[float, str]], obs_dt: datetime | None):
    if not nws_curve or obs_dt is None:
        return None, None

    best = None

    for val, start in nws_curve:
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except Exception:
            continue

        diff = abs((dt - obs_dt).total_seconds())

        if best is None or diff < best[0]:
            best = (diff, start, val)

    if best is None:
        return None, None

    return best[2], best[1]


# =============================
# OPEN-METEO DETERMINISTIC HOURLY
# =============================

def fetch_openmeteo_hourly_curve(info: dict) -> list[tuple[str, float]]:
    """
    Returns list of (local_time_string, temp_f).
    Open-Meteo hourly times are local naive strings when timezone is supplied.
    """
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": info["lat"],
        "longitude": info["lon"],
        "timezone": info["tz"],
        "temperature_unit": "fahrenheit",
        "forecast_days": 2,
        "hourly": "temperature_2m",
    }

    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()

    hourly = r.json().get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])

    out = []

    for t, v in zip(times, temps):
        if v is not None:
            out.append((t, float(v)))

    return out


def nearest_om_value(
    om_curve: list[tuple[str, float]],
    obs_dt: datetime | None,
    tz_name: str,
):
    if not om_curve or obs_dt is None:
        return None, None

    tz = ZoneInfo(tz_name)
    best = None

    for t, v in om_curve:
        try:
            dt = datetime.fromisoformat(t).replace(tzinfo=tz)
        except Exception:
            continue

        diff = abs((dt - obs_dt.astimezone(tz)).total_seconds())

        if best is None or diff < best[0]:
            best = (diff, t, v)

    if best is None:
        return None, None

    return best[2], best[1]


# =============================
# OPEN-METEO NAMED MODEL FAMILIES
# =============================

def fetch_model_family_hourly_payload(info: dict) -> dict:
    """
    Pull named deterministic model-family hourly forecasts:
      GFS, ICON, GEM, ECMWF, UKMO

    This is separate from the Open-Meteo ensemble API.
    """
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": info["lat"],
        "longitude": info["lon"],
        "timezone": info["tz"],
        "temperature_unit": "fahrenheit",
        "forecast_days": 2,
        "hourly": "temperature_2m",
        "models": ",".join(MODEL_FAMILIES),
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def find_model_hourly_key(hourly: dict, model: str) -> str | None:
    """
    Open-Meteo usually returns:
      temperature_2m_gfs_seamless
      temperature_2m_icon_global
      etc.

    This helper is intentionally tolerant in case naming changes slightly.
    """
    exact = f"temperature_2m_{model}"

    if exact in hourly:
        return exact

    # Fallback: find a key containing the model name.
    for key in hourly.keys():
        if key.startswith("temperature_2m") and model in key:
            return key

    return None


def model_family_curves(payload: dict) -> dict[str, list[tuple[str, float]]]:
    """
    Convert model-family hourly payload to:

      {
        "gfs_seamless": [("2026-05-29T09:00", 78.2), ...],
        ...
      }
    """
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])

    out: dict[str, list[tuple[str, float]]] = {}

    for model in MODEL_FAMILIES:
        key = find_model_hourly_key(hourly, model)
        if not key:
            continue

        vals = hourly.get(key, [])

        curve = []

        for t, v in zip(times, vals):
            if v is not None:
                curve.append((t, float(v)))

        if curve:
            out[model] = curve

    return out


def nearest_model_family_values(
    curves: dict[str, list[tuple[str, float]]],
    obs_dt: datetime | None,
    tz_name: str,
) -> dict[str, float]:
    """
    Return nearest forecast value for each named model at obs_dt.
    """
    if obs_dt is None:
        return {}

    tz = ZoneInfo(tz_name)
    out: dict[str, float] = {}

    for model, curve in curves.items():
        best = None

        for t, v in curve:
            try:
                dt = datetime.fromisoformat(t).replace(tzinfo=tz)
            except Exception:
                continue

            diff = abs((dt - obs_dt.astimezone(tz)).total_seconds())

            if best is None or diff < best[0]:
                best = (diff, t, v)

        if best is not None:
            out[model] = best[2]

    return out


def summarize_family_now(values: dict[str, float]) -> dict:
    if not values:
        return {
            "family_n": 0,
            "family_mean": math.nan,
            "family_min": math.nan,
            "family_max": math.nan,
            "family_spread": math.nan,
            "family_compact": "",
        }

    vals = list(values.values())

    compact = " ".join(
        f"{MODEL_SHORT_NAMES.get(model, model)}:{val:.1f}"
        for model, val in values.items()
    )

    return {
        "family_n": len(vals),
        "family_mean": sum(vals) / len(vals),
        "family_min": min(vals),
        "family_max": max(vals),
        "family_spread": max(vals) - min(vals),
        "family_compact": compact,
    }


# =============================
# SCAN
# =============================

def scan_station(icao: str, info: dict) -> dict:
    try:
        hf_text = fetch_recent_hf_asos(icao, info)
        hf_rows = parse_hf_rows(hf_text, info)
        obs = latest_obs(hf_rows)

        if not obs or not obs["dt"]:
            raise RuntimeError("no recent obs")

        # NWS hourly now
        nws_curve = fetch_nws_hourly_curve(info)
        nws_now, nws_time = nearest_nws_value(nws_curve, obs["dt"])

        # Open-Meteo deterministic hourly now
        om_curve = fetch_openmeteo_hourly_curve(info)
        om_now, om_time = nearest_om_value(om_curve, obs["dt"], info["tz"])

        # Named model-family hourly now
        fam_payload = fetch_model_family_hourly_payload(info)
        fam_curves = model_family_curves(fam_payload)
        fam_values = nearest_model_family_values(fam_curves, obs["dt"], info["tz"])
        fam = summarize_family_now(fam_values)

        delta_nws = obs["temp_f"] - nws_now if nws_now is not None else math.nan
        delta_om = obs["temp_f"] - om_now if om_now is not None else math.nan
        delta_family = (
            obs["temp_f"] - fam["family_mean"]
            if fam["family_n"] > 0 and not math.isnan(fam["family_mean"])
            else math.nan
        )

        vals = [
            x
            for x in [delta_nws, delta_om, delta_family]
            if not (isinstance(x, float) and math.isnan(x))
        ]

        avg_delta = sum(vals) / len(vals) if vals else math.nan

        return {
            "icao": icao,
            "city": info["name"],
            "local_time": obs["valid"],
            "latest_obs": obs["temp_f"],
            "nws_now": nws_now,
            "om_now": om_now,
            "family_mean": fam["family_mean"],
            "family_min": fam["family_min"],
            "family_max": fam["family_max"],
            "family_spread": fam["family_spread"],
            "family_n": fam["family_n"],
            "family_compact": fam["family_compact"],
            "delta_nws": delta_nws,
            "delta_om": delta_om,
            "delta_family": delta_family,
            "avg_delta": avg_delta,
            "status": classify(delta_nws, delta_om, delta_family),
            "nws_time": nws_time,
            "om_time": om_time,
            "error": "",
        }

    except Exception as e:
        return {
            "icao": icao,
            "city": info.get("name", ""),
            "local_time": "",
            "latest_obs": math.nan,
            "nws_now": math.nan,
            "om_now": math.nan,
            "family_mean": math.nan,
            "family_min": math.nan,
            "family_max": math.nan,
            "family_spread": math.nan,
            "family_n": 0,
            "family_compact": "",
            "delta_nws": math.nan,
            "delta_om": math.nan,
            "delta_family": math.nan,
            "avg_delta": math.nan,
            "status": "ERR",
            "nws_time": "",
            "om_time": "",
            "error": str(e),
        }


def main():
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"run_at={run_at}")
    print()

    rows = []

    for icao, info in STATIONS.items():
        row = scan_station(icao, info)
        rows.append(row)
        time.sleep(REQUEST_SLEEP)

    # Sort hottest first, errors last.
    rows.sort(
        key=lambda r: (
            r["status"] == "ERR",
            -999
            if isinstance(r["avg_delta"], float) and math.isnan(r["avg_delta"])
            else -r["avg_delta"],
        )
    )

    print(
        f"{'ICAO':<5} {'City':<16} {'ObsTime':<16} "
        f"{'Obs':>6} {'NWS':>6} {'OM':>6} "
        f"{'FamMn':>6} {'FamLo':>6} {'FamHi':>6} {'FamSpr':>7} "
        f"{'dNWS':>7} {'dOM':>7} {'dFam':>7} {'AvgD':>7} "
        f"{'Status':<11} Error"
    )
    print("-" * 150)

    for r in rows:
        print(
            f"{r['icao']:<5} {r['city']:<16} {r['local_time']:<16} "
            f"{fmt(r['latest_obs']):>6} {fmt(r['nws_now']):>6} {fmt(r['om_now']):>6} "
            f"{fmt(r['family_mean']):>6} {fmt(r['family_min']):>6} "
            f"{fmt(r['family_max']):>6} {fmt(r['family_spread']):>7} "
            f"{fmt(r['delta_nws'], 2):>7} {fmt(r['delta_om'], 2):>7} "
            f"{fmt(r['delta_family'], 2):>7} {fmt(r['avg_delta'], 2):>7} "
            f"{r['status']:<11} {r['error']}"
        )

    print()
    print("Model-family abbreviations:")
    print("  GFS = gfs_seamless")
    print("  ICON = icon_global")
    print("  GEM = gem_global")
    print("  ECMWF = ecmwf_ifs025")
    print("  UKMO = ukmo_global_deterministic_10km")


if __name__ == "__main__":
    main()