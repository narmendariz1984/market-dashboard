#!/usr/bin/env python3
"""
Edge Watch data builder.
Pulls daily OHLC (Stooq, no key -> Tiingo fallback if TIINGO_TOKEN set)
and a one-sentence news+sentiment blurb per ticker (Marketaux, if MARKETAUX_TOKEN set).
Writes data.json. Pure stdlib, no pip installs needed.
"""
import os, sys, io, csv, json, time, datetime as dt, urllib.parse, urllib.request

# ---- watchlist (edit here — this file is the source of truth) ----
TIER1 = ["DDOG", "CSCO", "NET", "CRWD", "DT", "SNOW", "MDB"]   # your domain edge
TIER2 = ["NVDA", "CAT", "XOM"]                                  # thematic, no edge
BENCH = ["SPY", "QQQ", "IWM"]                                   # benchmarks
TICKERS = TIER1 + TIER2 + BENCH
BARS = 160  # daily bars to keep (enough for SMA50 / MACD with buffer)

TIINGO = os.environ.get("TIINGO_TOKEN", "").strip()
MARKETAUX = os.environ.get("MARKETAUX_TOKEN", "").strip()
UA = {"User-Agent": "edge-watch/1.0"}


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---- OHLC: Stooq primary (no key), Tiingo fallback (keyed) ----
def stooq_symbol(t):
    # US equities + ETFs use the ".us" suffix on Stooq; indices alt: ^SPX, ^NDX
    return t.lower() + ".us"


def from_stooq(t):
    url = f"https://stooq.com/q/d/l/?s={stooq_symbol(t)}&i=d"
    rows = list(csv.DictReader(io.StringIO(http_get(url))))  # Date,Open,High,Low,Close,Volume
    out = []
    for row in rows:
        try:
            out.append({"d": row["Date"], "o": float(row["Open"]), "h": float(row["High"]),
                        "l": float(row["Low"]), "c": float(row["Close"]),
                        "v": float(row.get("Volume") or 0)})
        except (ValueError, KeyError):
            continue  # skips 'N/D' rows
    if len(out) < 30:
        raise ValueError(f"stooq thin/empty for {t}")
    return out[-BARS:]


def from_tiingo(t):
    if not TIINGO:
        raise ValueError("no tiingo token")
    start = (dt.date.today() - dt.timedelta(days=370)).isoformat()
    url = (f"https://api.tiingo.com/tiingo/daily/{urllib.parse.quote(t)}/prices"
           f"?startDate={start}&format=json&token={TIINGO}")
    data = json.loads(http_get(url))
    out = [{"d": x["date"][:10], "o": x["open"], "h": x["high"], "l": x["low"],
            "c": x["close"], "v": x.get("volume", 0)} for x in data]
    if len(out) < 30:
        raise ValueError(f"tiingo thin for {t}")
    return out[-BARS:]


def get_ohlc(t):
    for fn, name in ((from_stooq, "stooq"), (from_tiingo, "tiingo")):
        try:
            return fn(t), name
        except Exception as e:
            sys.stderr.write(f"[ohlc] {name} failed {t}: {e}\n")
    return [], "none"


# ---- news + sentiment: Marketaux, one call for all tickers ----
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
    if not MARKETAUX:
        return res
    url = ("https://api.marketaux.com/v1/news/all?"
           f"symbols={','.join(TICKERS)}&filter_entities=true&language=en&limit=3"
           f"&api_token={MARKETAUX}")
    try:
        payload = json.loads(http_get(url))
    except Exception as e:
        sys.stderr.write(f"[news] marketaux failed: {e}\n")
        return res
    for art in payload.get("data", []):
        title, desc = art.get("title", ""), (art.get("description") or art.get("snippet", ""))
        link = art.get("url", "")
        for ent in art.get("entities", []):
            sym = (ent.get("symbol") or "").upper()
            if sym not in TICKERS or sym in res:
                continue
            hls = ent.get("highlights") or []
            hl = hls[0].get("highlight", "") if hls else ""
            sentence = first_sentence(hl) or first_sentence(desc) or first_sentence(title)
            try:
                score = float(ent.get("sentiment_score")) if ent.get("sentiment_score") is not None else None
            except (TypeError, ValueError):
                score = None
            res[sym] = {"sentence": sentence, "sentiment": score,
                        "label": sentiment_label(score), "url": link}
    return res


def main():
    news = get_news()
    tickers, sources = {}, {}
    for t in TICKERS:
        ohlc, src = get_ohlc(t)
        sources[t] = src
        closes = [b["c"] for b in ohlc]
        last = closes[-1] if closes else None
        prev = closes[-2] if len(closes) >= 2 else None
        chg = ((last - prev) / prev * 100.0) if (last is not None and prev) else None
        tickers[t] = {
            "ohlc": ohlc,
            "last": last,
            "prev_close": prev,
            "change_pct": round(chg, 2) if chg is not None else None,
            "news": news.get(t, {"sentence": "", "sentiment": None, "label": "n/a", "url": ""}),
        }
        time.sleep(0.6)  # be gentle to Stooq

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "tiers": {"tier1": TIER1, "tier2": TIER2, "bench": BENCH},
        "sources": sources,
        "tickers": tickers,
    }
    with open("data.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))

    ok = sum(1 for s in sources.values() if s != "none")
    print(f"wrote data.json :: {ok}/{len(TICKERS)} ohlc ok, {len(news)} news blurbs, {out['generated_at']}")


if __name__ == "__main__":
    main()
