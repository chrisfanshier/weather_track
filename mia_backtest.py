from datetime import datetime, timedelta
import requests
import pandas as pd

def get_miami_past_year_temps() -> pd.DataFrame:
    """
    Dynamically calculates the past 365 days from today
    and pulls the official daily max temperatures for KMIA.
    """
    url = "https://data.rcc-acis.org/StnData"
    
    # Calculate rolling 1-year window based on today's date (May 25, 2026)
    today = datetime.now()
    start_date = (today - timedelta(days=365)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")
    
    payload = {
        "sid": "KMIA",
        "sdate": start_date,
        "edate": end_date,
        "elems": [{"name": "maxt", "vtype": "H"}]
    }
    
    print(f"Fetching KMIA data from {start_date} to {end_date}...")
    response = requests.post(url, json=payload)
    response.raise_for_status()
    
    parsed_records = []
    for row in response.json().get("data", []):
        date_str, max_temp = row[0], row[1]
        
        # Keep only valid numerical readings
        if max_temp.isdigit() or (max_temp.startswith('-') and max_temp[1:].isdigit()):
            parsed_records.append({
                "date": date_str,
                "max_temp_f": int(max_temp)
            })
            
    df = pd.DataFrame(parsed_records)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        
    return df

if __name__ == "__main__":
    df_past_year = get_miami_past_year_temps()
    
    print(f"\nSuccess! Retrieved {len(df_past_year)} days of data.")
    print("\n--- First 5 Days ---")
    print(df_past_year.head())
    print("\n--- Last 5 Days ---")
    print(df_past_year.tail())
    
    print("\n--- Quick Statistics ---")
    print(df_past_year["max_temp_f"].describe())