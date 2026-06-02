#!/usr/bin/env python3
"""
Edge Watch data builder.
OHLC: Tiingo primary -> Stooq fallback. (watchlist + 11 sector ETFs)
News+sentiment: Marketaux, one blurb per watchlist ticker.
Sector board: 11 SPDR sector ETFs, daily % change.
Market movers: Alpha Vantage TOP_GAINERS_LOSERS (1 call/day, optional).
Pure stdlib.
"""
import os, sys, io, csv, json, time, datetime as dt, urllib.parse, urllib.request, urllib.error

# ---- watchlist (source of truth) ----
TIER1 = ["DDOG", "CSCO", "NET", "CRWD", "DT", "SNOW", "MDB"]
TIER2 = ["NVDA", "CAT", "XOM"]
BENCH = ["SPY", "QQQ", "IWM"]
TICKERS = TIER1 + TIER2 + BENCH

# ---- 11 SPDR sector ETFs -> sector rotation board ----
SECTORS = [("XLK", "Technology"), ("XLF", "Financials"), ("XLV", "Health Care"),
           ("XLE", "Energy"), ("XLY", "Consumer Discretionary"), ("XLP", "Consumer Staples"),
           ("XLI", "Industrials"), ("XLB", "Materials"), ("XLRE", "Real Estate"),
           ("XLU", "Utilities"), ("XLC", "Communication Services")]
SECTOR_ETFS = [e for e, _ in SECTORS]
PRICE_TICKERS = TICKERS + SECTOR_ETFS          # OHLC fetched for all of these
BARS = 160

TIINGO = os.environ.get("TIINGO_TOKEN", "").strip()
MARKETAUX = os.environ.get("MARKETAUX_TOKEN", "").strip()
ALPHAVANTAGE = os.environ.get("ALPHAVANTAGE_KEY", "").strip()
UA = {"User-Agent": "edge-watch/1.0"}


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# ---- OHLC ----
def from_tiingo(t):
    if not TIINGO:
        raise ValueError("no tiingo token")
    start = (dt.date.today() - dt.timedelta(days=370)).isoformat()
    url = (f"https://api.tiingo.com/tiingo/daily/{urllib.parse.quote(t)}/prices"
           f"?startDate={start}&format=json&token={TIINGO}")
    status, body = fetch(url)
    if status != 200:
        raise ValueError(f"tiingo http {status}: {body[:120]}")
    data = json.loads(body)
    out = [{"d": x["date"][:10], "o": x["open"], "h": x["high"], "l": x["low"],
            "c": x["close"], "v": x.get("volume", 0)} for x in data]
    if len(out) < 30:
        raise ValueError(f"tiingo thin for {t}")
    return out[-BARS:]


def from_stooq(t):
    status, body = fetch(f"https://stooq.com/q/d/l/?s={t.lower()}.us&i=d")
    if status != 200:
        raise ValueError(f"stooq http {status}")
    rows = list(csv.DictReader(io.StringIO(body)))
    out = []
    for row in rows:
        try:
            out.append({"d": row["Date"], "o": float(row["Open"]), "h": float(row["High"]),
                        "l": float(row["Low"]), "c": float(row["Close"]),
                        "v": float(row.get("Volume") or 0)})
        except (ValueError, KeyError):
            continue
    if len(out) < 30:
        raise ValueError(f"stooq thin/empty for {t}")
    return out[-BARS:]


def get_ohlc(t):
    chain = [(from_tiingo, "tiingo"), (from_stooq, "stooq")] if TIINGO \
        else [(from_stooq, "stooq"), (from_tiingo, "tiingo")]
    for fn, name in chain:
        try:
            return fn(t), name
        except Exception as e:
            sys.stderr.write(f"[ohlc] {name} skip {t}: {e}\n")
    return [], "none"


# ---- sentiment ----
def sentiment_label(score):
    if score is None:
        return "n/a"
    if score <= -0.35:
        return "Bearish"
    if score <= -0.15:
        return "Somewhat-Bearish"
    if score < 0.15:
        return "Neutral"
    if score < 0.35:
        return "Somewhat-Bullish"
    return "Bullish"


def first_sentence(text, limit=240):
    text = (text or "").strip().replace("\n", " ")
    if not text:
        return ""
    for sep in (". ", " — ", " | "):
        if sep in text:
            text = text.split(sep)[0].strip()
            break
    return text[:limit].strip()


def get_news():
    res = {}
    print(f"[news] MARKETAUX token set: {bool(MARKETAUX)}")
    if not MARKETAUX:
        return res
    for t in TICKERS:                                   # news only for watchlist, not sector ETFs
        url = (f"https://api.marketaux.com/v1/news/all?symbols={t}"
               f"&filter_entities=true&language=en&limit=1&api_token={MARKETAUX}")
        try:
            status, body = fetch(url)
            data = json.loads(body)
        except Exception as e:
            print(f"[news] {t} error: {e}")
            continue
        if isinstance(data, dict) and data.get("error"):
            print(f"[news] {t} api error: {data.get('error')}")
            continue
        arts = data.get("data", []) if isinstance(data, dict) else []
        if not arts:
            continue
        art = arts[0]
        desc = art.get("description") or art.get("snippet", "")
        title = art.get("title", "")
        link = art.get("url", "")
        score, hl = None, ""
        for ent in art.get("entities", []):
            if (ent.get("symbol") or "").upper() == t:
                v = ent.get("sentiment_score")
                try:
                    score = float(v) if v is not None else None
                except (TypeError, ValueError):
                    score = None
                hls = ent.get("highlights") or []
                hl = hls[0].get("highlight", "") if hls else ""
                break
        sentence = first_sentence(hl) or first_sentence(desc) or first_sentence(title)
        res[t] = {"sentence": sentence, "sentiment": score,
                  "label": sentiment_label(score), "url": link}
        time.sleep(0.4)
    print(f"[news] got {len(res)} blurbs")
    return res


# ---- market movers (Alpha Vantage, optional) ----
def _num(s):
    try:
        return round(float(str(s).replace("%", "").replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return None


def get_movers():
    out = {"gainers": [], "losers": [], "active": [], "as_of": ""}
    print(f"[movers] ALPHAVANTAGE key set: {bool(ALPHAVANTAGE)}")
    if not ALPHAVANTAGE:
        return out
    url = f"https://www.alphavantage.co/query?function=TOP_GAINERS_LOSERS&apikey={ALPHAVANTAGE}"
    try:
        status, body = fetch(url)
        data = json.loads(body)
    except Exception as e:
        print(f"[movers] AV error: {e}")
        return out
    if "top_gainers" not in data:
        msg = data.get("Information") or data.get("Note") or data.get("Error Message") or str(data)[:160]
        print(f"[movers] AV no data: {msg}")
        return out

    def pack(arr):
        return [{"ticker": x.get("ticker"), "change_pct": _num(x.get("change_percentage")),
                 "price": _num(x.get("price"))} for x in (arr or [])[:5]]

    out["gainers"] = pack(data.get("top_gainers"))
    out["losers"] = pack(data.get("top_losers"))
    out["active"] = pack(data.get("most_actively_traded"))
    out["as_of"] = data.get("last_updated", "")
    print(f"[movers] AV ok: {len(out['gainers'])} gainers, {len(out['losers'])} losers")
    return out


def main():
    print(f"[init] TIINGO:{bool(TIINGO)} MARKETAUX:{bool(MARKETAUX)} ALPHAVANTAGE:{bool(ALPHAVANTAGE)}")
    news = get_news()
    movers = get_movers()
    tickers, sources = {}, {}
    for t in PRICE_TICKERS:
        ohlc, src = get_ohlc(t)
        sources[t] = src
        closes = [b["c"] for b in ohlc]
        last = closes[-1] if closes else None
        prev = closes[-2] if len(closes) >= 2 else None
        chg = ((last - prev) / prev * 100.0) if (last is not None and prev) else None
        tickers[t] = {
            "ohlc": ohlc, "last": last, "prev_close": prev,
            "change_pct": round(chg, 2) if chg is not None else None,
            "news": news.get(t, {"sentence": "", "sentiment": None, "label": "n/a", "url": ""}),
        }
        time.sleep(0.3)

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "tiers": {"tier1": TIER1, "tier2": TIER2, "bench": BENCH},
        "sectors": [{"etf": e, "name": n} for e, n in SECTORS],
        "movers": movers,
        "sources": sources, "tickers": tickers,
    }
    with open("data.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    ok = sum(1 for s in sources.values() if s != "none")
    print(f"wrote data.json :: {ok}/{len(PRICE_TICKERS)} ohlc ok, {len(news)} news, "
          f"{len(movers['gainers'])} movers, {out['generated_at']}")


if __name__ == "__main__":
    main()
