import re

# ... inside your processing loop for each ob in records ...

raw_text = ob.get('rawOb', '')
utc_time_str = ob.get('reportTime', 'UNKNOWN')

if raw_text:
    # 1. PRIMARY SOURCE: Extract the precise Hourly Snapshot Float (T0xxx0xxx)
    # Example: T03000183 -> 30.0°C -> 86.0°F
    t_match = re.search(r'\bT([01])([0-9]{3})[0-9]{4}\b', raw_text)
    
    # 2. SECONDARY SAFETY NET: If no T-Group, check for the 6-Hr Max (1sTTT)
    max_match = re.search(r'\b1([01])([0-9]{3})\b', raw_text)
    
    if t_match:
        sign = -1 if t_match.group(1) == '1' else 1
        celsius = (sign * int(t_match.group(2))) / 10.0
        data_source = "Hourly T-Group"
    elif max_match:
        sign = -1 if max_match.group(1) == '1' else 1
        celsius = (sign * int(max_match.group(2))) / 10.0
        data_source = "6-Hr Max RMK"
    else:
        # 3. FALLBACK ONLY: Trust the API's parsed float if remarks are blank
        celsius = ob.get('temp')
        data_source = "AWC API Estimate"
else:
    celsius = ob.get('temp')
    data_source = "AWC API Estimate"

if celsius is not None:
    inst_f = (celsius * 1.8) + 32
    settled_integer = round(inst_f)
    
    print(f"{utc_time_str:<24} | {inst_f:<6.2f}°F | Settles: {settled_integer}°F | Source: {data_source}")