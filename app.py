import io
from datetime import datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="Nifty ATH Correction Scanner", page_icon="📊", layout="wide")

IST = ZoneInfo("Asia/Kolkata")
NSE_URL = "https://www.nseindia.com"
NIFTY50_CSV = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
NIFTY100_CSV = "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv"

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/139.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": NSE_URL + "/",
}

def ist_now():
    return datetime.now(IST)

def market_open(dt):
    return dt.weekday() < 5 and time(9, 15) <= dt.time() <= time(15, 30)

def nse_session():
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    session.get(NSE_URL + "/", timeout=15)
    return session

def clean_symbol(value):
    return str(value).strip().upper()

@st.cache_data(ttl=3600, show_spinner=False)
def get_universe(csv_url):
    session = nse_session()
    response = session.get(csv_url, timeout=20)
    response.raise_for_status()
    df = pd.read_csv(io.BytesIO(response.content))
    df.columns = [str(c).strip() for c in df.columns]
    symbol_col = next((c for c in df.columns if c.upper() == "SYMBOL"), None)
    if symbol_col is None:
        raise RuntimeError(f"NSE index CSV changed format. Columns: {list(df.columns)}")
    symbols = [clean_symbol(x) for x in df[symbol_col].dropna().tolist() if clean_symbol(x)]
    return list(dict.fromkeys(symbols))

@st.cache_data(ttl=60, show_spinner=False)
def get_daily_data(symbols):
    return yf.download(
        [s + ".NS" for s in symbols], period="max", interval="1d",
        auto_adjust=False, progress=False, group_by="ticker", threads=True, prepost=False
    )

@st.cache_data(ttl=20, show_spinner=False)
def get_intraday_data(symbols):
    return yf.download(
        [s + ".NS" for s in symbols], period="1d", interval="5m",
        auto_adjust=False, progress=False, group_by="ticker", threads=True, prepost=False
    )

def ticker_frame(data, ticker):
    if data is None or data.empty:
        return pd.DataFrame()
    try:
        if isinstance(data.columns, pd.MultiIndex):
            level0 = data.columns.get_level_values(0)
            level1 = data.columns.get_level_values(1)
            if ticker in level0:
                frame = data[ticker].copy()
            elif ticker in level1:
                frame = data.xs(ticker, axis=1, level=1).copy()
            else:
                return pd.DataFrame()
        else:
            frame = data.copy()
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = [c[-1] if isinstance(c, tuple) else c for c in frame.columns]
        if "Close" not in frame.columns:
            return pd.DataFrame()
        for col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        return frame.dropna(how="all")
    except Exception:
        return pd.DataFrame()

def local_dates(index):
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert(IST).tz_localize(None)
    return pd.Series(idx.date, index=index)

def unavailable_row(symbol):
    return {
        "Stock Name": symbol, "Live Price": np.nan, "All-Time High": np.nan,
        "Correction %": np.nan, "All-Time High Date": "—"
    }

def calculate_stock(symbol, daily_all, intraday_all, dt):
    daily = ticker_frame(daily_all, symbol + ".NS")
    if daily.empty or "High" not in daily.columns or "Close" not in daily.columns:
        return unavailable_row(symbol)
    daily = daily.dropna(subset=["High", "Close"]).copy()
    if daily.empty:
        return unavailable_row(symbol)

    today = dt.date()
    dates = local_dates(daily.index)
    completed = daily.loc[dates < today].copy()
    if completed.empty:
        completed = daily.copy()
    completed = completed.dropna(subset=["High", "Close"])
    if completed.empty:
        return unavailable_row(symbol)

    # True ATH = highest intraday High, not highest closing price.
    ath_idx = completed["High"].idxmax()
    ath = float(completed.loc[ath_idx, "High"])
    ath_date = pd.Timestamp(ath_idx)
    if ath_date.tzinfo is not None:
        ath_date = ath_date.tz_convert(IST)

    intraday = ticker_frame(intraday_all, symbol + ".NS")
    today_price = np.nan
    if not intraday.empty and "Close" in intraday.columns:
        intra_dates = local_dates(intraday.index)
        today_rows = intraday.loc[intra_dates == today].dropna(subset=["Close"])
        if not today_rows.empty:
            today_price = float(today_rows["Close"].iloc[-1])

    # Market hours: live/intraday price. Outside market hours: latest completed close.
    if np.isfinite(today_price) and today_price > 0:
        price = today_price
    else:
        price = float(completed["Close"].iloc[-1])

    if not np.isfinite(price) or price <= 0 or ath <= 0:
        return unavailable_row(symbol)

    correction = (price / ath - 1) * 100
    return {
        "Stock Name": symbol,
        "Live Price": price,
        "All-Time High": ath,
        "Correction %": correction,
        "All-Time High Date": ath_date.strftime("%d-%b-%Y"),
    }

def scan(symbols):
    dt = ist_now()
    daily = get_daily_data(symbols)
    intraday = get_intraday_data(symbols)
    rows = [calculate_stock(symbol, daily, intraday, dt) for symbol in symbols]
    return pd.DataFrame(rows), dt

st.title("📊 Nifty ATH Correction Scanner")
st.subheader("Nifty 50 & Nifty 100 — All-Time High Correction")

universe = st.selectbox("Universe", ["Nifty 50", "Nifty 100"])

try:
    symbols = get_universe(NIFTY50_CSV if universe == "Nifty 50" else NIFTY100_CSV)
except Exception as exc:
    st.error("NSE universe could not be loaded.")
    st.code(str(exc))
    st.stop()

if st.button("🔄 Refresh Now", type="primary", use_container_width=False):
    get_daily_data.clear()
    get_intraday_data.clear()
    with st.spinner(f"Loading {len(symbols)} stocks..."):
        result, updated = scan(symbols)
    st.session_state.ath_result = result
    st.session_state.ath_updated = updated
    st.session_state.ath_universe = universe

if "ath_result" not in st.session_state or st.session_state.get("ath_universe") != universe:
    with st.spinner(f"Loading {len(symbols)} stocks..."):
        result, updated = scan(symbols)
    st.session_state.ath_result = result
    st.session_state.ath_updated = updated
    st.session_state.ath_universe = universe

result = st.session_state.ath_result
updated = st.session_state.ath_updated

st.caption(f"Last Updated: {updated.strftime('%d-%b-%Y %I:%M:%S %p')} IST")

st.dataframe(
    result,
    column_config={
        "Live Price": st.column_config.NumberColumn("Live Price", format="₹%.2f"),
        "All-Time High": st.column_config.NumberColumn("All-Time High", format="₹%.2f"),
        "Correction %": st.column_config.NumberColumn("Correction %", format="%.2f%%"),
    },
    use_container_width=True,
    hide_index=True,
    height=680,
)
