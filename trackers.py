"""Stdlib-only live trackers (run by watch.py every watcher cycle; results read by update.py and the page).
Each tracker is fail-safe: on error it keeps the last value, records the error, and marks itself stale.
State lives in data/trackers.json; values that change the model are written to data/events_state.json
only when confidence is high. Low-confidence findings are returned as review hits (webhook) and NOT applied.
"""
import json, re, html, datetime as dt, urllib.request, urllib.parse, xml.etree.ElementTree as X
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo
D = Path(__file__).parent / "data"; ET = ZoneInfo("America/New_York")
UA_SEC = "merger-dashboard tracker (contact: bfarrell20@gmail.com)"
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
# Nasdaq full closures and early-close (not "full") days, 2026-2027
HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
HALF_DAYS = {"2026-11-27", "2026-12-24", "2027-07-02", "2027-11-26"}

def now(): return dt.datetime.now(ET)
def stamp(): return now().strftime("%Y-%m-%d %H:%M ET")
def get(url, ua="Mozilla/5.0", timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")
def load(n, d): p = D / n; return json.loads(p.read_text()) if p.exists() else d
def text_of(url): return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", get(url, UA_SEC))))

def full_trading_day(d): return d.weekday() < 5 and str(d) not in HOLIDAYS and str(d) not in HALF_DAYS
def nth_full_day_after(release_date, timing, n=2):
    """'Second full trading day immediately following the release'. Pre-market release: the release day is the 1st."""
    d = release_date if timing == "pre" else release_date + dt.timedelta(days=1)
    k = 0
    while True:
        if full_trading_day(d):
            k += 1
            if k == n: return d
        d += dt.timedelta(days=1)

def parse_date(s, year_hint=2026):
    m = re.search(rf"({MONTHS})\.? (\d{{1,2}})(?:st|nd|rd|th)?(?:,? (20\d\d))?", s)
    if not m: return None
    mon = m.group(1)[:3]; y = int(m.group(3) or year_hint)
    try: return dt.datetime.strptime(f"{mon} {m.group(2)} {y}", "%b %d %Y").date()
    except ValueError: return None

def gnews(q, days=7):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    out = []
    for it in X.fromstring(get(url)).iter("item"):
        try: pd = parsedate_to_datetime(it.findtext("pubDate")).astimezone(ET)
        except Exception: continue
        if pd < now() - dt.timedelta(days=days): continue
        t = it.findtext("title") or ""; src = t.rsplit(" - ", 1)[-1] if " - " in t else "?"
        out.append({"title": t, "url": it.findtext("link"), "src": src, "at": pd})
    return out

def hit(id_, title, url, factor, review=False):
    return {"id": id_, "type": "review" if review else "tracker", "title": ("REVIEW NEEDED: " if review else "") + title,
            "url": url, "source": "tracker", "time_et": now().strftime("%Y-%m-%d %H:%M"), "factor": factor}

# ---------- 1. SpaceX Q3 results date -> 180-day 28% tranche ----------
def spcx_q3(prev, ev):
    hits, found = [], None
    sub = json.loads(get("https://data.sec.gov/submissions/CIK0001181412.json", UA_SEC))["filings"]["recent"]
    for i in range(len(sub["form"])):
        if sub["filingDate"][i] < "2026-10-01": break
        if not sub["form"][i].startswith("8-K"): continue
        items = set(filter(None, sub["items"][i].split(",")))
        url = f"https://www.sec.gov/Archives/edgar/data/1181412/{sub['accessionNumber'][i].replace('-', '')}/{sub['primaryDocument'][i]}"
        acc = dt.datetime.fromisoformat(sub["acceptanceDateTime"][i].replace("Z", "+00:00")).astimezone(ET)
        if "2.02" in items:   # results actually released
            t = text_of(url)
            if re.search(r"September 30, 2026|third quarter", t, re.I):
                mins = acc.hour * 60 + acc.minute
                found = {"release_date": str(acc.date()), "timing": "pre" if mins < 570 else "post", "kind": "released",
                         "source": f"8-K Item 2.02 accepted {acc:%Y-%m-%d %H:%M} ET", "url": url, "confidence": "high"}
                break
        else:                  # date announcement (7.01/8.01 press release)
            t = text_of(url)
            m = re.search(r"(third quarter|Q3)[^.]{0,200}?(results|earnings)[^.]{0,250}", t, re.I)
            if m:
                d = parse_date(m.group(0))
                if d:
                    tm = "post" if re.search(r"after (the )?(market|close)", m.group(0), re.I) else "pre" if re.search(r"before (the )?(market|open)", m.group(0), re.I) else "post"
                    found = {"release_date": str(d), "timing": tm, "kind": "announced", "source": f"8-K {','.join(items)} filed {sub['filingDate'][i]}",
                             "url": url, "confidence": "high", "evidence": m.group(0)[:240]}
                    break
    news = None
    if not found:   # news only -> low confidence, flag, don't apply
        for n in gnews('SpaceX "third quarter" OR Q3 results date', days=10):
            if re.search(r"SpaceX|SPCX", n["title"]) and re.search(r"third.quarter|Q3", n["title"], re.I) and re.search(r"results|earnings|report", n["title"], re.I):
                d = parse_date(n["title"])
                if d and d >= dt.date(2026, 10, 14):
                    news = {"release_date": str(d), "timing": "post", "source": n["src"], "url": n["url"], "confidence": "low", "title": n["title"]}; break
    st = {"status": "not announced", "checked": "EDGAR SPCX 8-Ks since Oct 1 + Google News"}
    if found:
        u = nth_full_day_after(dt.date.fromisoformat(found["release_date"]), found["timing"])
        st = {"status": "confirmed", **found, "unlock_date": str(u)}
        old = ev.get("spcx_q3", {})
        if old.get("unlock_date") != str(u):
            ev["spcx_q3"] = {"release_date": found["release_date"], "timing": found["timing"], "unlock_date": str(u), "url": found["url"], "confirmed": True}
            hits.append(hit(f"spcx_q3:{u}", f"SpaceX Q3 results {found['kind']} {found['release_date']} ({found['timing']}-market): 28% 180-day tranche (~1.3B sh) unlocks {u}; blackout now ON", found["url"], "lockup"))
    elif news:
        u = nth_full_day_after(dt.date.fromisoformat(news["release_date"]), "post")
        st = {"status": "unconfirmed (news only)", **news, "unlock_date_if_post_market": str(u)}
        hits.append(hit(f"spcx_q3news:{news['release_date']}", f"SpaceX Q3 date in news only: {news['title']} (if after the close, unlock {u}). Not applied until an SEC filing confirms.", news["url"], "lockup", review=True))
    return st, hits

# ---------- 2. Starship Flight 15 schedule + outcome ----------
SUCC = r"success(ful(ly)?)?|splash(es|ed)? ?down|caught|catch(es)? (the )?(ship|booster)|nails|aces|completes"
FAIL = r"explod|anomal|\blost\b|failure|fails|RUD|breaks up|destroyed"
PART = r"partial(ly)?|mixed|some objectives|but lost|despite"
def starship(prev, ev):
    hits = []; items = [n for n in gnews('"Starship" "Flight 15"', days=10) if re.search(r"Flight 15|15th", n["title"], re.I)]
    cur = dict(ev.get("starship", {})); status = cur.get("status", "scheduled")
    # schedule: dated headlines (targets/scheduled/NET/scrub->new date)
    dates = {}
    for n in items:
        t = n["title"]
        if re.search(r"target|schedul|set for|no earlier than|\bNET\b|launch (on|date)|delay|scrub|now (aim|target)", t, re.I):
            d = parse_date(t)
            if d and d >= now().date(): dates.setdefault(str(d), set()).add(n["src"])
    sched = None
    if dates:
        best = max(dates.items(), key=lambda kv: (len(kv[1]), kv[0]))
        sched = {"date": best[0], "sources": sorted(best[1])}
        if status in ("scheduled", "scrub") and best[0] != cur.get("next_net"):
            if len(best[1]) >= 2:
                cur.update({"next_net": best[0], "date_src": ", ".join(sorted(best[1]))})
                hits.append(hit(f"starship_date:{best[0]}", f"Starship Flight 15 date moved to {best[0]} (headlines from {', '.join(sorted(best[1]))}); launch-day/next-day blackout moved", None, "starship"))
            else:
                hits.append(hit(f"starship_date1:{best[0]}", f"Starship Flight 15 possibly {best[0]} (single source: {', '.join(best[1])}); not applied", None, "starship", review=True))
    # outcome (only recent, last 2 days)
    rec = [n for n in items if n["at"] > now() - dt.timedelta(days=2) and not re.search(r"\bwill\b|could|aims|preview|ahead of|what to expect|attempt to|static[- ]fire|test[- ]fire|fires all|ignit|engine test|prepar", n["title"], re.I)]
    votes = {"success": set(), "failure": set(), "partial": set()}; first = {}
    for n in rec:
        t = n["title"]; k = "partial" if re.search(PART, t, re.I) and re.search(FAIL + "|" + SUCC, t, re.I) else "failure" if re.search(FAIL, t, re.I) else "success" if re.search(SUCC, t, re.I) else None
        if k: votes[k].add(n["src"]); first[k] = min(first.get(k, n["at"]), n["at"])
    if status not in ("success", "failure"):
        strong = [k for k in ("success", "failure") if len(votes[k]) >= 2]
        if len(strong) == 1 and not votes["partial"] and not votes[{"success": "failure", "failure": "success"}[strong[0]]]:
            k = strong[0]; ld = str(first[k].date())
            cur.update({"status": k, "next_net": ld, "evidence": f"{len(votes[k])} sources: {', '.join(sorted(votes[k]))}"})
            hits.append(hit(f"starship:{k}", f"Starship Flight 15 {k.upper()} on {ld} ({len(votes[k])} sources agree); blackout rules applied", None, "starship"))
        elif any(votes.values()):
            summ = "; ".join(f"{k}: {', '.join(sorted(v))}" for k, v in votes.items() if v)
            hits.append(hit("starship_review:" + summ[:80], f"Starship Flight 15 outcome unclear ({summ}); not applied, please classify", None, "starship", review=True))
    if cur != ev.get("starship", {}): ev["starship"] = cur
    return {"status": cur.get("status", status), "next_net": cur.get("next_net"), "headline_date": sched, "outcome_votes": {k: sorted(v) for k, v in votes.items()}}, hits

# ---------- 3. Grain / FCC ECFS ----------
def grain(prev, ev):
    hits = []; rows = {}
    for q in ({"proceedings.name": "25-178"}, {"q": '"Grain" AND ("SpaceX" OR "Space Exploration")'}):
        r = json.loads(get("https://publicapi.fcc.gov/ecfs/filings?" + urllib.parse.urlencode({"api_key": "DEMO_KEY", "sort": "date_disseminated,DESC", "limit": 15, **q})))
        for x in r.get("filing", []):
            if x["date_disseminated"][:10] < "2026-10-01": continue
            blob = " ".join(p["name"] for p in x.get("filers", [])) + " " + " ".join(p["name"] + " " + (p.get("description") or "") for p in x.get("proceedings", []))
            if not any(p["name"] == "25-178" for p in x.get("proceedings", [])) and not re.search(r"Grain|SpaceX|Space Exploration|Starlink", blob, re.I): continue
            rows[x["id_submission"]] = {"id": x["id_submission"], "date": x["date_disseminated"][:10], "type": x["submissiontype"]["description"],
                                       "filers": [p["name"] for p in x.get("filers", [])], "proceedings": [p["name"] for p in x.get("proceedings", [])],
                                       "url": (x.get("documents") or [{}])[0].get("src")}
    seen = set(prev.get("seen_ids", [])); new = [r for r in rows.values() if r["id"] not in seen]
    approved = None
    for r in rows.values():
        if re.search(r"\border\b|grant|consent|approv", r["type"], re.I) and (any(re.search(r"FCC|Commission|Bureau", f) for f in r["filers"]) or r["type"].upper() in ("ORDER", "FCC")):
            approved = r
    if prev.get("seen_ids") is not None:
        for r in new: hits.append(hit(f"ecfs:{r['id']}", f"FCC ECFS new filing {r['date']}: {r['type']} by {', '.join(r['filers'])} ({', '.join(r['proceedings'])})", r["url"], "grain"))
    g = dict(ev.get("grain", {}))
    if approved and g.get("status") != "approved":
        g.update({"status": "approved", "approved_on": approved["date"], "evidence": approved}); ev["grain"] = g
        hits.append(hit(f"grain_approved:{approved['id']}", f"FCC action on Grain/SpaceX: {approved['type']} {approved['date']}; Grain factor set to APPROVED", approved["url"], "grain"))
    return {"status": g.get("status", "pending"), "filings": sorted(rows.values(), key=lambda r: r["date"], reverse=True)[:8], "seen_ids": sorted(set(rows) | seen)}, hits

# ---------- 4. Prices ----------
def prices(prev, ev):
    out = {}
    for t in ("TSLA", "SPCX"):
        m = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?interval=1m&range=1d"))["chart"]["result"][0]["meta"]
        out[t] = {"price": m["regularMarketPrice"], "prev_close": m.get("chartPreviousClose") or m.get("previousClose"),
                  "as_of": dt.datetime.fromtimestamp(m["regularMarketTime"], ET).strftime("%Y-%m-%d %H:%M ET")}
    return out, []

TRACKERS = {"spcx_q3": spcx_q3, "starship": starship, "grain": grain, "prices": prices}

def run(only=None, dry=False):
    tr = load("trackers.json", {}); ev = load("events_state.json", {}); hits = []; log = tr.get("log", [])
    for name, fn in TRACKERS.items():
        if only and name not in only: continue
        prev = tr.get(name, {})
        try:
            val, h = fn(prev.get("value", {}), ev)
            tr[name] = {"value": val, "ok": True, "as_of": stamp(), "last_ok": stamp(), "error": None}; hits += h
        except Exception as ex:
            tr[name] = {**prev, "ok": False, "as_of": stamp(), "error": f"{type(ex).__name__}: {ex}"[:200]}
            log.append(f"{stamp()} {name}: FAILED, kept last value ({ex})"[:240])
    tr["log"] = log[-30:]
    old = load("trackers.json", {})
    def sig(t, with_prices=False):   # tracked content only: drop as-of stamps (and prices unless asked)
        return json.dumps({k: ({kk: vv for kk, vv in v.items() if kk not in ("as_of", "last_ok")} if isinstance(v, dict) else v)
                           for k, v in t.items() if k != "prices_written" and (with_prices or k != "prices")}, sort_keys=True, default=str)
    changed = sig(tr) != sig(old)
    px_changed = sig({"p": tr.get("prices", {}).get("value")}, True) != sig({"p": old.get("prices", {}).get("value")}, True)
    last_px = old.get("prices_written"); tr["prices_written"] = last_px
    px_due = px_changed and (not last_px or now() - dt.datetime.fromisoformat(last_px) >= dt.timedelta(minutes=60))
    if px_due: tr["prices_written"] = now().isoformat(timespec="minutes")
    if not (changed or px_due):
        for k in tr:   # keep the stored file byte-identical so git sees no change
            if k in old: tr[k] = old[k]
    if not dry and (changed or px_due):
        (D / "trackers.json").write_text(json.dumps(tr, indent=1, default=str))
        (D / "events_state.json").write_text(json.dumps(ev, indent=1, default=str))
    return tr, hits

if __name__ == "__main__":
    import sys
    tr, h = run(dry="--dry" in sys.argv)
    print(json.dumps({k: (v if k == "log" else {kk: vv for kk, vv in v.items() if kk != "value"} | {"value": v.get("value")}) for k, v in tr.items()}, indent=1, default=str)[:5000]); print("HITS", json.dumps(h, indent=1, default=str))
