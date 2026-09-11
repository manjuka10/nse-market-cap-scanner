import re
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="NSE Market Cap Scanner", page_icon="📊", layout="wide")

NSE_HOME = "https://www.nseindia.com"
NSE_REPORTS = "https://www.nseindia.com/all-reports"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/139.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_HOME + "/",
}

@st.cache_resource
def nse_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(NSE_HOME, timeout=15)
    return s

def get_constituents(index_name):
    s = nse_session()
    r = s.get(
        "https://www.nseindia.com/api/equity-stockIndices",
        params={"index": index_name},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    rows = []
    for x in data:
        symbol = x.get("symbol")
        if symbol and symbol != index_name:
            rows.append({
                "Symbol": symbol,
                "Company": x.get("meta", {}).get("companyName", symbol),
            })
    df = pd.DataFrame(rows).drop_duplicates("Symbol")
    if df.empty:
        raise RuntimeError("NSE returned no index constituents.")
    return df

def parse_cap(x):
    if pd.isna(x):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    t = str(x).strip().replace(",", "").replace("₹", "")
    m = re.match(r"^([-+]?\d+(?:\.\d+)?)\s*Lakh\s*Cr$", t, re.I)
    if m:
        return float(m.group(1)) * 100000
    m = re.match(r"^([-+]?\d+(?:\.\d+)?)\s*Cr$", t, re.I)
    if m:
        return float(m.group(1))
    try:
        return float(t)
    except ValueError:
        return None

def find_report_url(html):
    links = re.findall(r'href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S)
    candidates = []
    for href, label in links:
        text = re.sub("<[^>]+>", " ", label)
        combined = (href + " " + text).lower()
        if "market" in combined and "capital" in combined and "weight" in combined:
            candidates.append(href)
    if not candidates:
        raise RuntimeError(
            "NSE did not expose the current market-cap report download link on its All Reports page."
        )
    href = candidates[0]
    if href.startswith("/"):
        href = NSE_HOME + href
    elif href.startswith("//"):
        href = "https:" + href
    return href

def get_market_cap_report():
    s = nse_session()
    page = s.get(NSE_REPORTS, timeout=20)
    page.raise_for_status()
    url = find_report_url(page.text)
    r = s.get(url, timeout=30)
    r.raise_for_status()
    try:
        df = pd.read_csv(io.BytesIO(r.content))
        if len(df.columns) > 1:
            return df
    except Exception:
        pass
    try:
        return pd.read_excel(io.BytesIO(r.content))
    except Exception as e:
        raise RuntimeError("NSE market-cap report could not be parsed.") from e

def normalize_report(raw):
    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]
    symbol_col = next(
        (c for c in df.columns if c.lower() in {"symbol", "nse code", "security"}), None
    )
    name_col = next(
        (c for c in df.columns if "company" in c.lower() or "security name" in c.lower()), None
    )
    cap_col = next(
        (
            c for c in df.columns
            if "market capital" in c.lower()
            and "weight" not in c.lower()
            and "rank" not in c.lower()
        ),
        None,
    )
    if cap_col is None:
        cap_col = next((c for c in df.columns if "mkt" in c.lower() and "cap" in c.lower()), None)
    if symbol_col is None or cap_col is None:
        raise RuntimeError("Could not identify Symbol and Market Capitalisation columns in the NSE report.")
    out = pd.DataFrame({
        "Symbol": df[symbol_col].astype(str).str.strip().str.upper(),
        "Company": df[name_col].astype(str).str.strip() if name_col else df[symbol_col].astype(str),
        "Market Cap (₹ Cr)": df[cap_col].map(parse_cap),
    })
    return out.dropna(subset=["Market Cap (₹ Cr)"]).drop_duplicates("Symbol")

@st.cache_data(ttl=300)
def load_data(universe):
    constituents = get_constituents(universe.upper())
    report = normalize_report(get_market_cap_report())
    out = constituents.merge(report, on="Symbol", how="left", suffixes=("", "_report"))
    if "Company_report" in out:
        out["Company"] = out["Company_report"].fillna(out["Company"])
        out = out.drop(columns=["Company_report"])
    out = out.sort_values("Market Cap (₹ Cr)", ascending=False, na_position="last").reset_index(drop=True)
    out.insert(0, "Rank", range(1, len(out) + 1))
    return out[["Rank", "Symbol", "Company", "Market Cap (₹ Cr)"]]

st.title("📊 NSE Market Cap Scanner")

c1, c2 = st.columns([2, 1])
with c1:
    universe = st.selectbox("Universe", ["NIFTY 50", "NIFTY 100"])
with c2:
    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

try:
    table = load_data(universe)
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    st.caption("Last updated: " + now.strftime("%d-%m-%Y %I:%M:%S %p IST"))
    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Rank": st.column_config.NumberColumn("Rank", format="%d"),
            "Market Cap (₹ Cr)": st.column_config.NumberColumn("Market Cap (₹ Cr)", format="₹ %.2f"),
        },
    )
except Exception as e:
    st.error("Unable to retrieve NSE market-cap data right now.")
    st.caption(str(e))
