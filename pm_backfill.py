#!/usr/bin/env python3
"""Backfill prediction-market price history (free, no auth) into data/markets_backfill.json.
Polymarket: CLOB /prices-history (6h fidelity). Kalshi: /series/.../candlesticks (hourly, bid/ask mid at close, thinned to 4h)."""
import json, time, urllib.request, datetime as dt
from pathlib import Path
D = Path(__file__).parent / "data"; START = int(dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc).timestamp())
def get(u): return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "merger-dashboard"}), timeout=25).read())
def iso(ts): return dt.datetime.fromtimestamp(ts, dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-4))).isoformat(timespec="minutes")
def run():
    out, now = {}, int(time.time())
    ev = get("https://gamma-api.polymarket.com/events?slug=tesla-and-spacex-merger-officially-announced-by-june-30")[0]
    for m in ev["markets"]:
        if m.get("closed"): continue
        tok = json.loads(m["clobTokenIds"])[0]; pts = []; s = START
        while s < now:   # API caps the window length; walk in 14-day chunks
            e = min(now, s + 14 * 86400)
            pts += get(f"https://clob.polymarket.com/prices-history?market={tok}&startTs={s}&endTs={e}&fidelity=360").get("history", []); s = e
        out["polymarket:" + str(m["id"])] = {"name": "Polymarket: announced by " + m["groupItemTitle"], "src": "Polymarket CLOB prices-history",
                                             "points": [{"t": iso(p["t"]), "p": round(float(p["p"]), 4)} for p in sorted({p["t"]: p for p in pts}.values(), key=lambda p: p["t"])]}
    for m in get("https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=KXCOMPANYACTIONMERGER-27&limit=50")["markets"]:
        if m.get("status") not in ("active", "open"): continue
        c = get(f"https://api.elections.kalshi.com/trade-api/v2/series/KXCOMPANYACTIONMERGER/markets/{m['ticker']}/candlesticks?start_ts={START}&end_ts={now}&period_interval=60").get("candlesticks", [])
        pts, seen = [], {}
        for k in c:
            b, a = k["yes_bid"].get("close_dollars"), k["yes_ask"].get("close_dollars")
            if b and a and 0 < float(a) - float(b) <= 0.08: p = (float(b) + float(a)) / 2
            elif k["price"].get("close_dollars"): p = float(k["price"]["close_dollars"])
            else: continue
            seen[k["end_period_ts"] // (4 * 3600)] = {"t": iso(k["end_period_ts"]), "p": round(p, 4)}   # last candle in each 4h bucket
        pts = [seen[b] for b in sorted(seen)]
        out["kalshi:" + m["ticker"]] = {"name": "Kalshi: definitive agreement " + (m.get("yes_sub_title") or m["ticker"]), "src": "Kalshi candlesticks (hourly, 4h samples)", "points": pts}
    out = {k: v for k, v in out.items() if v["points"]}
    (D / "markets_backfill.json").write_text(json.dumps({"fetched": iso(now), "start": "2026-08-20", "series": out}, indent=0))
    return out
if __name__ == "__main__":
    for k, v in run().items(): print(k, len(v["points"]), v["points"][0]["t"] if v["points"] else None, v["points"][-1]["t"] if v["points"] else None)
