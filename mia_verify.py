import requests
import pandas as pd
import re

def get_text_parsed_actuals_fixed(start_date, end_date):
    url = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
    params = {"pil": "CLIMIA", "sdate": start_date, "edate": end_date, "fmt": "text", "limit": 9999}
    response = requests.get(url, params=params)
    
    # Split the server response into independent transmission blocks
    chunks = response.text.split("\n\n")
    
    date_pattern = re.compile(r'(?:AM|PM)\s+[A-Z]{3}\s+[A-Z]{3}\s+([A-Z]{3})\s+(\d{1,2})\s+(\d{4})', re.IGNORECASE)
    parsed_records = []
    
    # Process each block in complete isolation
    for chunk in chunks:
        if not chunk.strip():
            continue
            
        # Check if this specific chunk contains a date header
        date_match = date_pattern.search(chunk)
        if date_match:
            month_str, day_str, year_str = date_match.groups()
            try:
                chunk_date = pd.to_datetime(f"{year_str}-{month_str}-{day_str}").strftime("%Y-%m-%d")
            except:
                continue
                
            # The date is valid for this block. Is the MAXIMUM temperature in this same block?
            if "MAXIMUM" in chunk:
                temp_match = re.search(r'MAXIMUM\s+(\d{2,3})', chunk)
                if temp_match:
                    parsed_records.append({
                        "date": chunk_date,
                        "text_high": int(temp_match.group(1))
                    })

    df = pd.DataFrame(parsed_records)
    if df.empty:
        return pd.DataFrame(columns=["date", "text_high"])
        
    # Deduplicate keeping the latest afternoon update for any duplicate dates
    return df.sort_values(by="date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)

def get_noaa_summary_actuals(start_date, end_date):
    url = "https://data.rcc-acis.org/StnData"
    payload = {
        "sid": "KMIA", "sdate": start_date, "edate": end_date,
        "elems": [{"name": "maxt", "vtype": "H"}]
    }
    response = requests.post(url, json=payload)
    response.raise_for_status()
    
    parsed_db = []
    for row in response.json().get("data", []):
        date_str, max_temp = row[0], row[1]
        if max_temp.isdigit():
            parsed_db.append({"date": date_str, "noaa_high": int(max_temp)})
    return pd.DataFrame(parsed_db)

def verify_datasets():
    start, end = "2025-01-01", "2025-12-31"
    
    print("1. Fetching raw text logs with strict chunk boundaries...")
    df_text = get_text_parsed_actuals_fixed(start, end)
    
    print("2. Fetching secondary official NOAA ACIS database...")
    df_noaa = get_noaa_summary_actuals(start, end)
    
    print("3. Merging matrices for comparison check...\n")
    comparison = pd.merge(df_text, df_noaa, on="date", how="inner")
    
    comparison["discrepancy"] = comparison["text_high"] - comparison["noaa_high"]
    mismatches = comparison[comparison["discrepancy"] != 0]
    
    print("="*65)
    print(f"               CROSS-VERIFICATION SUMMARY")
    print("="*65)
    print(f" Total Overlapping Days Compared : {len(comparison)}")
    print(f" Perfect Matches (0°F Delta)     : {len(comparison) - len(mismatches)}")
    print(f" Total Mismatches Found          : {len(mismatches)}")
    print("="*65)
    
    if not mismatches.empty:
        print("\n--- DETECTED MISMATCHES ---")
        print(mismatches.to_string(index=False))
    else:
        print("\n -> 100% clean match! The parsing boundaries are now completely accurate.")

if __name__ == "__main__":
    verify_datasets()