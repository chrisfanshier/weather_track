from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import tier_ev_new as tev
from tracker import STATIONS


st.set_page_config(
    page_title="Weather Market EV Dashboard",
    layout="wide",
)


# =============================
# HELPERS
# =============================

def safe_float(x):
    try:
        if x is None:
            return math.nan
        return float(x)
    except Exception:
        return math.nan


def build_row(
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


def scan_station_all_rows(
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

    # This app is primarily ensemble-EV based. If ensemble is missing, rows
    # cannot get useful EV/tier values, but we still avoid crashing.
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

            row = build_row(
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
def scan_all_all_rows(
    min_yes_price: float,
    max_yes_price: float,
    min_ev_no: float,
    apply_price_filter: bool,
    apply_ev_filter: bool,
) -> pd.DataFrame:
    all_rows = []

    for icao, info in STATIONS.items():
        station_rows = scan_station_all_rows(
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

    # Helpful display aliases.
    df["ens_yes_pct"] = df["ens_yes"] * 100
    df["ens_no_pct"] = df["ens_no"] * 100
    df["raw_interval"] = df.apply(
        lambda r: tev.fmt_interval(r["raw_low"], r["raw_high"]),
        axis=1,
    )

    # Stable sort, but Streamlit table can be sorted interactively by clicking headers.
    tier_order = {"TIER1": 0, "TIER2": 1, "NONE": 2, "NO_ENSEMBLE": 3}
    df["_tier_order"] = df["tier"].map(tier_order).fillna(9)

    df = df.sort_values(
        by=["_tier_order", "date", "ev_no", "icao", "type", "bucket"],
        ascending=[True, True, False, True, True, True],
    )

    return df


# =============================
# SIDEBAR
# =============================

st.sidebar.title("Controls")

st.sidebar.subheader("Filters")

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

Current script scans today + tomorrow, high + low, for every station.
"""
)

st.sidebar.subheader("Notes")

st.sidebar.markdown(
    """
- `ens_yes_pct` is the Open-Meteo ensemble bucket probability.
- `ev_no` is `100 * ens_no - NO price`.
- `source_spread` uses NWS hourly, NWS period, OM daily, OM hourly, and ensemble mean.
- `family_*` columns are GFS / ICON / GEM / ECMWF / UKMO daily high/low values.
- Kalshi dates are parsed from tickers to avoid mixing today/tomorrow markets.
"""
)


# =============================
# MAIN APP
# =============================

st.title("Weather Market EV Dashboard")
st.caption("All station/bucket rows from tier_ev_new logic, with tier as a sortable column.")

col_a, col_b, col_c = st.columns([1, 1, 2])

with col_a:
    run_scan = st.button("Run / refresh scan", type="primary")

with col_b:
    clear_cache = st.button("Clear cache")

with col_c:
    st.write(f"Last page load: {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC")

if clear_cache:
    st.cache_data.clear()
    st.success("Cache cleared. Click Run / refresh scan.")

if run_scan:
    st.session_state["has_run"] = True

if not st.session_state.get("has_run"):
    st.info("Click **Run / refresh scan** to pull all stations and markets.")
    st.stop()

with st.spinner("Scanning all stations..."):
    df = scan_all_all_rows(
        min_yes_price=min_yes_price,
        max_yes_price=max_yes_price,
        min_ev_no=min_ev_no,
        apply_price_filter=apply_price_filter,
        apply_ev_filter=apply_ev_filter,
    )

if df.empty:
    st.warning("No rows returned.")
    st.stop()

# Optional quick filters after scan.
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

st.dataframe(tier_counts, use_container_width=True, hide_index=True)