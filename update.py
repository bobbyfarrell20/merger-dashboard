#!/usr/bin/env python3
"""Refresh live data and recompute the merger-announcement model.
Free sources: Yahoo Finance (yfinance) prices/options, SEC EDGAR submissions,
Polymarket gamma API, Kalshi public API. Anything that fails keeps its last value.
Usage: python update.py [--offline]
"""
import json, math, sys, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
D = Path(__file__).parent / "data"
UA = {"User-Agent": "merger-dashboard research (contact: dashboard@example.com)"}
OFFLINE = "--offline" in sys.argv

def load(name, default=None):
    p = D / name
    return json.loads(p.read_text()) if p.exists() else default

def save(name, obj):
    (D / name).write_text(json.dumps(obj, indent=1))

def now_et():
    return dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")

# ---------------- live fetchers ----------------
def fetch_prices(live):
    import yfinance as yf
    out = {}
    for t in ["TSLA", "SPCX"]:
        tk = yf.Ticker(t); fi = tk.fast_info
        h = tk.history(period="5d").dropna(subset=["Close"])
        last, prevc = fi.get("lastPrice"), fi.get("previousClose")
        if last and last == last:
            out[t] = {"close": round(float(last), 2), "prev": round(float(prevc), 2), "date": str(dt.datetime.now(ET).date()) + " (last)"}
        else:
            out[t] = {"close": round(float(h["Close"].iloc[-1]), 2), "prev": round(float(h["Close"].iloc[-2]), 2), "date": str(h.index[-1].date())}
    live["prices"] = out; live["prices_as_of"] = now_et()

def fetch_options(live):
    import yfinance as yf
    t = yf.Ticker("TSLA"); spot = live["prices"]["TSLA"]["close"]
    rows = []
    for e in t.options:
        try:
            ch = t.option_chain(e)
            c, p = ch.calls, ch.puts
            c = c[(c.volume.fillna(0) > 0) | (c.openInterest.fillna(0) > 0)]
            atm = c.iloc[(c.strike - spot).abs().argsort()[:2]]
            iv = float(atm.impliedVolatility.mean())
            rows.append({"expiry": e, "atm_iv": round(iv * 100, 1),
                         "call_vol": int(ch.calls.volume.fillna(0).sum()),
                         "put_vol": int(p.volume.fillna(0).sum()),
                         "call_oi": int(ch.calls.openInterest.fillna(0).sum())})
        except Exception as ex:
            rows.append({"expiry": e, "error": str(ex)[:80]})
    live["options"] = rows; live["options_as_of"] = now_et()

def fetch_edgar(live, watch):
    import requests
    out = {}
    for name, cik in [("TSLA", "0001318605"), ("SPCX", "0001181412")]:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=UA, timeout=20).json()["filings"]["recent"]
        fl = []
        for i in range(min(25, len(r["form"]))):
            fl.append({"date": r["filingDate"][i], "form": r["form"][i], "items": r["items"][i],
                       "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{r['accessionNumber'][i].replace('-','')}/{r['primaryDocument'][i]}",
                       "watch": r["form"][i] in watch})
        out[name] = fl
    live["edgar"] = out; live["edgar_as_of"] = now_et()

NEWS_Q = '(Tesla SpaceX merger) OR (Tesla annual meeting proxy) OR (SpaceX SPCX lockup) OR (Tesla Roadster) OR (Starship launch) OR (Tesla 8-K)'
FACTOR_KW = [("sec", ["8-k","filing","sec ","proxy","annual meeting","committee","s-4","vote"]),
             ("markets", ["polymarket","kalshi","odds"]), ("options", ["options","implied volatility","call volume"]),
             ("calendar", ["launch","starship","lockup","lock-up","unlock","earnings","event","roadster","rebalance"]),
             ("prices", ["stock","shares rise","shares fall","valuation","market cap"])]

def fetch_news(news):
    """Append Google News RSS headlines (free) to news.json, grouped by ET day, deduped by URL/title."""
    import requests, urllib.parse, xml.etree.ElementTree as X
    from email.utils import parsedate_to_datetime
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(NEWS_Q + " when:2d") + "&hl=en-US&gl=US&ceid=US:en"
    root = X.fromstring(requests.get(url, timeout=20, headers=UA).content)
    seen = {i["title"].lower() for d in news["days"] for i in d["items"]} | {i["url"] for d in news["days"] for i in d["items"]}
    added = 0
    for it in root.iter("item"):
        title, link = it.findtext("title"), it.findtext("link")
        if not title or title.lower() in seen or link in seen: continue
        t = parsedate_to_datetime(it.findtext("pubDate")).astimezone(ET)
        low = title.lower()
        if "quote" in low or "forecast -" in low: continue
        if not any(k in low for k in ["tesla","spacex","spcx","musk","starship"]): continue
        factor = next((f for f, kws in FACTOR_KW if any(k in low for k in kws)), "news")
        day = next((d for d in news["days"] if d["date"] == str(t.date())), None)
        if not day:
            day = {"date": str(t.date()), "odds_note": "", "items": []}; news["days"].append(day)
        day["items"].append({"time_et": t.strftime("%H:%M"), "title": title, "url": link, "source": (it.findtext("source") or "Google News"), "factor": factor, "effect": "auto-tagged; review", "auto": True})
        seen.add(title.lower()); added += 1
    news["days"].sort(key=lambda d: d["date"], reverse=True)
    return added

# ---------------- model ----------------
WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
def P(s): return dt.date.fromisoformat(s)
def logit(p): return math.log(p / (1 - p))
def sig(x): return 1 / (1 + math.exp(-x))

def inputs_of(cfg, jud):
    keys = ["window", "p_any_by_end", "a_share", "signals", "urgency", "weekday_mult", "blackouts", "post_event_boost"]
    return {k: cfg[k] for k in keys}

def compute(cfg, today):
    """Competing-risks daily model. Returns dict with days, weeks, totals (all measured from `today`)."""
    start = max(P(cfg["window"]["start"]), today); end = P(cfg["window"]["end"])
    lp, la = logit(cfg["p_any_by_end"]["base"]), logit(cfg["a_share"]["base"])
    for s in cfg["signals"]:
        if s["active"]:
            if s["target"] == "p_any": lp += s["logodds"]
            else: la += s["logodds"]
    p_end, a_sh = sig(lp), sig(la)
    days = []
    d = start
    while d <= end:
        w = cfg["weekday_mult"][WD[d.weekday()]]; why = [f"{WD[d.weekday()]} x{w}"]
        for u in cfg["urgency"]:
            if P(u["from"]) <= d <= P(u["to"]): w *= u["mult"]; why.append(f"urgency x{u['mult']}")
        wa = wb = w; ev = []
        for b in cfg["blackouts"] + cfg["post_event_boost"]:
            if b["date"] == str(d):
                ev.append(b["label"])
                if b["applies"] in ("A", "both"): wa *= b["mult"]
                if b["applies"] in ("B", "both"): wb *= b["mult"]
                why.append(f"{b['label']} x{b['mult']}" + ("" if b["applies"] == "both" else f" ({b['applies']} only)"))
        days.append({"date": str(d), "wa": wa, "wb": wb, "why": why, "events": ev})
        d += dt.timedelta(days=1)
    tA, tB = p_end * a_sh, p_end * (1 - a_sh)
    def run(kA, kB):
        S = 1.0; cA = cB = 0.0
        for x in days:
            hA, hB = min(kA * x["wa"], .95), min(kB * x["wb"], .95)
            if hA + hB > .98: hA, hB = hA * .98 / (hA + hB), hB * .98 / (hA + hB)
            x["pA"], x["pB"] = S * hA, S * hB; cA += x["pA"]; cB += x["pB"]; S *= 1 - hA - hB
            x["cumA"], x["cumB"], x["none"] = cA, cB, S
        return cA, cB
    kA = kB = 0.01
    for _ in range(200):  # alternating calibration of the two hazards
        cA, cB = run(kA, kB)
        kA *= (tA / cA) ** 0.7 if cA > 0 else 2; kB *= (tB / cB) ** 0.7 if cB > 0 else 2
    run(kA, kB)
    # weeks: Mon-Sun (Sunday folded into next Monday already by weight 0)
    weeks = {}
    for x in days:
        dd = P(x["date"]); mon = dd - dt.timedelta(days=dd.weekday())
        w = weeks.setdefault(str(mon), {"monday": str(mon), "friday": str(mon + dt.timedelta(days=4)), "first_day": x["date"],
                                        "A": 0, "B": 0, "monA": 0, "monB": 0, "tueA": 0, "tueB": 0})
        w["A"] += x["pA"]; w["B"] += x["pB"]; w["cumA"], w["cumB"], w["none"] = x["cumA"], x["cumB"], x["none"]
        if dd.weekday() == 0: w["monA"] += x["pA"]; w["monB"] += x["pB"]
        if dd.weekday() == 1: w["tueA"] += x["pA"]; w["tueB"] += x["pB"]
    W = list(weeks.values())
    for w in W:
        w["either"] = w["A"] + w["B"]; w["mon"] = w["monA"] + w["monB"]; w["tue"] = w["tueA"] + w["tueB"]
        w["cum"] = w["cumA"] + w["cumB"]
    best = max(W, key=lambda w: w["either"])["monday"] if W else None
    return {"days": days, "weeks": W, "best_week": best, "p_end": p_end, "a_share": a_sh,
            "totals": {"A": days[-1]["cumA"], "B": days[-1]["cumB"], "none": days[-1]["none"]} if days else {}}

def metrics(m):
    out = {f"end_{k}": v for k, v in m["totals"].items()}
    for w in m["weeks"]:
        out[f"wk{w['monday']}_either"] = w["either"]; out[f"wk{w['monday']}_A"] = w["A"]; out[f"wk{w['monday']}_B"] = w["B"]
    return out

def drivers_diff(old, new):
    out = []
    os_ = {x["id"]: x for x in old["signals"]}
    for x in new["signals"]:
        o = os_.get(x["id"])
        if not o: out.append(f"new signal: {x['label']}")
        elif o["active"] != x["active"]: out.append(f"signal {'ON' if x['active'] else 'OFF'}: {x['label']} ({x['logodds']:+})")
        elif o["logodds"] != x["logodds"]: out.append(f"signal weight changed: {x['label']}")
    for k in ("p_any_by_end", "a_share"):
        if old[k]["base"] != new[k]["base"]: out.append(f"{k} prior {old[k]['base']} -> {new[k]['base']}")
    for k in ("urgency", "weekday_mult", "blackouts", "post_event_boost", "window"):
        if old[k] != new[k]: out.append(f"calendar/config changed: {k}")
    return out

def main():
    cfg, jud = load("config.json"), load("judgment.json")
    live = load("live.json", {})
    news = load("news.json", {"days": []})
    if not OFFLINE:
        for f in (fetch_prices, fetch_options, lambda l: fetch_edgar(l, cfg["alerts"]["watch_forms"])):
            try: f(live)
            except Exception as ex: live.setdefault("errors", []).append(f"{now_et()} {getattr(f, '__name__', 'edgar')}: {ex}")
        try: fetch_news(news)
        except Exception as ex: live.setdefault("errors", []).append(f"{now_et()} news: {ex}")
        live["errors"] = live.get("errors", [])[-10:]
        live.pop("markets", None); live.pop("markets_as_of", None)
        save("live.json", live)
    today = dt.datetime.now(ET).date()
    m = compute(cfg, today)
    exps = sorted(o["expiry"] for o in live.get("options", []) if "expiry" in o)
    ivs = {o["expiry"]: o.get("atm_iv") for o in live.get("options", []) if "expiry" in o}
    for w in m["weeks"]:
        fri = w["friday"]  # Friday weekly that covers a Mon/Tue announcement that week
        e = fri if fri in ivs else next((x for x in exps if w["first_day"] <= x <= fri), None)
        w["expiry"] = fri; w["expiry_listed"] = fri in ivs; w["expiry_iv"] = ivs.get(fri)
    # history + news/time split
    hist = load("history.json", [])
    hist = [h for h in hist if h.get("v") == 2]           # old-format snapshots dropped on redesign
    inp = inputs_of(cfg, jud); met = metrics(m)
    prev = next((h for h in reversed(hist) if h["date"] < str(today)), None)
    split, drivers = {}, []
    if prev:
        cf = metrics(compute({**cfg, **prev["inputs"]}, today))
        drivers = drivers_diff(prev["inputs"], inp)
        for k, v in met.items():
            if k in prev["metrics"]:
                split[k] = {"total": round((v - prev["metrics"][k]) * 100, 2), "time": round((cf[k] - prev["metrics"][k]) * 100, 2),
                            "news": round((v - cf[k]) * 100, 2)}
    hist.append({"v": 2, "date": str(today), "as_of": now_et(), "metrics": met, "inputs": inp})
    hist = [h for i, h in enumerate(hist) if h["date"] != str(today) or i == len(hist) - 1]
    save("history.json", hist[-200:])
    news_moved = any(abs(v["news"]) > 0.005 for v in split.values())
    day = next((d for d in news["days"] if d["date"] == str(today)), None)
    if not day:
        day = {"date": str(today), "odds_note": "", "items": []}; news["days"].insert(0, day)
    if prev:
        e = lambda k: split.get(k, {"total": 0, "time": 0, "news": 0})
        day["odds_note"] = ("By Dec 14: with merger {:+.1f} pts (time {:+.1f}, news {:+.1f}); routine {:+.1f} (time {:+.1f}, news {:+.1f}); nothing {:+.1f}. "
                            .format(e("end_A")["total"], e("end_A")["time"], e("end_A")["news"], e("end_B")["total"], e("end_B")["time"], e("end_B")["news"], e("end_none")["total"])
                            + ("Why: " + "; ".join(drivers) + "." if drivers else "No new information, so this is just time passing."))
        day["split"] = {k: split[k] for k in ("end_A", "end_B", "end_none") if k in split}
    news["days"].sort(key=lambda d: d["date"], reverse=True); save("news.json", news)
    alerts = []
    if news_moved:
        alerts.append({"type": "news_driven_change", "drivers": drivers, "headlines": [i["title"] for i in day["items"]],
                       "text": "News-driven change: " + ", ".join(f"{k} {split[k]['news']:+.1f}pts" for k in ("end_A", "end_B", "end_none") if k in split)})
    for co, fl in live.get("edgar", {}).items():
        for f in fl:
            if f["watch"] and f["date"] >= str(today - dt.timedelta(days=1)):
                alerts.append({"type": "filing", "text": f"{co} filed {f['form']} on {f['date']}", "url": f["url"]})
    model = {"generated": now_et(), "today": str(today), "window": cfg["window"], "weeks": m["weeks"], "best_week": m["best_week"],
             "totals": m["totals"], "p_end": m["p_end"], "a_share": m["a_share"], "split": split, "drivers": drivers, "alerts": alerts,
             "prev_as_of": prev["as_of"] if prev else None,
             "daily": [{k: x[k] for k in ("date", "cumA", "cumB", "none", "pA", "pB", "why", "events")} for x in m["days"]]}
    save("model.json", model)
    print(json.dumps({"totals": m["totals"], "best_week": m["best_week"], "a_share": m["a_share"],
                      "weeks": [(w["monday"], round(w["A"]*100, 1), round(w["B"]*100, 1), round(w["either"]*100, 1), round(w["mon"]*100, 1), round(w["tue"]*100, 1), round(w["cum"]*100, 1), w["expiry"]) for w in m["weeks"]],
                      "alerts": alerts}, indent=1))

if __name__ == "__main__":
    main()
