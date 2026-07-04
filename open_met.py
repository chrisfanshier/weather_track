import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

def get_openmeteo_forecasts():
    # Production registry: Coordinates + Explicit Time Zones to completely bypass 'timezone=auto' bottlenecks
    STATIONS = {
        "Atlanta (KATL)": {"lat": 33.6407, "lon": -84.4277, "tz": "America/New_York"},
        "Austin (KAUS)": {"lat": 30.1945, "lon": -97.6699, "tz": "America/Chicago"},
        "Boston (KBOS)": {"lat": 42.3656, "lon": -71.0096, "tz": "America/New_York"},
        "Chicago (KMDW)": {"lat": 41.7860, "lon": -87.7524, "tz": "America/Chicago"},
        "Dallas (KDFW)": {"lat": 32.8998, "lon": -97.0403, "tz": "America/Chicago"},
        "Denver (KDEN)": {"lat": 39.8561, "lon": -104.6737, "tz": "America/Denver"},
        "Houston (KIAH)": {"lat": 29.9804, "lon": -95.3393, "tz": "America/Chicago"},
        "Las Vegas (KLAS)": {"lat": 36.0840, "lon": -115.1537, "tz": "America/Los_Angeles"},
        "Los Angeles (KLAX)": {"lat": 33.9416, "lon": -118.4085, "tz": "America/Los_Angeles"},
        "Miami (KMIA)": {"lat": 25.7959, "lon": -80.2870, "tz": "America/New_York"},
        "Minneapolis (KMSP)": {"lat": 44.8848, "lon": -93.2223, "tz": "America/Chicago"},
        "New Orleans (KMSY)": {"lat": 29.9911, "lon": -90.2592, "tz": "America/Chicago"},
        "New York City (KNYC)": {"lat": 40.7829, "lon": -73.9654, "tz": "America/New_York"},
        "Oklahoma City (KOKC)": {"lat": 35.3931, "lon": -97.6007, "tz": "America/Chicago"},
        "Philadelphia (KPHL)": {"lat": 39.8729, "lon": -75.2437, "tz": "America/New_York"},
        "Phoenix (KPHX)": {"lat": 33.4343, "lon": -112.0083, "tz": "America/Phoenix"}, # No DST shift
        "San Antonio (KSAT)": {"lat": 29.5337, "lon": -98.4698, "tz": "America/Chicago"},
        "San Francisco (KSFO)": {"lat": 37.6213, "lon": -122.3790, "tz": "America/Los_Angeles"},
        "Seattle (KSEA)": {"lat": 47.4502, "lon": -122.3088, "tz": "America/Los_Angeles"},
        "Washington DC (KDCA)": {"lat": 38.8512, "lon": -77.0402, "tz": "America/New_York"}
    }

    tomorrow_iso = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    url = "https://api.open-meteo.com/v1/forecast"
    
    # --- INDUSTRIAL RETRY WRAPPER ---
    # Configures an automated backoff mechanism for handling 500, 502, 503, 504 server faults cleanly
    session = requests.Session()
    retries = Retry(
        total=5, 
        backoff_factor=0.5, # Sleeps 0.5s, 1s, 2s, 4s between connection retries
        status_forcelist=[500, 502, 503, 504]
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    
    forecast_summary = []
    print(f"Pinging Open-Meteo for tomorrow's arrays ({tomorrow_iso})...\n")

    for city, coords in STATIONS.items():
        params = {
            "latitude": coords["lat"],
            "longitude": coords["lon"],
            "hourly": "temperature_2m",
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "timezone": coords["tz"], # Forced explicit string
            "forecast_days": 2
        }
        
        try:
            res = session.get(url, params=params)
            res.raise_for_status()
            data = res.json()
            
            # Isolate Daily Max Element
            daily_dates = data["daily"]["time"]
            daily_highs = data["daily"]["temperature_2m_max"]
            tomorrow_high = next((high for d, high in zip(daily_dates, daily_highs) if d == tomorrow_iso), None)
            
            # Isolate Hourly Time Slices
            hourly_times = data["hourly"]["time"]
            hourly_temps = data["hourly"]["temperature_2m"]
            tomorrow_hourly = [temp for t, temp in zip(hourly_times, hourly_temps) if tomorrow_iso in t]
            
            calculated_peak = max(tomorrow_hourly) if tomorrow_hourly else None
            
            forecast_summary.append({
                "Station": city,
                "API Daily High": f"{tomorrow_high}°F" if tomorrow_high else "N/A",
                "Hourly Array Peak": f"{calculated_peak:.1f}°F" if calculated_peak else "N/A",
                "Hourly Slices": len(tomorrow_hourly)
            })
            
        except Exception as e:
            print(f"❌ Structural failure collecting {city} after maximum retries: {e}")
            continue

    df = pd.DataFrame(forecast_summary)
    print("\n=========================================================")
    print(df.to_string(index=False))
    print("=========================================================\n")

if __name__ == "__main__":
    get_openmeteo_forecasts()