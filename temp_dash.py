from __future__ import annotations

import math
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

import tier_ev_new as tev
from tracker import STATIONS


# =============================================================================
# STREAMLIT SETUP
# =============================================================================

st.set_page_config(
    page_title="Weather Market EV Dashboard",
    layout="wide",
)


# =============================================================================
# GENERAL HELPERS
# =============================================================================

def safe_float(x):
    try:
        if x is None:
            return math.nan
        return float(x)
    except Exception:
        return math.nan


def fmt_metric_temp(x):
    if x is None:
        return "NA"
    if isinstance(x, float) and math.isnan(x):
        return "NA"
    try:
        return f"{float(x):.1f}°F"
    except Exception:
        return "NA"


def fmt_metric_delta(x):
    if x is None:
        return "NA"
    if isinstance(x, float) and math.isnan(x):
        return "NA"
    try:
        return f"{float(x):+.1f}°F"
    except Exception:
        return "NA"


def build_market_row(
    *,
    tier: str,
    icao: str,
    info: dict,
    market_type: str,
    bucket: dict,
    om_daily,
    om_hourly,
    nwsh_val,
    nwsp_val,
    ens: dict,
    ev_y,
    ev_n,
    src_sprd,
    src_vote,
    family: dict,
) -> dict:
    return {
        "tier": tier,
        "icao": icao,
        "city": info["name"],
        "date": bucket["target_date"],
        "type": market_type,
        "bucket": bucket["label"],
        "raw_low": bucket["raw_low"],
        "raw_high": bucket["raw_high"],
        "yes": bucket["yes"],
        "no": bucket["no"],
        "ev_no": ev_n,
        "ev_yes": ev_y,
        "ens_yes": ens["p_yes"],
        "ens_no": ens["p_no"],
        "ens_mean": ens["mean"],
        "ens_sd": ens["sd"],
        "ens_p10": ens["p10"],
        "ens_p50": ens["p50"],
        "ens_p90": ens["p90"],
        "n_members": ens["n_members"],
        "n_inside": ens["n_inside"],
        "n_below": ens["n_below"],
        "n_above": ens["n_above"],
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
        "ticker": bucket["ticker"],
        "event_key": bucket.get("event_key", ""),
    }


# =============================================================================
# MARKET SCANNER LOGIC
# =============================================================================

def scan_station_all_market_rows(
    icao: str,
    info: dict,
    *,
    min_yes_price: float,
    max_yes_price: float,
    min_ev_no: float,
    apply_price_filter: bool,
    apply_ev_filter: bool,
) -> list[dict]:
    rows = []
    tz = info["tz"]
    target_dates = tev.target_dates_for_station(tz)

    errors = []

    om_det = None
    try:
        om_det = tev.fetch_openmeteo_deterministic(info["lat"], info["lon"], tz)
        time.sleep(tev.REQUEST_SLEEP)
    except Exception as e:
        errors.append(f"OM deterministic failed: {e}")

    om_ens = None
    try:
        om_ens = tev.fetch_openmeteo_ensemble(info["lat"], info["lon"], tz)
        time.sleep(tev.REQUEST_SLEEP)
    except Exception as e:
        errors.append(f"OM ensemble failed: {e}")

    om_family = None
    try:
        om_family = tev.fetch_openmeteo_model_families(info["lat"], info["lon"], tz)
        time.sleep(tev.REQUEST_SLEEP)
    except Exception as e:
        errors.append(f"OM model families failed: {e}")

    nws_hourly = {}
    try:
        nws_hourly = tev.fetch_nws_hourly_extremes(info["lat"], info["lon"], tz)
        time.sleep(tev.REQUEST_SLEEP)
    except Exception as e:
        errors.append(f"NWS hourly failed: {e}")

    nws_period = {}
    try:
        nws_period = tev.fetch_nws_period_extremes(info["lat"], info["lon"], tz)
        time.sleep(tev.REQUEST_SLEEP)
    except Exception as e:
        errors.append(f"NWS period failed: {e}")

    for market_type in ("high", "low"):
        buckets = tev.fetch_station_buckets(info, target_dates, market_type)

        for b in buckets:
            yes = b["yes"]
            no = b["no"]

            if apply_price_filter and not (min_yes_price <= yes <= max_yes_price):
                continue

            d = b["target_date"]

            om_daily = tev.om_daily_value(om_det, d, market_type)
            om_hourly, _ = tev.om_hourly_extreme(om_det, d, market_type)

            nwsh_val = None
            if d in nws_hourly and market_type in nws_hourly[d]:
                nwsh_val, _ = nws_hourly[d][market_type]

            nwsp_val = None
            if d in nws_period:
                nwsp_val = nws_period[d].get(market_type)

            ens_vals = tev.ensemble_member_extremes(om_ens, d, market_type)
            ens = tev.ensemble_bucket_summary(
                ens_vals,
                b["bucket_low"],
                b["bucket_high"],
            )

            if ens["n_members"] == 0:
                p_yes = math.nan
                p_no = math.nan
                ev_y = math.nan
                ev_n = math.nan
            else:
                p_yes = ens["p_yes"]
                p_no = ens["p_no"]
                ev_y = tev.ev_yes_cents(p_yes, yes)
                ev_n = tev.ev_no_cents(p_no, no)

            if apply_ev_filter and not math.isnan(ev_n) and ev_n < min_ev_no:
                continue

            family_values = tev.model_family_values(om_family, d, market_type)
            family = tev.model_family_bucket_summary(
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

            src_sprd = tev.source_spread(list(point_values.values()))

            src_vote = tev.source_votes_for_bucket(
                point_values,
                b["bucket_low"],
                b["bucket_high"],
            )

            if ens["n_members"] == 0:
                tier = "NO_ENSEMBLE"
            else:
                tier_result = tev.classify_tier(
                    src_sprd,
                    ens["sd"],
                    src_vote,
                    family,
                )
                tier = tier_result if tier_result else "NONE"

            row = build_market_row(
                tier=tier,
                icao=icao,
                info=info,
                market_type=market_type,
                bucket=b,
                om_daily=om_daily,
                om_hourly=om_hourly,
                nwsh_val=nwsh_val,
                nwsp_val=nwsp_val,
                ens=ens,
                ev_y=ev_y,
                ev_n=ev_n,
                src_sprd=src_sprd,
                src_vote=src_vote,
                family=family,
            )

            row["source_errors"] = "; ".join(errors)
            rows.append(row)

    return rows


@st.cache_data(ttl=300, show_spinner=False)
def scan_all_market_rows(
    min_yes_price: float,
    max_yes_price: float,
    min_ev_no: float,
    apply_price_filter: bool,
    apply_ev_filter: bool,
) -> pd.DataFrame:
    all_rows = []

    for icao, info in STATIONS.items():
        station_rows = scan_station_all_market_rows(
            icao,
            info,
            min_yes_price=min_yes_price,
            max_yes_price=max_yes_price,
            min_ev_no=min_ev_no,
            apply_price_filter=apply_price_filter,
            apply_ev_filter=apply_ev_filter,
        )
        all_rows.extend(station_rows)

    df = pd.DataFrame(all_rows)

    if df.empty:
        return df

    df["ens_yes_pct"] = df["ens_yes"] * 100
    df["ens_no_pct"] = df["ens_no"] * 100

    df["raw_interval"] = df.apply(
        lambda r: tev.fmt_interval(r["raw_low"], r["raw_high"]),
        axis=1,
    )

    tier_order = {
        "TIER1": 0,
        "TIER2": 1,
        "NONE": 2,
        "NO_ENSEMBLE": 3,
    }

    df["_tier_order"] = df["tier"].map(tier_order).fillna(9)

    df = df.sort_values(
        by=["_tier_order", "date", "ev_no", "icao", "type", "bucket"],
        ascending=[True, True, False, True, True, True],
    )

    return df


# =============================================================================
# STATION DIG HELPERS
# =============================================================================

def parse_tgroup_f(raw: str | None) -> float | None:
    """
    Parse precise METAR T group:
      T02500117 -> temp 25.0C
      T02400120 -> temp 24.0C
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


def fetch_recent_hf_asos(icao: str, info: dict, hours: int = 12) -> str:
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

    r = tev._get_with_retry(url, params=params, timeout=30, max_retries=2)
    return r.text


def parse_hf_rows(txt: str, info: dict) -> pd.DataFrame:
    rows = []
    tz = ZoneInfo(info["tz"])

    for line in txt.splitlines():
        if not line or line.startswith("station"):
            continue

        parts = line.split(",", 3)
        if len(parts) < 4:
            continue

        station, valid, tmpf, raw = parts

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

        if val is not None and dt is not None:
            rows.append(
                {
                    "valid": valid,
                    "dt": dt,
                    "temp_f": val,
                    "tmpf": tmpf,
                    "raw": raw,
                }
            )

    return pd.DataFrame(rows)


def obs_extreme(obs_df: pd.DataFrame, market_type: str) -> dict | None:
    if obs_df.empty:
        return None

    if market_type == "high":
        idx = obs_df["temp_f"].idxmax()
    else:
        idx = obs_df["temp_f"].idxmin()

    return obs_df.loc[idx].to_dict()


def latest_obs(obs_df: pd.DataFrame) -> dict | None:
    if obs_df.empty:
        return None

    idx = obs_df["dt"].idxmax()
    return obs_df.loc[idx].to_dict()


def fetch_gfs_hourly_curve(info: dict) -> pd.DataFrame:
    """
    Pull GFS hourly temperature from Open-Meteo model-family endpoint.
    """
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": info["lat"],
        "longitude": info["lon"],
        "timezone": info["tz"],
        "temperature_unit": "fahrenheit",
        "forecast_days": 3,
        "hourly": "temperature_2m",
        "models": "gfs_seamless",
    }

    r = tev._get_with_retry(url, params=params, timeout=30, max_retries=3)
    payload = r.json()

    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])

    key = "temperature_2m_gfs_seamless"

    if key not in hourly:
        possible = [
            k
            for k in hourly.keys()
            if k.startswith("temperature_2m") and "gfs" in k
        ]
        key = possible[0] if possible else ""

    vals = hourly.get(key, [])

    out = []
    tz = ZoneInfo(info["tz"])

    for t, v in zip(times, vals):
        if v is None:
            continue

        try:
            dt = datetime.fromisoformat(t).replace(tzinfo=tz)
        except Exception:
            continue

        out.append(
            {
                "dt": dt,
                "gfs_hourly": float(v),
            }
        )

    return pd.DataFrame(out)


def fetch_nws_full_hourly_curve(info: dict) -> pd.DataFrame:
    """
    Pull full NWS hourly curve for plotting.
    """
    props = tev.fetch_nws_urls(info["lat"], info["lon"])
    hourly_url = props["forecastHourly"]

    r = tev._get_with_retry(
        hourly_url,
        headers={"User-Agent": "weather-dashboard/1.0"},
        timeout=30,
        max_retries=2,
    )

    rows = []
    tz = ZoneInfo(info["tz"])

    for p in r.json()["properties"]["periods"]:
        start = p.get("startTime")
        temp = p.get("temperature")

        if start is None or temp is None:
            continue

        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(tz)
        except Exception:
            continue

        rows.append(
            {
                "dt": dt,
                "nws_hourly": float(temp),
            }
        )

    return pd.DataFrame(rows)


def om_curve_to_df(om_det: dict | None, info: dict) -> pd.DataFrame:
    if om_det is None:
        return pd.DataFrame()

    hourly = om_det.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])

    rows = []
    tz = ZoneInfo(info["tz"])

    for t, v in zip(times, temps):
        if v is None:
            continue

        try:
            dt = datetime.fromisoformat(t).replace(tzinfo=tz)
        except Exception:
            continue

        rows.append(
            {
                "dt": dt,
                "om_hourly": float(v),
            }
        )

    return pd.DataFrame(rows)


def nearest_curve_value(df: pd.DataFrame, time_col: str, value_col: str, obs_dt):
    if df.empty or obs_dt is None:
        return math.nan

    tmp = df.copy()
    tmp["absdiff"] = (tmp[time_col] - obs_dt).abs()

    return float(tmp.sort_values("absdiff").iloc[0][value_col])


def make_station_plot(obs_df, nws_df, om_df, gfs_df, title: str):
    fig = go.Figure()

    if not obs_df.empty:
        fig.add_trace(
            go.Scatter(
                x=obs_df["dt"],
                y=obs_df["temp_f"],
                mode="markers+lines",
                name="ASOS/METAR obs",
            )
        )

    if not nws_df.empty:
        fig.add_trace(
            go.Scatter(
                x=nws_df["dt"],
                y=nws_df["nws_hourly"],
                mode="lines",
                name="NWS hourly",
            )
        )

    if not om_df.empty:
        fig.add_trace(
            go.Scatter(
                x=om_df["dt"],
                y=om_df["om_hourly"],
                mode="lines",
                name="OM hourly",
            )
        )

    if not gfs_df.empty:
        fig.add_trace(
            go.Scatter(
                x=gfs_df["dt"],
                y=gfs_df["gfs_hourly"],
                mode="lines",
                name="GFS hourly",
            )
        )

    fig.update_layout(
        title=title,
        xaxis_title="Local time",
        yaxis_title="Temperature (°F)",
        hovermode="x unified",
        height=520,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
        ),
        margin=dict(l=40, r=20, t=80, b=40),
    )

    return fig


@st.cache_data(ttl=180, show_spinner=False)
def build_station_dig_report(
    icao: str,
    market_type: str,
    day_choice: str,
) -> dict:
    info = STATIONS[icao]
    tz = info["tz"]
    local_today = datetime.now(ZoneInfo(tz)).date()

    if day_choice == "today":
        target_date = local_today.isoformat()
    else:
        target_date = (local_today + timedelta(days=1)).isoformat()

    errors = []

    om_det = None
    try:
        om_det = tev.fetch_openmeteo_deterministic(info["lat"], info["lon"], tz)
    except Exception as e:
        errors.append(f"OM deterministic failed: {e}")

    om_ens = None
    try:
        om_ens = tev.fetch_openmeteo_ensemble(info["lat"], info["lon"], tz)
    except Exception as e:
        errors.append(f"OM ensemble failed: {e}")

    om_family = None
    try:
        om_family = tev.fetch_openmeteo_model_families(info["lat"], info["lon"], tz)
    except Exception as e:
        errors.append(f"OM model families failed: {e}")

    nws_hourly = {}
    try:
        nws_hourly = tev.fetch_nws_hourly_extremes(info["lat"], info["lon"], tz)
    except Exception as e:
        errors.append(f"NWS hourly extremes failed: {e}")

    nws_period = {}
    try:
        nws_period = tev.fetch_nws_period_extremes(info["lat"], info["lon"], tz)
    except Exception as e:
        errors.append(f"NWS period extremes failed: {e}")

    obs_df = pd.DataFrame()

    if day_choice == "today":
        try:
            hf_text = fetch_recent_hf_asos(icao, info, hours=12)
            obs_df = parse_hf_rows(hf_text, info)
        except Exception as e:
            errors.append(f"ASOS/METAR obs failed: {e}")

    nws_df = pd.DataFrame()
    try:
        nws_df = fetch_nws_full_hourly_curve(info)
    except Exception as e:
        errors.append(f"NWS hourly curve failed: {e}")

    om_df = om_curve_to_df(om_det, info)

    gfs_df = pd.DataFrame()
    try:
        gfs_df = fetch_gfs_hourly_curve(info)
    except Exception as e:
        errors.append(f"GFS hourly failed: {e}")

    om_daily = tev.om_daily_value(om_det, target_date, market_type)
    om_hourly, om_hourly_time = tev.om_hourly_extreme(
        om_det,
        target_date,
        market_type,
    )

    nwsh_val = None
    nwsh_time = None

    if target_date in nws_hourly and market_type in nws_hourly[target_date]:
        nwsh_val, nwsh_time = nws_hourly[target_date][market_type]

    nwsp_val = None

    if target_date in nws_period:
        nwsp_val = nws_period[target_date].get(market_type)

    ens_vals = tev.ensemble_member_extremes(om_ens, target_date, market_type)

    ens_mean = statistics.mean(ens_vals) if ens_vals else math.nan
    ens_sd = statistics.pstdev(ens_vals) if len(ens_vals) > 1 else math.nan
    ens_p10 = tev.percentile(ens_vals, 0.10)
    ens_p90 = tev.percentile(ens_vals, 0.90)

    family_values = tev.model_family_values(om_family, target_date, market_type)

    if family_values:
        family_vals = list(family_values.values())
        family_summary = {
            "family_mean": statistics.mean(family_vals),
            "family_min": min(family_vals),
            "family_max": max(family_vals),
            "family_spread": max(family_vals) - min(family_vals),
            "family_models": " ".join(
                f"{tev.MODEL_SHORT_NAMES.get(k, k)}:{v:.1f}"
                for k, v in family_values.items()
            ),
        }
    else:
        family_summary = {
            "family_mean": math.nan,
            "family_min": math.nan,
            "family_max": math.nan,
            "family_spread": math.nan,
            "family_models": "",
        }

    obs_now = latest_obs(obs_df) if not obs_df.empty else None
    obs_ext = obs_extreme(obs_df, market_type) if not obs_df.empty else None

    latest_temp = obs_now["temp_f"] if obs_now is not None else math.nan
    obs_dt = obs_now["dt"] if obs_now is not None else None

    nws_now = nearest_curve_value(nws_df, "dt", "nws_hourly", obs_dt)
    om_now = nearest_curve_value(om_df, "dt", "om_hourly", obs_dt)
    gfs_now = nearest_curve_value(gfs_df, "dt", "gfs_hourly", obs_dt)

    delta_nws = latest_temp - nws_now if not math.isnan(nws_now) else math.nan
    delta_om = latest_temp - om_now if not math.isnan(om_now) else math.nan
    delta_gfs = latest_temp - gfs_now if not math.isnan(gfs_now) else math.nan

    buckets = tev.fetch_station_buckets(info, [target_date], market_type)
    bucket_rows = []

    for b in buckets:
        ens = tev.ensemble_bucket_summary(
            ens_vals,
            b["bucket_low"],
            b["bucket_high"],
        )

        p_yes = ens["p_yes"]
        p_no = ens["p_no"]

        ev_n = tev.ev_no_cents(p_no, b["no"]) if not math.isnan(p_no) else math.nan
        ev_y = tev.ev_yes_cents(p_yes, b["yes"]) if not math.isnan(p_yes) else math.nan

        fam_bucket = tev.model_family_bucket_summary(
            family_values,
            b["bucket_low"],
            b["bucket_high"],
        )

        obs_dist = math.nan
        read = "no live obs"
        obs_extreme_value = math.nan

        if obs_ext is not None:
            obs_extreme_value = obs_ext["temp_f"]
            obs_dist = tev.distance_to_cli_bucket(
                obs_extreme_value,
                b["bucket_low"],
                b["bucket_high"],
            )

            raw_low, raw_high = tev.raw_interval_for_cli_bucket(
                b["bucket_low"],
                b["bucket_high"],
            )

            if raw_low <= obs_extreme_value < raw_high:
                read = "OBS currently rounds into bucket"
            elif market_type == "high" and obs_extreme_value >= raw_high:
                read = "bucket already passed above"
            elif market_type == "low" and obs_extreme_value < raw_low:
                read = "bucket already passed below"
            else:
                read = "still live"

        bucket_rows.append(
            {
                "bucket": b["label"],
                "raw_interval": tev.fmt_interval(b["raw_low"], b["raw_high"]),
                "yes": b["yes"],
                "no": b["no"],
                "ens_yes_pct": p_yes * 100 if not math.isnan(p_yes) else math.nan,
                "ev_no": ev_n,
                "ev_yes": ev_y,
                "family_inside": fam_bucket["family_inside"],
                "family_inside_names": fam_bucket["family_inside_names"],
                "family_mean": fam_bucket["family_mean"],
                "family_spread": fam_bucket["family_spread"],
                "obs_dist": obs_dist,
                "obs_extreme": obs_extreme_value,
                "read": read,
                "ticker": b["ticker"],
            }
        )

    summary = {
        "icao": icao,
        "city": info["name"],
        "target_date": target_date,
        "market_type": market_type,
        "latest_obs": latest_temp,
        "latest_obs_time": obs_now["valid"] if obs_now is not None else "",
        "obs_extreme": obs_ext["temp_f"] if obs_ext is not None else math.nan,
        "obs_extreme_time": obs_ext["valid"] if obs_ext is not None else "",
        "nws_now": nws_now,
        "om_now": om_now,
        "gfs_now": gfs_now,
        "delta_nws": delta_nws,
        "delta_om": delta_om,
        "delta_gfs": delta_gfs,
    }

    forecast_extremes = {
        "nws_hourly": nwsh_val,
        "nws_hourly_time": nwsh_time,
        "nws_period": nwsp_val,
        "om_daily": om_daily,
        "om_hourly": om_hourly,
        "om_hourly_time": om_hourly_time,
        "ensemble_mean": ens_mean,
        "ensemble_sd": ens_sd,
        "ensemble_p10": ens_p10,
        "ensemble_p90": ens_p90,
        "family_mean": family_summary["family_mean"],
        "family_min": family_summary["family_min"],
        "family_max": family_summary["family_max"],
        "family_spread": family_summary["family_spread"],
        "family_models": family_summary["family_models"],
    }

    return {
        "summary": summary,
        "forecast_extremes": forecast_extremes,
        "buckets": bucket_rows,
        "obs_df": obs_df,
        "nws_df": nws_df,
        "om_df": om_df,
        "gfs_df": gfs_df,
        "errors": errors,
    }


# =============================================================================
# SIDEBAR
# =============================================================================

st.sidebar.title("Controls")

st.sidebar.subheader("Market scanner filters")

apply_price_filter = st.sidebar.checkbox(
    "Apply YES price filter",
    value=True,
)

min_yes_price, max_yes_price = st.sidebar.slider(
    "YES price range",
    min_value=0.0,
    max_value=100.0,
    value=(10.0, 55.0),
    step=1.0,
)

apply_ev_filter = st.sidebar.checkbox(
    "Apply minimum EV_N filter",
    value=False,
)

min_ev_no = st.sidebar.slider(
    "Minimum EV_N",
    min_value=-100.0,
    max_value=100.0,
    value=5.0,
    step=1.0,
)

st.sidebar.subheader("Tier rules")

st.sidebar.markdown(
    """
**TIER1** requires:

- `source_spread <= 2.0°F`
- `ensemble_sd <= 2.5°F`
- `n_sources_inside == 0`
- model-family gate passes:
  - no model family inside bucket
  - `family_spread <= 3.0°F`

**TIER2** requires:

- `source_spread <= 4.0°F`
- `ensemble_sd <= 4.0°F`
- `n_sources_inside <= 1`
- model-family gate passes:
  - at most one model family inside bucket
  - `family_spread <= 5.0°F`

**NONE** means the row did not meet Tier 1 or Tier 2.

**NO_ENSEMBLE** means ensemble data was unavailable, so EV/tier is not reliable.

Current scanner checks today + tomorrow, high + low, for every station.
"""
)

st.sidebar.subheader("Column notes")

st.sidebar.markdown(
    """
- `ens_yes_pct`: Open-Meteo ensemble bucket probability.
- `ev_no`: `100 * ens_no - NO price`.
- `source_spread`: NWS hourly, NWS period, OM daily, OM hourly, ensemble mean.
- `family_*`: GFS / ICON / GEM / ECMWF / UKMO daily high/low values.
- Kalshi event dates are parsed from tickers.
"""
)


# =============================================================================
# MAIN APP
# =============================================================================

st.title("Weather Market EV Dashboard")

tab_scan, tab_dig = st.tabs(
    [
        "Market Scanner",
        "Station Dig",
    ]
)


# =============================================================================
# TAB 1: MARKET SCANNER
# =============================================================================

with tab_scan:
    st.header("Market Scanner")
    st.caption(
        "All station/bucket rows from tier_ev_new logic, with tier as a sortable column."
    )

    col_a, col_b, col_c = st.columns([1, 1, 2])

    with col_a:
        run_scan = st.button(
            "Run / refresh scan",
            type="primary",
            key="run_market_scan",
        )

    with col_b:
        clear_cache = st.button(
            "Clear cache",
            key="clear_market_cache",
        )

    with col_c:
        st.write(
            f"Page time: {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC"
        )

    if clear_cache:
        st.cache_data.clear()
        st.success("Cache cleared. Click Run / refresh scan.")

    if run_scan:
        st.session_state["market_scan_has_run"] = True

    if not st.session_state.get("market_scan_has_run"):
        st.info("Click **Run / refresh scan** to pull all stations and markets.")
    else:
        with st.spinner("Scanning all stations..."):
            df = scan_all_market_rows(
                min_yes_price=min_yes_price,
                max_yes_price=max_yes_price,
                min_ev_no=min_ev_no,
                apply_price_filter=apply_price_filter,
                apply_ev_filter=apply_ev_filter,
            )

        if df.empty:
            st.warning("No rows returned.")
        else:
            st.subheader("Results")

            filter_col1, filter_col2, filter_col3, filter_col4 = st.columns(4)

            with filter_col1:
                tier_filter = st.multiselect(
                    "Tier",
                    options=sorted(df["tier"].dropna().unique().tolist()),
                    default=sorted(df["tier"].dropna().unique().tolist()),
                )

            with filter_col2:
                type_filter = st.multiselect(
                    "Market type",
                    options=sorted(df["type"].dropna().unique().tolist()),
                    default=sorted(df["type"].dropna().unique().tolist()),
                )

            with filter_col3:
                date_filter = st.multiselect(
                    "Date",
                    options=sorted(df["date"].dropna().unique().tolist()),
                    default=sorted(df["date"].dropna().unique().tolist()),
                )

            with filter_col4:
                station_filter = st.multiselect(
                    "Station",
                    options=sorted(df["icao"].dropna().unique().tolist()),
                    default=sorted(df["icao"].dropna().unique().tolist()),
                )

            view = df[
                df["tier"].isin(tier_filter)
                & df["type"].isin(type_filter)
                & df["date"].isin(date_filter)
                & df["icao"].isin(station_filter)
            ].copy()

            preferred_cols = [
                "tier",
                "icao",
                "city",
                "date",
                "type",
                "bucket",
                "raw_interval",
                "yes",
                "no",
                "ev_no",
                "ev_yes",
                "ens_yes_pct",
                "ens_mean",
                "ens_sd",
                "ens_p10",
                "ens_p90",
                "nws_hourly",
                "nws_period",
                "om_daily",
                "om_hourly",
                "source_spread",
                "n_sources_inside",
                "sources_inside",
                "family_min",
                "family_max",
                "family_mean",
                "family_spread",
                "family_inside",
                "family_inside_names",
                "family_compact",
                "n_members",
                "n_inside",
                "ticker",
                "event_key",
                "source_errors",
            ]

            cols = [c for c in preferred_cols if c in view.columns]

            st.dataframe(
                view[cols],
                use_container_width=True,
                hide_index=True,
            )

            st.download_button(
                "Download filtered CSV",
                data=view[cols].to_csv(index=False),
                file_name="weather_market_ev_dashboard.csv",
                mime="text/csv",
            )

            st.subheader("Tier counts")

            tier_counts = (
                view.groupby(["tier"], dropna=False)
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )

            st.dataframe(
                tier_counts,
                use_container_width=True,
                hide_index=True,
            )


# =============================================================================
# TAB 2: STATION DIG
# =============================================================================

with tab_dig:
    st.header("Station Dig")
    st.caption(
        "Station-specific live obs, forecasts, Kalshi buckets, and forecast-path chart."
    )

    station_options = {
        f"{icao} — {info['name']}": icao
        for icao, info in STATIONS.items()
    }

    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])

    with c1:
        station_label = st.selectbox(
            "Station",
            list(station_options.keys()),
            key="dig_station",
        )
        dig_icao = station_options[station_label]

    with c2:
        dig_market_type = st.radio(
            "Market type",
            ["high", "low"],
            horizontal=True,
            key="dig_market_type",
        )

    with c3:
        dig_day_choice = st.radio(
            "Target day",
            ["today", "tomorrow"],
            horizontal=True,
            key="dig_day_choice",
        )

    with c4:
        st.write("")
        st.write("")
        run_dig = st.button(
            "Run Station Dig",
            type="primary",
            key="run_station_dig",
        )

    if run_dig:
        with st.spinner(f"Pulling station dig for {dig_icao}..."):
            report = build_station_dig_report(
                dig_icao,
                dig_market_type,
                dig_day_choice,
            )

        st.session_state["dig_report"] = report

    report = st.session_state.get("dig_report")

    if report is None:
        st.info("Choose a station and click **Run Station Dig**.")
    else:
        summary = report["summary"]
        fx = report["forecast_extremes"]

        if report["errors"]:
            with st.expander("Source errors / degraded mode notes", expanded=False):
                for err in report["errors"]:
                    st.write(f"- {err}")

        st.subheader(
            f"{summary['icao']} {summary['city']} — "
            f"{summary['target_date']} {summary['market_type']}"
        )

        m1, m2, m3, m4, m5, m6 = st.columns(6)

        m1.metric(
            "Latest obs",
            fmt_metric_temp(summary["latest_obs"]),
        )

        m2.metric(
            "Obs extreme",
            fmt_metric_temp(summary["obs_extreme"]),
        )

        m3.metric(
            "Δ vs NWS",
            fmt_metric_delta(summary["delta_nws"]),
        )

        m4.metric(
            "Δ vs OM",
            fmt_metric_delta(summary["delta_om"]),
        )

        m5.metric(
            "Δ vs GFS",
            fmt_metric_delta(summary["delta_gfs"]),
        )

        m6.metric(
            "NWS extreme",
            fmt_metric_temp(fx["nws_hourly"]),
        )

        st.caption(
            f"Latest obs time: {summary['latest_obs_time']} | "
            f"Observed {summary['market_type']} so far time: "
            f"{summary['obs_extreme_time']}"
        )

        fig = make_station_plot(
            report["obs_df"],
            report["nws_df"],
            report["om_df"],
            report["gfs_df"],
            title=(
                f"{summary['icao']} temperature path — obs vs NWS / OM / GFS"
            ),
        )

        st.plotly_chart(fig, use_container_width=True)

        st.subheader("Forecast extremes")

        fx_df = pd.DataFrame(
            [
                {
                    "source": "NWS hourly",
                    "value": fx["nws_hourly"],
                    "time_or_detail": fx["nws_hourly_time"],
                },
                {
                    "source": "NWS period",
                    "value": fx["nws_period"],
                    "time_or_detail": "",
                },
                {
                    "source": "OM daily",
                    "value": fx["om_daily"],
                    "time_or_detail": "",
                },
                {
                    "source": "OM hourly",
                    "value": fx["om_hourly"],
                    "time_or_detail": fx["om_hourly_time"],
                },
                {
                    "source": "OM ensemble mean",
                    "value": fx["ensemble_mean"],
                    "time_or_detail": "",
                },
                {
                    "source": "OM ensemble sd",
                    "value": fx["ensemble_sd"],
                    "time_or_detail": "",
                },
                {
                    "source": "OM ensemble p10",
                    "value": fx["ensemble_p10"],
                    "time_or_detail": "",
                },
                {
                    "source": "OM ensemble p90",
                    "value": fx["ensemble_p90"],
                    "time_or_detail": "",
                },
                {
                    "source": "Family mean",
                    "value": fx["family_mean"],
                    "time_or_detail": fx["family_models"],
                },
                {
                    "source": "Family min",
                    "value": fx["family_min"],
                    "time_or_detail": "",
                },
                {
                    "source": "Family max",
                    "value": fx["family_max"],
                    "time_or_detail": "",
                },
                {
                    "source": "Family spread",
                    "value": fx["family_spread"],
                    "time_or_detail": "",
                },
            ]
        )

        st.dataframe(
            fx_df,
            use_container_width=True,
            hide_index=True,
        )

        st.subheader("Kalshi buckets")

        bucket_df = pd.DataFrame(report["buckets"])

        if bucket_df.empty:
            st.warning("No Kalshi buckets found.")
        else:
            st.dataframe(
                bucket_df,
                use_container_width=True,
                hide_index=True,
            )

        st.subheader("Recent ASOS / METAR observations")

        obs_df = report["obs_df"].copy()

        if obs_df.empty:
            st.warning("No recent observations found.")
        else:
            obs_display = obs_df.copy()
            obs_display["dt"] = obs_display["dt"].astype(str)

            st.dataframe(
                obs_display[["valid", "temp_f", "tmpf", "raw"]],
                use_container_width=True,
                hide_index=True,
            )