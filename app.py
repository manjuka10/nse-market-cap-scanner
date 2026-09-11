import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from curl_cffi import requests

st.set_page_config(
    page_title="NSE Market Cap Scanner",
    page_icon="📊",
    layout="wide",
)

NSE_HOME = "https://www.nseindia.com"
NSE_ARCHIVE = "https://nsearchives.nseindia.com/content/indices"

INDEX_FILES = {
    "NIFTY 50": f"{NSE_ARCHIVE}/ind_nifty50list.csv",
    "NIFTY 100": f"{NSE_ARCHIVE}/ind_nifty100list.csv",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/139.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
    "Referer": NSE_HOME + "/",
}


def new_nse_session():
    session = requests.Session(impersonate="chrome")
    session.headers.update(HEADERS)
    session.get(NSE_HOME, timeout=20)
    return session


@st.cache_data(ttl=3600)
def get_constituents(universe):
    url = INDEX_FILES[universe]

    # NSE's official index constituent CSV.
    s = new_nse_session()
    r = s.get(url, timeout=20)
    r.raise_for_status()

    df = pd.read_csv(io.BytesIO(r.content))
    df.columns = [str(c).strip() for c in df.columns]

    symbol_col = next(
        (c for c in df.columns if c.lower() == "symbol"),
        None,
    )
    company_col = next(
        (
            c for c in df.columns
            if c.lower() in {"company name", "companyname", "security"}
        ),
        None,
    )

    if symbol_col is None:
        raise RuntimeError("NSE index CSV does not contain a Symbol column.")

    out = pd.DataFrame()
    out["Symbol"] = df[symbol_col].astype(str).str.strip().str.upper()

    if company_col:
        out["Company"] = df[company_col].astype(str).str.strip()
    else:
        out["Company"] = out["Symbol"]

    return out.drop_duplicates("Symbol").reset_index(drop=True)


def parse_market_cap(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", "").replace("₹", "")
    try:
        return float(text)
    except ValueError:
        return None


def fetch_one_market_cap(symbol):
    """
    NSE quote-equity trade_info returns totalMarketCap for the security.
    This is NSE data; no Yahoo Finance market-cap data is used.
    """
    s = new_nse_session()

    quote_page = f"{NSE_HOME}/get-quotes/equity?symbol={symbol}"
    api_url = f"{NSE_HOME}/api/quote-equity"

    # Open the quote page first so NSE can establish the required session.
    page = s.get(quote_page, timeout=20)
    page.raise_for_status()

    r = s.get(
        api_url,
        params={"symbol": symbol, "section": "trade_info"},
        timeout=20,
        headers={"Referer": quote_page},
    )
    r.raise_for_status()

    data = r.json()
    trade_info = (
        data.get("marketDeptOrderBook", {})
        .get("tradeInfo", {})
    )

    market_cap = trade_info.get("totalMarketCap")

    if market_cap is None:
        raise RuntimeError(f"NSE returned no total market cap for {symbol}.")

    return symbol, parse_market_cap(market_cap)


@st.cache_data(ttl=300)
def get_market_caps(symbols):
    results = {}

    # Parallel requests make Nifty 100 refresh substantially faster.
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(fetch_one_market_cap, symbol): symbol
            for symbol in symbols
        }

        for future in as_completed(futures):
            symbol = futures[future]
            try:
                sym, cap = future.result()
                results[sym] = cap
            except Exception:
                results[symbol] = None

    return results


def build_table(universe):
    constituents = get_constituents(universe)
    caps = get_market_caps(constituents["Symbol"].tolist())

    out = constituents.copy()
    out["Market Cap (₹ Cr)"] = out["Symbol"].map(caps)

    # Largest market cap first.
    out = out.sort_values(
        "Market Cap (₹ Cr)",
        ascending=False,
        na_position="last",
    ).reset_index(drop=True)

    out.insert(0, "Rank", range(1, len(out) + 1))

    return out[["Rank", "Symbol", "Company", "Market Cap (₹ Cr)"]]


st.title("📊 NSE Market Cap Scanner")

col1, col2 = st.columns([2, 1])

with col1:
    universe = st.selectbox(
        "Universe",
        ["NIFTY 50", "NIFTY 100"],
    )

with col2:
    refresh = st.button(
        "🔄 Refresh",
        use_container_width=True,
    )

if refresh:
    get_constituents.clear()
    get_market_caps.clear()
    st.rerun()

try:
    table = build_table(universe)

    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    st.caption(
        "Last updated: "
        + now.strftime("%d-%m-%Y %I:%M:%S %p IST")
    )

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Rank": st.column_config.NumberColumn(
                "Rank",
                format="%d",
            ),
            "Symbol": st.column_config.TextColumn("Symbol"),
            "Company": st.column_config.TextColumn("Company"),
            "Market Cap (₹ Cr)": st.column_config.NumberColumn(
                "Market Cap (₹ Cr)",
                format="₹ %.2f",
            ),
        },
    )

    missing = int(table["Market Cap (₹ Cr)"].isna().sum())
    if missing:
        st.warning(
            f"NSE did not return market-cap data for {missing} stock(s). "
            "Those rows are left blank rather than using another data source."
        )

except Exception as e:
    st.error("Unable to retrieve NSE market-cap data right now.")
    st.caption(str(e))
