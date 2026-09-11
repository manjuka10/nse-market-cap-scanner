import time
from datetime import datetime
from urllib.parse import quote
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
NSE_ARCHIVES = "https://nsearchives.nseindia.com"

INDEX_FILES = {
    "NIFTY 50": f"{NSE_ARCHIVES}/content/indices/ind_nifty50list.csv",
    "NIFTY 100": f"{NSE_ARCHIVES}/content/indices/ind_nifty100list.csv",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_HOME + "/",
}


def create_nse_session():
    s = requests.Session(impersonate="chrome")
    s.headers.update(HEADERS)

    # Establish NSE cookies before calling the quote API.
    r = s.get(NSE_HOME, timeout=20)
    r.raise_for_status()
    return s


@st.cache_data(ttl=3600)
def get_constituents(universe):
    s = create_nse_session()

    r = s.get(INDEX_FILES[universe], timeout=20)
    r.raise_for_status()

    df = pd.read_csv(r.content)
    df.columns = [str(c).strip() for c in df.columns]

    symbol_col = next(
        (c for c in df.columns if c.lower() == "symbol"),
        None,
    )

    company_col = next(
        (
            c for c in df.columns
            if c.lower() in {
                "company name",
                "companyname",
                "security name",
                "security",
            }
        ),
        None,
    )

    if symbol_col is None:
        raise RuntimeError("NSE index CSV does not contain Symbol column.")

    out = pd.DataFrame()
    out["Symbol"] = (
        df[symbol_col]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    out["Company"] = (
        df[company_col].astype(str).str.strip()
        if company_col
        else out["Symbol"]
    )

    return out.drop_duplicates("Symbol").reset_index(drop=True)


def get_total_market_cap(s, symbol, retries=3):
    """
    NSE quote-equity -> trade_info -> totalMarketCap.

    NSE's API value is expressed in ₹ lakh.
    Convert to ₹ crore by dividing by 100.

    Example from NSE:
        Total Market Cap shown on the website = ₹ Cr
        API field = totalMarketCap
    """

    encoded_symbol = quote(symbol, safe="")
    quote_url = f"{NSE_HOME}/get-quotes/equity?symbol={encoded_symbol}"
    api_url = f"{NSE_HOME}/api/quote-equity"

    last_error = None

    for attempt in range(retries):
        try:
            # NSE quote page first establishes the correct session/cookies.
            page = s.get(quote_url, timeout=20)
            page.raise_for_status()

            time.sleep(0.15)

            response = s.get(
                api_url,
                params={
                    "symbol": symbol,
                    "section": "trade_info",
                },
                headers={
                    "Referer": quote_url,
                    "Accept": "application/json,text/plain,*/*",
                },
                timeout=20,
            )

            response.raise_for_status()
            data = response.json()

            trade_info = (
                data
                .get("marketDeptOrderBook", {})
                .get("tradeInfo", {})
            )

            api_value = trade_info.get("totalMarketCap")

            if api_value is None:
                raise RuntimeError(
                    f"NSE did not return totalMarketCap for {symbol}."
                )

            # NSE API totalMarketCap is in ₹ lakh.
            market_cap_crore = float(api_value) / 100.0

            return market_cap_crore

        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(1.0)

    raise RuntimeError(str(last_error))


@st.cache_data(ttl=120)
def get_market_caps(symbols):
    """
    Fetch all selected stocks from NSE using one persistent session.

    Sequential requests are intentional: this reduces NSE rate-limit/WAF
    issues compared with firing 50-100 simultaneous requests.
    """
    s = create_nse_session()

    results = {}
    errors = {}

    for i, symbol in enumerate(symbols):
        try:
            results[symbol] = get_total_market_cap(s, symbol)
        except Exception as e:
            results[symbol] = None
            errors[symbol] = str(e)

        # Small pause between securities to reduce NSE request throttling.
        if i < len(symbols) - 1:
            time.sleep(0.10)

    return results, errors


def build_table(universe):
    constituents = get_constituents(universe)

    market_caps, errors = get_market_caps(
        constituents["Symbol"].tolist()
    )

    out = constituents.copy()
    out["Market Cap (₹ Cr)"] = out["Symbol"].map(market_caps)

    out = out.sort_values(
        "Market Cap (₹ Cr)",
        ascending=False,
        na_position="last",
    ).reset_index(drop=True)

    out.insert(0, "Rank", range(1, len(out) + 1))

    return (
        out[["Rank", "Symbol", "Company", "Market Cap (₹ Cr)"]],
        errors,
    )


st.title("📊 NSE Market Cap Scanner")

col1, col2 = st.columns([2, 1])

with col1:
    universe = st.selectbox(
        "Universe",
        ["NIFTY 50", "NIFTY 100"],
    )

with col2:
    if st.button("🔄 Refresh", use_container_width=True):
        get_constituents.clear()
        get_market_caps.clear()
        st.rerun()

try:
    table, errors = build_table(universe)

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
            f"NSE did not return market-cap data for {missing} "
            "stock(s). Those rows are left blank."
        )

        with st.expander("NSE request details"):
            for symbol, error in errors.items():
                st.write(f"**{symbol}:** {error}")

except Exception as e:
    st.error("Unable to retrieve NSE market-cap data right now.")
    st.caption(str(e))
