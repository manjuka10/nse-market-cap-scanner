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
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
    "Referer": NSE_HOME + "/",
}


def nse_get(url, timeout=30):
    s = requests.Session(impersonate="chrome")
    s.headers.update(HEADERS)
    s.get(NSE_HOME, timeout=20)
    r = s.get(url, timeout=timeout)
    r.raise_for_status()
    return r


@st.cache_data(ttl=3600)
def get_constituents(universe):
    r = nse_get(INDEX_FILES[universe], timeout=20)
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


def recent_weekdays(start_date, count=8):
    dates = []
    d = start_date

    while len(dates) < count:
        if d.weekday() < 5:
            dates.append(d)
        d -= timedelta(days=1)

    return dates


@st.cache_data(ttl=300)
def get_market_cap_report():
    """
    NSE's official PR Bhavcopy ZIP contains:
        mcapDDMMYYYY.csv

    The current NSE archive location is:
        /archives/equities/bhavcopy/pr/PR{DDMMYY}.zip

    The mcap file contains security-wise full market capitalisation.
    """
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    errors = []

    for d in recent_weekdays(today):
        ddmmyy = d.strftime("%d%m%y")
        ddmmyyyy = d.strftime("%d%m%Y")

        # Current NSE archive URL.
        urls = [
            f"{NSE_ARCHIVES}/archives/equities/bhavcopy/pr/PR{ddmmyy}.zip",
            # Fallback archive location used by older NSE archive pages.
            (
                f"{NSE_ARCHIVES}/content/historical/EQUITIES/"
                f"{d.year}/{d.strftime('%b').upper()}/PR{ddmmyy}.zip"
            ),
        ]

        for url in urls:
            try:
                r = nse_get(url, timeout=40)

                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    names = z.namelist()

                    target = next(
                        (
                            name for name in names
                            if name.lower() == f"mcap{ddmmyyyy}.csv"
                        ),
                        None,
                    )

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
                            "The NSE PR ZIP was downloaded, but the "
                            "security-wise mcap CSV was not found."
                        )

                    with z.open(target) as f:
                        raw = pd.read_csv(f)

                return raw, d

            except Exception as e:
                errors.append(f"{d} ({url}): {e}")

    raise RuntimeError(
        "Could not retrieve the NSE market-cap report from the recent "
        "trading-day archives. " + " | ".join(errors[-4:])
    )


def normalize_market_cap_report(raw):
    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]

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

    # Current NSE mcap file uses:
    #   Symbol
    #   Security Name
    #   Market Cap(Rs.)
    # Market Cap(Rs.) is in rupees, so convert to ₹ crore.
    cap_col = next(
        (
            c for c in df.columns
            if "market cap" in c.lower()
            and "weight" not in c.lower()
        ),
        None,
    )

    if symbol_col is None or cap_col is None:
        raise RuntimeError(
            "NSE mcap report format changed. "
            f"Columns received: {list(df.columns)}"
        )

    out = pd.DataFrame()
    out["Symbol"] = (
        df[symbol_col]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    # NSE's Market Cap(Rs.) is rupees.
    market_cap_rupees = pd.to_numeric(
        df[cap_col]
        .astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("₹", "", regex=False)
        .str.strip(),
        errors="coerce",
    )

    out["Market Cap (₹ Cr)"] = market_cap_rupees / 10_000_000

    return (
        out
        .dropna(subset=["Market Cap (₹ Cr)"])
        .drop_duplicates("Symbol")
    )


def build_table(universe):
    constituents = get_constituents(universe)
    raw, report_date = get_market_cap_report()
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
    if st.button("🔄 Refresh", use_container_width=True):
        get_constituents.clear()
        get_market_cap_report.clear()
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
            f"NSE did not return market-cap data for {missing} "
            "constituent(s). Those rows are left blank."
        )

except Exception as e:
    st.error("Unable to retrieve NSE market-cap data right now.")
    st.caption(str(e))
