"""Weekly collector: public market-flow data -> data/*.csv, plus a Telegram summary.
Every source is tried independently; one failure never stops the others. data/run_log.csv records what happened.
"""
import csv
import io
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime.now(IST)
TODAY = NOW.strftime("%Y-%m-%d")
log = []


def note(source, ok, detail=""):
    log.append({"run_date": TODAY, "source": source, "ok": ok, "detail": str(detail)[:300]})
    print(("OK   " if ok else "FAIL ") + source + (": " + str(detail) if detail else ""))


def browser_session():
    try:
        from curl_cffi import requests as creq
        return creq.Session(impersonate="chrome")
    except ImportError:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        return s


def _sortable(v):
    v = str(v)
    for fmt_ in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return (0, datetime.strptime(v, fmt_).timestamp(), "")
        except ValueError:
            pass
    try:
        return (0, float(v), "")
    except ValueError:
        return (1, 0, v)


def upsert(path, rows, key, fields):
    existing = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            existing = {tuple(r[k] for k in key): r for r in csv.DictReader(f)}
    for r in rows:
        existing[tuple(str(r[k]) for k in key)] = {k: r.get(k) for k in fields}
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(sorted(existing.values(), key=lambda r: tuple(_sortable(r[k]) for k in key)))


def num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except ValueError:
        return None


# ---------- 1. NSE FII/DII (latest day only) ----------
def nse_fii_dii():
    s = browser_session()
    s.get("https://www.nseindia.com", timeout=30)
    r = s.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=30,
              headers={"Accept": "application/json", "Referer": "https://www.nseindia.com/"})
    r.raise_for_status()
    out = {}
    for row in r.json():
        cat = str(row.get("category", "")).upper()
        who = "fii" if ("FII" in cat or "FPI" in cat) else "dii" if "DII" in cat else None
        if who:
            out["date"] = row.get("date")
            out[who + "_buy_cr"], out[who + "_sell_cr"] = num(row.get("buyValue")), num(row.get("sellValue"))
            out[who + "_net_cr"] = num(row.get("netValue"))
    if "date" not in out:
        raise ValueError("no FII/DII rows in response")
    fields = ["date", "fii_buy_cr", "fii_sell_cr", "fii_net_cr", "dii_buy_cr", "dii_sell_cr", "dii_net_cr"]
    upsert(DATA / "fii_dii_daily.csv", [out], ["date"], fields)
    return out


# ---------- 2. NSDL FPI monthly net investment (current calendar year) ----------
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]


def nsdl_fpi():
    s = browser_session()
    r = s.get("https://www.fpi.nsdl.co.in/Reports/Yearwise.aspx?RptType=6", timeout=60)
    r.raise_for_status()
    (DATA / "nsdl_last_page.html").write_text(r.text, encoding="utf-8")  # kept for debugging the parser
    rows = []
    for t in pd.read_html(io.StringIO(r.text)):
        t = t.astype(str)
        for _, row in t.iterrows():
            cells = [c for c in row.tolist() if c and c != "nan"]
            if not cells:
                continue
            m = cells[0].strip().lower()
            if m in MONTHS:
                vals = [num(c) for c in cells[1:]]
                vals = [v for v in vals if v is not None]
                if vals:
                    rows.append({"year": NOW.year, "month": m.title(), "month_no": MONTHS.index(m) + 1,
                                 "equity_net_cr": vals[0], "total_net_cr": vals[-1], "collected": TODAY})
    if not rows:
        raise ValueError("no monthly rows found in NSDL page (layout may have changed)")
    # one row per month; keep the last occurrence
    uniq = {r["month_no"]: r for r in rows}
    rows = [uniq[k] for k in sorted(uniq)]
    upsert(DATA / "fpi_monthly.csv", rows, ["year", "month_no"],
           ["year", "month_no", "month", "equity_net_cr", "total_net_cr", "collected"])
    latest = rows[-1]
    upsert(DATA / "fpi_mtd_snapshots.csv",
           [{"collected": TODAY, "year": latest["year"], "month": latest["month"],
             "equity_mtd_cr": latest["equity_net_cr"]}],
           ["collected"], ["collected", "year", "month", "equity_mtd_cr"])
    return latest


def weekly_fpi_change():
    p = DATA / "fpi_mtd_snapshots.csv"
    if not p.exists():
        return None
    snaps = list(csv.DictReader(open(p, encoding="utf-8")))
    if len(snaps) < 2:
        return None
    a, b = snaps[-2], snaps[-1]
    if (a["year"], a["month"]) == (b["year"], b["month"]):
        return num(b["equity_mtd_cr"]) - num(a["equity_mtd_cr"])
    return None  # new month started; month-to-date already is the flow since the 1st


# ---------- 3. Market indicators ----------
TICKERS = {"dollar_index": "DX-Y.NYB", "usd_inr": "INR=X", "us_10y_yield": "^TNX", "brent": "BZ=F",
           "nifty": "^NSEI", "india_vix": "^INDIAVIX", "sp500": "^GSPC",
           "INDA": "INDA", "EEM": "EEM", "SPY": "SPY"}


def indicators():
    import yfinance as yf
    rows = []
    for name, sym in TICKERS.items():
        try:
            c = yf.Ticker(sym).history(period="1y", auto_adjust=True)["Close"].dropna()
            last = float(c.iloc[-1])
            def chg(days):
                past = c[c.index <= c.index[-1] - pd.Timedelta(days=days)]
                return round((last / float(past.iloc[-1]) - 1) * 100, 2) if len(past) else None
            rows.append({"collected": TODAY, "name": name, "symbol": sym, "as_of": str(c.index[-1].date()),
                         "close": round(last, 4), "chg_1w_pct": chg(7), "chg_1m_pct": chg(30),
                         "chg_3m_pct": chg(91), "above_200dma": last > float(c.iloc[-200:].mean())})
        except Exception as e:
            note("indicator " + name, False, f"{type(e).__name__}: {e}")
    if not rows:
        raise ValueError("no indicators fetched")
    upsert(DATA / "indicators.csv", rows, ["collected", "name"],
           ["collected", "name", "symbol", "as_of", "close", "chg_1w_pct", "chg_1m_pct", "chg_3m_pct", "above_200dma"])
    return {r["name"]: r for r in rows}


# ---------- 4. Telegram ----------
def telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        note("telegram", False, "TELEGRAM_BOT_TOKEN secret not set")
        return
    chat_file = DATA / "telegram_chat_id.txt"
    chat = chat_file.read_text().strip() if chat_file.exists() else ""
    if not chat:
        upd = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
        chats = [u["message"]["chat"]["id"] for u in upd.get("result", []) if "message" in u]
        if not chats:
            note("telegram", False, "no chat found: open your bot in Telegram, tap Start, then re-run")
            return
        chat = str(chats[-1])
        chat_file.write_text(chat)
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=30)
    note("telegram", r.ok, "" if r.ok else r.text)


def fmt(v, pct=False):
    if v is None:
        return "n/a"
    return f"{v:+.1f}%" if pct else f"{v:+,.0f}"


def main():
    results = {}
    for name, fn in [("nse_fii_dii", nse_fii_dii), ("nsdl_fpi", nsdl_fpi), ("indicators", indicators)]:
        try:
            results[name] = fn()
            note(name, True)
        except Exception as e:
            note(name, False, f"{type(e).__name__}: {e}")

    lines = [f"Weekly market flows ({NOW.strftime('%d %b %Y')})", ""]
    f = results.get("nsdl_fpi")
    if f:
        wk = weekly_fpi_change()
        current = f["month_no"] == NOW.month
        label = f"{f['month']} so far" if current else f"{f['month']} (latest month NSDL has published)"
        lines.append(f"Foreign investors (NSDL), {label}: Rs {fmt(f['equity_net_cr'])} cr equity")
        lines.append(f"  This week: Rs {fmt(wk)} cr" if wk is not None else "  Weekly change: available from next run")
    else:
        lines.append("NSDL foreign flows: not available this run")
    d = results.get("nse_fii_dii")
    if d:
        lines.append(f"NSE {d['date']}: FII {fmt(d.get('fii_net_cr'))} cr, DII {fmt(d.get('dii_net_cr'))} cr")
    i = results.get("indicators") or {}
    for key, label in [("nifty", "Nifty"), ("india_vix", "India VIX"), ("dollar_index", "Dollar index"),
                       ("usd_inr", "USD/INR"), ("us_10y_yield", "US 10y yield"), ("brent", "Brent crude")]:
        if key in i:
            r = i[key]
            lines.append(f"{label}: {r['close']:,.2f} ({fmt(r['chg_1w_pct'], True)} 1w, "
                         f"{fmt(r['chg_3m_pct'], True)} 3m){'' if r['above_200dma'] else ', below 200-DMA'}")
    if "INDA" in i and "EEM" in i and i["INDA"]["chg_3m_pct"] is not None and i["EEM"]["chg_3m_pct"] is not None:
        gap = i["INDA"]["chg_3m_pct"] - i["EEM"]["chg_3m_pct"]
        lines.append(f"India vs emerging markets (3m): {gap:+.1f} points")
    failed = [l["source"] for l in log if not l["ok"]]
    if failed:
        lines += ["", "Failed this run: " + ", ".join(failed)]
    lines += ["", "Source: NSDL, NSE, Yahoo Finance. Computed by script, no AI."]
    text = "\n".join(lines)
    print("\n" + text)
    telegram(text)

    p = DATA / "run_log.csv"
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["run_date", "source", "ok", "detail"])
        if new:
            w.writeheader()
        w.writerows(log)


if __name__ == "__main__":
    main()
