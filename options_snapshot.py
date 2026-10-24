"""Copied from finance_engine/src/options.py (keep the two in step). Daily option-chain snapshot and log. Positioning signal: real option contracts held and traded.

Sources (all free, no key):
- Nifty: NSE option chain (unofficial endpoint, can be blocked), nearest weekly expiry. OI and volume in contracts.
- BTC / ETH: Deribit public API (largest crypto options venue). OI and volume in coins.
- S&P 500: SPY options via Yahoo Finance (yfinance). OI and volume in contracts.

Each run appends one row per underlying per day to data/options/options_log.csv (same day = overwritten),
so expiry-day and PCR patterns can be tested once enough history builds up. Options data is not kept
anywhere for free, so a missed day is a permanent gap.
"""
import csv
from datetime import date, datetime, timezone

import requests
import yfinance as yf

from pathlib import Path

LOG = Path(__file__).parent / "data" / "options_log.csv"
FIELDS = ["date", "underlying", "source", "as_of", "spot", "expiry", "days_to_expiry",
          "pcr_oi", "pcr_volume", "call_oi", "put_oi", "call_volume", "put_volume",
          "max_pain", "max_pain_vs_spot_pct", "top_call_oi_strikes", "top_put_oi_strikes",
          "atm_strike", "atm_straddle", "expected_move_pct",
          "all_expiries_pcr_oi", "all_expiries_pcr_volume"]
NSE_HOME = "https://www.nseindia.com/option-chain"
NSE_INFO = "https://www.nseindia.com/api/option-chain-contract-info?symbol={}"
NSE_CHAIN = "https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol={}&expiry={}"
DERIBIT = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"


def _r(x, n=2):
    return None if x is None else round(float(x), n)


def _summarise(strikes, spot):
    """strikes: {strike: dict(call_oi, put_oi, call_vol, put_vol, call_px, put_px)}. Prices in underlying currency."""
    ks = sorted(strikes)
    c_oi = sum(strikes[k]["call_oi"] for k in ks)
    p_oi = sum(strikes[k]["put_oi"] for k in ks)
    c_v = sum(strikes[k]["call_vol"] for k in ks)
    p_v = sum(strikes[k]["put_vol"] for k in ks)
    # Max pain: the expiry price at which option holders (buyers) would collect the least.
    pain = {s: sum(max(0, s - k) * strikes[k]["call_oi"] + max(0, k - s) * strikes[k]["put_oi"] for k in ks) for s in ks}
    max_pain = min(pain, key=pain.get) if pain else None
    atm = min(ks, key=lambda k: abs(k - spot))
    cp, pp = strikes[atm].get("call_px"), strikes[atm].get("put_px")
    straddle = cp + pp if cp and pp else None
    top = lambda key: "|".join(str(int(k) if float(k).is_integer() else k)
                               for k in sorted(ks, key=lambda k: -strikes[k][key])[:3])
    return {"pcr_oi": _r(p_oi / c_oi) if c_oi else None, "pcr_volume": _r(p_v / c_v) if c_v else None,
            "call_oi": _r(c_oi, 0), "put_oi": _r(p_oi, 0), "call_volume": _r(c_v, 0), "put_volume": _r(p_v, 0),
            "max_pain": max_pain, "max_pain_vs_spot_pct": _r((max_pain / spot - 1) * 100) if max_pain else None,
            "top_call_oi_strikes": top("call_oi"), "top_put_oi_strikes": top("put_oi"),
            "atm_strike": atm, "atm_straddle": _r(straddle), "expected_move_pct": _r(straddle / spot * 100) if straddle else None}


def _blank():
    return {"call_oi": 0, "put_oi": 0, "call_vol": 0, "put_vol": 0, "call_px": None, "put_px": None}


# ---------- Nifty (NSE) ----------
def nse_chain(symbol="NIFTY"):
    from curl_cffi import requests as creq  # installed with yfinance; looks like a real browser
    s = creq.Session(impersonate="chrome")
    s.get(NSE_HOME, timeout=20)
    hdr = {"Referer": NSE_HOME, "Accept": "application/json"}
    expiry = s.get(NSE_INFO.format(symbol), timeout=20, headers=hdr).json()["expiryDates"][0]
    rec = s.get(NSE_CHAIN.format(symbol, expiry), timeout=20, headers=hdr).json()["records"]
    spot = float(rec["underlyingValue"])
    strikes = {}
    for row in rec["data"]:
        k = float(row["strikePrice"])
        d = strikes.setdefault(k, _blank())
        for side, key in (("CE", "call"), ("PE", "put")):
            o = row.get(side)
            if o:
                d[f"{key}_oi"] = float(o.get("openInterest") or 0)
                d[f"{key}_vol"] = float(o.get("totalTradedVolume") or 0)
                d[f"{key}_px"] = float(o.get("lastPrice") or 0) or None
    exp = datetime.strptime(expiry, "%d-%b-%Y").date()
    out = {"underlying": symbol, "source": "NSE option chain (unofficial endpoint)",
           "as_of": rec.get("timestamp"), "spot": spot, "expiry": str(exp), "days_to_expiry": (exp - date.today()).days}
    out.update(_summarise(strikes, spot))
    return out


# ---------- BTC / ETH (Deribit) ----------
def deribit_chain(currency):
    rows = requests.get(DERIBIT, params={"currency": currency, "kind": "option"}, timeout=20).json()["result"]
    by_exp, all_c, all_p, all_cv, all_pv = {}, 0.0, 0.0, 0.0, 0.0
    for r in rows:
        _, exp, strike, cp = r["instrument_name"].split("-")
        oi, vol = float(r.get("open_interest") or 0), float(r.get("volume") or 0)
        if cp == "C":
            all_c += oi; all_cv += vol
        else:
            all_p += oi; all_pv += vol
        by_exp.setdefault(exp, []).append((float(strike), cp, oi, vol, r.get("mark_price"), r.get("underlying_price")))
    today = datetime.now(timezone.utc).date()
    dated = sorted((datetime.strptime(e, "%d%b%y").date(), e) for e in by_exp)
    # Nearest expiry at least 1 day away (same-day expiries are almost settled and distort the read).
    exp_d, exp = next((d, e) for d, e in dated if (d - today).days >= 1)
    legs = by_exp[exp]
    spot = float(next(u for *_, u in legs if u))
    strikes = {}
    for k, cp, oi, vol, mark, und in legs:
        d = strikes.setdefault(k, _blank())
        key = "call" if cp == "C" else "put"
        d[f"{key}_oi"], d[f"{key}_vol"] = oi, vol
        d[f"{key}_px"] = float(mark) * float(und) if mark and und else None  # Deribit quotes in coin; convert to USD
    out = {"underlying": currency, "source": "Deribit public API", "as_of": str(datetime.now(timezone.utc))[:16] + " UTC",
           "spot": _r(spot), "expiry": str(exp_d), "days_to_expiry": (exp_d - today).days}
    out.update(_summarise(strikes, spot))
    out["all_expiries_pcr_oi"] = _r(all_p / all_c) if all_c else None
    out["all_expiries_pcr_volume"] = _r(all_pv / all_cv) if all_cv else None
    return out


# ---------- S&P 500 (SPY via Yahoo) ----------
def spy_chain():
    t = yf.Ticker("SPY")
    spot = float(t.history(period="5d")["Close"].dropna().iloc[-1])
    today = date.today()
    exp = next(e for e in t.options if (date.fromisoformat(e) - today).days >= 1)
    ch = t.option_chain(exp)
    strikes = {}
    for df, key in ((ch.calls, "call"), (ch.puts, "put")):
        for _, r in df.iterrows():
            d = strikes.setdefault(float(r["strike"]), _blank())
            d[f"{key}_oi"] = float(r["openInterest"] or 0) if r["openInterest"] == r["openInterest"] else 0.0
            d[f"{key}_vol"] = float(r["volume"] or 0) if r["volume"] == r["volume"] else 0.0
            d[f"{key}_px"] = float(r["lastPrice"]) or None
    out = {"underlying": "SPY", "source": "Yahoo Finance via yfinance (OI updates once a day)",
           "as_of": str(today), "spot": _r(spot), "expiry": exp, "days_to_expiry": (date.fromisoformat(exp) - today).days}
    out.update(_summarise(strikes, spot))
    return out


# ---------- log ----------
def _save(rows):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    have = {}
    if LOG.exists():
        with open(LOG, encoding="utf-8") as f:
            have = {(r["date"], r["underlying"]): r for r in csv.DictReader(f)}
    for r in rows:
        have[(r["date"], r["underlying"])] = r
    with open(LOG, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(have.values(), key=lambda r: (r["date"], r["underlying"])))
    return len(have)


def _history(underlying, n=5):
    if not LOG.exists():
        return []
    with open(LOG, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["underlying"] == underlying]
    return [{"date": r["date"], "pcr_oi": r["pcr_oi"], "max_pain": r["max_pain"], "spot": r["spot"]} for r in rows[-n:]]


def snapshot():
    out = {"ok": True, "warnings": [], "label": "positioning (real option contracts)", "chains": {}}
    rows = []
    for name, fn in [("nifty", nse_chain), ("btc", lambda: deribit_chain("BTC")),
                     ("eth", lambda: deribit_chain("ETH")), ("sp500_spy", spy_chain)]:
        try:
            r = fn()
            r["date"] = str(date.today())
            rows.append(r)
            out["chains"][name] = r
        except Exception as e:
            out["chains"][name] = None
            out["warnings"].append(f"{name} option chain unavailable ({type(e).__name__}: {e})")
    out["log_rows_total"] = _save(rows) if rows else None
    out["log_file"] = str(LOG)
    for name, r in out["chains"].items():
        if r:
            r["recent_log"] = _history(r["underlying"])
    out["how_to_read"] = {
        "pcr_oi": "Put/call ratio of open contracts. Below ~0.7 = more call positions; above ~1.2 = more put positions. "
                  "Writers (sellers) dominate index options, so a high PCR is often read as support. Engine thresholds, not rules.",
        "max_pain": "Expiry price where option buyers collect least. Prices sometimes drift toward it near expiry; weak evidence.",
        "top_strikes": "Largest call OI = levels sellers bet won't be crossed (resistance); largest put OI = support.",
        "expected_move_pct": "ATM call + put price as % of spot: the move the market is pricing by expiry.",
        "caution": "Snapshot only. History in the log is too short to test any pattern until a few months accumulate."}
    return out
