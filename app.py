import io
import zipfile
from datetime import datetime, timedelta
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
NSE_ARCHIVE = "https://nsearchives.nseindia.com"

INDEX_FILES = {
    "NIFTY 50": f"{NSE_ARCHIVE}/content/indices/ind_nifty50list.csv",
    "NIFTY 100": f"{NSE_ARCHIVE}/content/indices/ind_nifty100list.csv",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/139.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
    "Referer": NSE_HOME + "/",
}


def nse_get(url, timeout=30):
    s = requests.Session(impersonate="chrome")
    s.headers.update(HEADERS)
    r = s.get(url, timeout=timeout)
    r.raise_for_status()
    return r


@st.cache_data(ttl=3600)
def get_constituents(universe):
    r = nse_get(INDEX_FILES[universe], timeout=20)
    df = pd.read_csv(io.BytesIO(r.content))
    df.columns = [str(c).strip() for c in df.columns]

    symbol_col = next(
        (c for c in df.columns if c.lower() == "symbol"), None
    )
    company_col = next(
        (
            c for c in df.columns
            if c.lower() in {"company name", "companyname", "security"}
        ),
        None,
    )

    if symbol_col is None:
        raise RuntimeError("NSE index file does not contain a Symbol column.")

    out = pd.DataFrame()
    out["Symbol"] = df[symbol_col].astype(str).str.strip().str.upper()
    out["Company"] = (
        df[company_col].astype(str).str.strip()
        if company_col
        else out["Symbol"]
    )

    return out.drop_duplicates("Symbol").reset_index(drop=True)


def trading_dates_back(start_date, count=10):
    dates = []
    d = start_date

    while len(dates) < count:
        if d.weekday() < 5:
            dates.append(d)
        d -= timedelta(days=1)

    return dates


@st.cache_data(ttl=300)
def get_nse_market_cap_report():
    """
    NSE's official PR bundle contains:
        mcapDDMMYYYY.csv

    This is the NSE security-wise market-cap report. We download the
    latest available trading-day PR bundle and extract only its mcap file.
    """
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()

    errors = []

    for d in trading_dates_back(today, count=8):
        ddmmyyyy = d.strftime("%d%m%Y")
        ddmmyy = d.strftime("%d%m%y")
        month = d.strftime("%b").upper()

        # NSE PR archive pattern.
        url = (
            f"{NSE_ARCHIVE}/content/historical/EQUITIES/"
            f"{d.year}/{month}/PR{ddmmyy}.zip"
        )

        try:
            r = nse_get(url, timeout=40)

            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                names = z.namelist()

                target = next(
                    (
                        name for name in names
                        if name.lower().endswith(f"mcap{ddmmyyyy}.csv")
                    ),
                    None,
                )

                # Be tolerant of an unexpected internal path/name.
                if target is None:
                    target = next(
                        (
                            name for name in names
                            if "mcap" in name.lower()
                            and name.lower().endswith(".csv")
                        ),
                        None,
                    )

                if target is None:
                    raise RuntimeError(
                        "NSE PR bundle was downloaded but its mcap CSV was not found."
                    )

                with z.open(target) as f:
                    raw = pd.read_csv(f)

            return raw, d

        except Exception as e:
            errors.append(f"{d}: {e}")

    raise RuntimeError(
        "Could not retrieve an NSE market-cap report from the recent "
        "trading-day archives. " + " | ".join(errors[-3:])
    )


def normalize_market_cap_report(raw):
    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]

    # Find the security symbol column.
    symbol_col = next(
        (
            c for c in df.columns
            if c.lower() in {
                "symbol",
                "security symbol",
                "nse symbol",
                "symbol name",
            }
        ),
        None,
    )

    # Find market-cap column.
    cap_col = next(
        (
            c for c in df.columns
            if "market" in c.lower()
            and "capital" in c.lower()
        ),
        None,
    )

    if symbol_col is None or cap_col is None:
        raise RuntimeError(
            "NSE market-cap report format changed. "
            f"Columns received: {list(df.columns)}"
        )

    out = pd.DataFrame()
    out["Symbol"] = (
        df[symbol_col]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    out["Market Cap (₹ Cr)"] = pd.to_numeric(
        df[cap_col]
        .astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("₹", "", regex=False)
        .str.strip(),
        errors="coerce",
    )

    return out.dropna(subset=["Market Cap (₹ Cr)"]).drop_duplicates("Symbol")


def build_table(universe):
    constituents = get_constituents(universe)
    raw, report_date = get_nse_market_cap_report()
    market_caps = normalize_market_cap_report(raw)

    out = constituents.merge(
        market_caps,
        on="Symbol",
        how="left",
    )

    out = out.sort_values(
        "Market Cap (₹ Cr)",
        ascending=False,
        na_position="last",
    ).reset_index(drop=True)

    out.insert(0, "Rank", range(1, len(out) + 1))

    return (
        out[["Rank", "Symbol", "Company", "Market Cap (₹ Cr)"]],
        report_date,
    )


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
    get_nse_market_cap_report.clear()
    st.rerun()

try:
    table, report_date = build_table(universe)

    now = datetime.now(ZoneInfo("Asia/Kolkata"))

    st.caption(
        "Last updated: "
        + now.strftime("%d-%m-%Y %I:%M:%S %p IST")
        + "  |  NSE market-cap report: "
        + report_date.strftime("%d-%m-%Y")
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
            f"NSE market-cap data was not available for {missing} "
            "constituent(s) in the selected universe. "
            "Those rows are left blank."
        )

except Exception as e:
    st.error("Unable to retrieve NSE market-cap data right now.")
    st.caption(str(e))
