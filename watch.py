#!/usr/bin/env python3
"""Lightweight intraday watcher: new Tesla/SpaceX SEC filings + strict-filtered merger headlines.
Stdlib only (fast install-free runs). Does NOT recompute any odds.
Only on NEW relevant hits: appends to data/news.json and POSTs {items:[...]} to $WAKE_WEBHOOK_URL (if set).
Usage: python watch.py [--dry-run] [--bootstrap]
  --dry-run   fetch + filter + print payload; write nothing, POST nothing
  --bootstrap mark everything currently visible as seen, no alert (also automatic if seen file missing)
"""
import json, os, re, sys, urllib.parse, urllib.request, datetime as dt, xml.etree.ElementTree as X
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
D = Path(__file__).parent / "data"
SEEN = D / "seen.json"
UA = "merger-dashboard watcher (contact: dashboard@example.com)"
DRY = "--dry-run" in sys.argv

COMPANIES = {"TSLA": "0001318605", "SPCX": "0001181412"}
WATCH_FORMS = {"8-K", "8-K/A", "DEF 14A", "PRE 14A", "DEFA14A", "DEFM14A", "PREM14A", "PREC14A", "DEFC14A",
               "S-4", "S-4/A", "F-4", "425", "SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A",
               "SC TO-T", "SC TO-C", "SC 14D9", "SC 13E3", "DFAN14A", "8-A12B"}
# Headline must match a company term AND a deal term, from an allowlisted source, and not a noise term.
COMPANY_RE = re.compile(r"\b(tesla|spacex|spcx|tsla|musk)\b", re.I)
DEAL_RE = re.compile(r"\b(merger|merge[sd]?|merging|combin(e|es|ed|ing|ation)|acqui(re|res|red|sition)|takeover|buyout|"
                     r"all-stock|stock[- ]swap|exchange ratio|bear[- ]hug|offer letter|special committee|independent directors|"
                     r"definitive agreement|tender offer|fairness opinion|(annual|special) (shareholder )?meeting|proxy statement|"
                     r"go(es)? private|roll[- ]up)\b", re.I)
NEED_BOTH_RE = re.compile(r"spacex|spcx", re.I)  # deal headlines must mention SpaceX/SPCX, or Tesla + meeting/proxy terms
MEETING_RE = re.compile(r"(annual|special) (shareholder )?meeting|proxy", re.I)
NOISE_RE = re.compile(r"price prediction|stock forecast|should you buy|\bquote\b|top \d+|vs\.? .* stock|"
                      r"rumou?r mill|podcast|livestream|how to watch|opinion:|op-ed", re.I)
SOURCES = ["reuters", "bloomberg", "wall street journal", "wsj", "financial times", "cnbc", "barron", "associated press",
           "ap news", "new york times", "washington post", "axios", "the information", "semafor", "fortune",
           "business insider", "yahoo finance", "marketwatch", "electrek", "teslarati", "the verge", "techcrunch",
           "ars technica", "space.com", "spacenews", "spaceflight now", "investor's business daily", "seeking alpha",
           "benzinga", "cnn", "bbc", "the guardian", "forbes", "fox business", "puck", "insideevs", "notateslaapp"]
QUERIES = ['Tesla SpaceX merger', 'Tesla SpaceX deal', 'Tesla special committee SpaceX',
           'Tesla annual meeting 2026', 'SpaceX acquire Tesla', 'Tesla SpaceX exchange ratio']

def get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def edgar_hits():
    out = []
    for co, cik in COMPANIES.items():
        r = json.loads(get(f"https://data.sec.gov/submissions/CIK{cik}.json"))["filings"]["recent"]
        for i in range(min(40, len(r["form"]))):
            if r["form"][i] not in WATCH_FORMS: continue
            acc = r["accessionNumber"][i]
            t = r.get("acceptanceDateTime", [""] * 99)[i]
            try: tet = dt.datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d %H:%M")
            except Exception: tet = r["filingDate"][i]
            items = r["items"][i]
            out.append({"id": "edgar:" + acc, "type": "filing",
                        "title": f"{co} filed {r['form'][i]}" + (f" (items {items})" if items else ""),
                        "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{r['primaryDocument'][i]}",
                        "source": "SEC EDGAR", "time_et": tet, "factor": "sec"})
    return out

def relevant(title, source):
    t = title or ""
    if NOISE_RE.search(t) or not COMPANY_RE.search(t) or not DEAL_RE.search(t): return False
    if not (NEED_BOTH_RE.search(t) or (re.search(r"tesla", t, re.I) and MEETING_RE.search(t))): return False
    return any(s in (source or "").lower() for s in SOURCES)

def news_hits():
    out = []
    for q in QUERIES:
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:1d") + "&hl=en-US&gl=US&ceid=US:en"
        try: root = X.fromstring(get(url))
        except Exception as ex: print("news fetch failed:", q, ex, file=sys.stderr); continue
        for it in root.iter("item"):
            title, link, src = it.findtext("title") or "", it.findtext("link"), it.findtext("source") or ""
            title = re.sub(r"\s+-\s+" + re.escape(src) + r"$", "", title) if src else title
            if not relevant(title, src): continue
            try: tet = parsedate_to_datetime(it.findtext("pubDate")).astimezone(ET).strftime("%Y-%m-%d %H:%M")
            except Exception: tet = None
            norm = re.sub(r"[^a-z0-9 ]", "", title.lower())[:120]
            out.append({"id": "news:" + norm, "type": "headline", "title": title, "url": link, "source": src,
                        "time_et": tet, "factor": "sec" if MEETING_RE.search(title) else "news"})
    return out

# ---------------- prediction-market move watch ----------------
MH = D / "markets_history.json"
PM_URL = "https://polymarket.com/event/tesla-and-spacex-merger-officially-announced-by-june-30"
KS_URL = "https://kalshi.com/markets/kxcompanyactionmerger/company-merger/kxcompanyactionmerger-27"

def usable(bid, ask, last, vol, mw):
    if bid is not None and ask is not None and 0 < ask - bid <= mw["max_spread"]:
        return round((bid + ask) / 2, 4), "mid"
    if last is not None and (vol or 0) >= mw["min_volume_24h"]:
        return round(last, 4), "last"
    return None, "thin"

def fetch_contracts(mw):
    f = lambda x: float(x) if x not in (None, "") else None
    out = []
    ev = json.loads(get("https://gamma-api.polymarket.com/events?slug=tesla-and-spacex-merger-officially-announced-by-june-30"))[0]
    for m in ev["markets"]:
        if m.get("closed"): continue
        p, how = usable(f(m.get("bestBid")), f(m.get("bestAsk")), f(m.get("lastTradePrice")), f(m.get("volume24hr")), mw)
        out.append({"id": "polymarket:" + str(m["id"]), "name": "Polymarket: announced by " + m["groupItemTitle"], "p": p, "how": how,
                    "vol24": f(m.get("volume24hr")), "url": PM_URL})
    for m in json.loads(get("https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=KXCOMPANYACTIONMERGER-27&limit=50"))["markets"]:
        if m.get("status") not in ("active", "open"): continue
        p, how = usable(f(m.get("yes_bid_dollars")), f(m.get("yes_ask_dollars")), f(m.get("last_price_dollars")), f(m.get("volume_24h_fp")), mw)
        out.append({"id": "kalshi:" + m["ticker"], "name": "Kalshi: definitive agreement " + (m.get("yes_sub_title") or m["ticker"]),
                    "p": p, "how": how, "vol24": f(m.get("volume_24h_fp")), "url": KS_URL})
    return out

def market_moves(contracts, hist, mw, now):
    """Update rolling history in place; return market_move items. Pure function apart from `hist`."""
    items = []; ts = now.isoformat(timespec="minutes")
    cutoff = now - dt.timedelta(days=mw["keep_days"])
    for c in contracts:
        h = hist.setdefault(c["id"], {"name": c["name"], "points": [], "last_alert_p": None})
        pts = [x for x in h["points"] if dt.datetime.fromisoformat(x["t"]) >= cutoff]
        if c["p"] is not None and pts:
            target = now - dt.timedelta(hours=mw["window_hours"])
            older = [x for x in pts if dt.datetime.fromisoformat(x["t"]) <= target]
            ref = older[-1] if older else pts[0]          # ~24h ago, else oldest stored point
            move = (c["p"] - ref["p"]) * 100
            hrs = (now - dt.datetime.fromisoformat(ref["t"])).total_seconds() / 3600
            fresh = h["last_alert_p"] is None or abs(c["p"] - h["last_alert_p"]) * 100 >= mw["move_pp"]
            if abs(move) >= mw["move_pp"] and fresh:
                items.append({"type": "market_move", "title": f"{c['name']}: {ref['p']*100:.1f}% -> {c['p']*100:.1f}% ({move:+.1f} pts in {hrs:.1f}h)",
                              "contract": c["name"], "from": ref["p"], "to": c["p"], "window_hours": round(hrs, 1), "url": c["url"],
                              "source": c["id"].split(":")[0].capitalize(), "time_et": now.strftime("%Y-%m-%d %H:%M"), "quote": c["how"]})
                h["last_alert_p"] = c["p"]
        if c["p"] is not None:
            pts.append({"t": ts, "p": c["p"], "how": c["how"], "vol24": c["vol24"]})
        h["points"] = pts; h["name"] = c["name"]
    return items

def main():
    first = not SEEN.exists() or "--bootstrap" in sys.argv
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    news = json.loads((D / "news.json").read_text())
    seen |= {"news:" + re.sub(r"[^a-z0-9 ]", "", i["title"].lower())[:120] for d in news["days"] for i in d["items"]}
    hits, errors = [], []
    for f in (edgar_hits, news_hits):
        try: hits += f()
        except Exception as ex: errors.append(f"{f.__name__}: {ex}")
    mitems = []
    try:
        mw = json.loads((D / "config.json").read_text())["market_watch"]
        mhist = json.loads(MH.read_text()) if MH.exists() else {}
        mitems = market_moves(fetch_contracts(mw), mhist, mw, dt.datetime.now(ET))
        if not DRY: MH.write_text(json.dumps(mhist, indent=0))
    except Exception as ex: errors.append(f"markets: {ex}")
    uniq = {}
    for h in hits: uniq.setdefault(h["id"], h)
    new = [h for h in uniq.values() if h["id"] not in seen]
    print(json.dumps({"checked": len(uniq), "new": len(new), "market_moves": len(mitems), "bootstrap": first, "errors": errors}))
    MKEYS = ("title", "url", "source", "time_et", "type", "contract", "from", "to", "window_hours")
    payload = {"items": [{k: h[k] for k in ("title", "url", "source", "time_et", "type")} for h in new]
                        + [{k: m[k] for k in MKEYS} for m in mitems]}
    if DRY:
        print(json.dumps(payload, indent=1)); return
    seen |= set(uniq)
    SEEN.write_text(json.dumps(sorted(seen), indent=0))
    if first: new = []
    if not new and not mitems:
        return
    for m in mitems: m["factor"] = "markets"
    for h in new + mitems:  # append to news feed
        day_s = (h["time_et"] or dt.datetime.now(ET).strftime("%Y-%m-%d"))[:10]
        day = next((d for d in news["days"] if d["date"] == day_s), None)
        if not day:
            day = {"date": day_s, "odds_note": "", "items": []}; news["days"].append(day)
        day["items"].append({"time_et": (h["time_et"] or "")[11:] or None, "title": h["title"], "url": h["url"],
                             "source": h["source"], "factor": h["factor"], "effect": ("prediction-market move" if h["type"] == "market_move" else "intraday watcher hit") + "; odds not yet re-assessed", "auto": True, "watch": True})
    news["days"].sort(key=lambda d: d["date"], reverse=True)
    (D / "news.json").write_text(json.dumps(news, indent=1))
    hook = os.environ.get("WAKE_WEBHOOK_URL", "").strip()
    if not hook:
        print("WAKE_WEBHOOK_URL unset; skipping POST"); return
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    auth = os.environ.get("WAKE_WEBHOOK_AUTH", "").strip()
    if auth.lower().startswith("authorization:"):
        auth = auth.split(":", 1)[1].strip()
    if auth and not auth.lower().startswith("bearer "):
        auth = "Bearer " + auth
    if auth: headers["Authorization"] = auth   # full header value as stored, no prefix added
    req = urllib.request.Request(hook, data=json.dumps(payload).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r: print("webhook", r.status)
    except Exception as ex:
        print("webhook failed:", ex, file=sys.stderr)

if __name__ == "__main__":
    main()
