#!/usr/bin/env python3
"""Self-check: data-guard replays (incl. the Nov 13, 2026 $358.22 / IV 13.1 bad pull) and model consistency.
Run: python selfcheck.py [--quick]   (exit code 1 on any failure; writes nothing; --quick skips the git-history replays)"""
import copy, json, sys, datetime as dt
from zoneinfo import ZoneInfo
import dataguard as G
ET = ZoneInfo("America/New_York"); T = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=ET)
FAILS = []; N = [0]
def check(cond, label, detail=""):
    N[0] += 1; print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not cond: FAILS.append(label)

# Last good state as of the Nov 13 08:24 ET morning run (committed live.json, verified vs. actual closes)
GOOD_LIVE = {"prices": {"TSLA": {"close": 397.45, "prev": 390.84, "date": "2026-11-12 16:00 ET", "market_time": "2026-11-12 16:00 ET"},
                        "SPCX": {"close": 139.94, "prev": 139.75, "date": "2026-11-12 16:00 ET", "market_time": "2026-11-12 16:00 ET"}},
             "options": [{"expiry": "2026-11-13", "atm_iv": 70.1}, {"expiry": "2026-11-16", "atm_iv": 52.4}, {"expiry": "2026-11-18", "atm_iv": 52.8},
                         {"expiry": "2026-11-20", "atm_iv": 54.3}, {"expiry": "2026-12-18", "atm_iv": 50.8}]}

def seeded_live():
    import update
    live = copy.deepcopy(GOOD_LIVE)
    # first guarded run (the good Nov 12 close pulled at 08:24 Nov 13) seeds last-good + recorded closes
    update.apply_prices(live, {t: {"price": v["close"], "prev": v["prev"], "time": T("2026-11-12T16:00")} for t, v in GOOD_LIVE["prices"].items()}, {}, now=T("2026-11-13T08:24"))
    update.apply_iv(live, copy.deepcopy(GOOD_LIVE["options"]), {}, now=T("2026-11-13T08:24"))
    live.pop("guard_run", None)
    return live

def guard_tests():
    import update, trackers
    print("== Data guard: replay of the Nov 13, 2026 13:26 ET bad pull")
    live = seeded_live()
    check(live["guard"]["closes"]["TSLA"].get("2026-11-12") == 397.45 and live["guard"]["closes"]["TSLA"].get("2026-11-11") == 390.84, "seed: recorded TSLA closes Nov 11/12", str(live["guard"]["closes"]["TSLA"]))
    bad = {"TSLA": {"price": 390.83, "prev": 358.22, "time": T("2026-11-13T13:26")}, "SPCX": {"price": 139.99, "prev": 134.27, "time": T("2026-11-13T13:26")}}
    W = update.apply_prices(live, bad, {}, now=T("2026-11-13T13:27"))
    p = live["prices"]["TSLA"]
    check(p["prev"] == 397.45, "TSLA bad previous close $358.22 replaced by last good $397.45", str(p))
    check(p["close"] == 390.83, "TSLA intraday price $390.83 (-1.7%) still accepted")
    check("prev" in p.get("held", []), "TSLA marked held:prev")
    check(live["prices"]["SPCX"]["prev"] == 139.94, "SPCX bad previous close $134.27 replaced by $139.94", str(live["prices"]["SPCX"]))
    check(any(w["review"] and "358.22" in w["msg"] for w in W), "warning logged with review flag", next((w["msg"] for w in W if "358.22" in w["msg"]), ""))
    hits = G.review_hits(W, T("2026-11-13T13:27"))
    check(hits and all(h["title"].startswith("REVIEW NEEDED: data guard") for h in hits) and len({h["id"] for h in hits}) == len(hits), "REVIEW NEEDED hits, one id per field per day", "; ".join(h["id"] for h in hits))
    # IV: 0-DTE 13.1 not shown; a 13.1 on a real expiry is rejected and the last good kept; 0.0 off-hours kept quietly
    rows = [{"expiry": "2026-11-13", "atm_iv": 13.1}, {"expiry": "2026-11-16", "atm_iv": 30.7}, {"expiry": "2026-11-18", "atm_iv": 40.9},
            {"expiry": "2026-11-20", "atm_iv": 13.1}, {"expiry": "2026-12-18", "atm_iv": 46.4}]
    W = update.apply_iv(live, rows, {}, now=T("2026-11-13T13:27")); iv = {r["expiry"]: r["atm_iv"] for r in live["options"]}
    check(iv["2026-11-13"] is None, "0-DTE IV 13.1 not shown (unreliable intraday)")
    check(iv["2026-11-20"] == 54.3, "Nov 20 IV 13.1 rejected (below 15 floor), last good 54.3 kept", str(iv["2026-11-20"]))
    check(iv["2026-11-16"] == 52.4, "Nov 16 IV 30.7 (-41% vs 52.4) held for review", str(iv["2026-11-16"]))
    check(iv["2026-11-18"] == 40.9 and iv["2026-12-18"] == 46.4, "normal IV moves (-23%, -9%) accepted")
    check(any(w["review"] and "13.1" in w["msg"] for w in W), "IV warning logged with review flag")
    rows = [{"expiry": "2026-11-20", "atm_iv": 0.0}, {"expiry": "2026-12-18", "atm_iv": 0.0}]
    W = update.apply_iv(live, rows, {}, now=T("2026-11-13T22:00")); iv = {r["expiry"]: r["atm_iv"] for r in live["options"]}
    check(iv["2026-11-20"] == 54.3 and iv["2026-12-18"] == 46.4 and not W, "off-hours IV 0.0 -> last good, no warning")
    # confirm-on-repeat: a real +30% gap is held twice, accepted on the 3rd consecutive run
    lv = seeded_live(); seq = []
    for i, m in enumerate(("2026-11-13T10:00", "2026-11-13T10:15", "2026-11-13T10:30")):
        update.apply_prices(lv, {"TSLA": {"price": 520.0, "prev": 397.45, "time": T(m)}}, {}, now=T(m)); seq.append(lv["prices"]["TSLA"]["close"])
    check(seq == [397.45, 397.45, 520.0], "genuine +31% move held twice, accepted on 3rd run", str(seq))
    # bounds, time going backwards, staleness
    lv = seeded_live(); update.apply_prices(lv, {"TSLA": {"price": 0.0, "prev": 397.45, "time": T("2026-11-13T11:00")}}, {}, now=T("2026-11-13T11:00"))
    check(lv["prices"]["TSLA"]["close"] == 397.45, "zero price rejected -> last good")
    lv = seeded_live(); update.apply_prices(lv, {"TSLA": {"price": 395.0, "prev": 390.84, "time": T("2026-11-11T16:00")}}, {}, now=T("2026-11-13T11:00"))
    check(lv["prices"]["TSLA"]["close"] == 397.45, "older-than-last-good quote rejected")
    lv = seeded_live(); W = update.apply_prices(lv, {"TSLA": {"price": 392.0, "prev": 397.45, "time": T("2026-11-13T10:00")}}, {}, now=T("2026-11-13T11:30"))
    check(lv["prices"]["TSLA"].get("stale") and any(w["key"] == "TSLA:stale" and not w["review"] for w in W), "90-min-old quote in market hours flagged stale (info, no review)")
    # tracker path (watch.py) on the same bad pull
    prev = {"TSLA": {"price": 397.45, "prev_close": 390.84, "as_of": "2026-11-12 16:00 ET"}, "SPCX": {"price": 139.94, "prev_close": 139.75, "as_of": "2026-11-12 16:00 ET"}}
    v, _ = trackers.guarded_prices({t: {"price": x["price"], "prev": x["prev_close"], "time": T("2026-11-12T16:00")} for t, x in prev.items()}, prev, n=T("2026-11-13T08:00"), cfg={})
    v2, hits = trackers.guarded_prices(bad, v, n=T("2026-11-13T13:27"), cfg={})
    check(v2["TSLA"]["prev_close"] == 397.45 and v2["TSLA"]["price"] == 390.83 and hits, "price tracker: same replay -> prev held, REVIEW NEEDED hit", hits[0]["title"][:90] if hits else "")
    print("== Prediction-market quote checks")
    mw = {"max_spread": 0.06, "min_volume_24h": 50}
    check(G.clean_quote(0.28, 0.29, 0.28, 1000, mw)[:2] == (0.285, "mid"), "tight book -> mid")
    check(G.clean_quote(0.40, 0.30, 0.35, 1000, mw)[0] is None or G.clean_quote(0.40, 0.30, 0.35, 1000, mw)[1] == "last", "crossed book -> no mid")
    check("crossed" in (G.clean_quote(0.40, 0.30, 0.35, 1000, mw)[2] or ""), "crossed book reported")
    check(G.clean_quote(0.0, 1.0, None, 0, mw)[0] is None, "empty book, no trades -> no price")
    check(G.clean_quote(0.0, 0.01, None, 90, mw)[:2] == (0.005, "mid"), "one-cent one-sided low book still usable (as before)")
    check(G.clean_quote(0.30, 0.50, 0.90, 5000, mw)[0] is None, "last trade far outside a wide book -> not used")
    check(G.clean_quote(1.2, 1.3, None, 100, mw)[1] == "bad", "price outside [0,1] -> bad")
    now = T("2026-11-15T03:00")
    check(G.quote_staleness("2026-11-14T10:00-05:00", "2026-11-14T20:28-05:00", now) is not None, "quote 17h old -> stale")
    check(G.quote_staleness("2026-11-14T20:28-05:00", "2026-11-14T20:28-05:00", now) is None, "quote 6.5h old on a weekend -> ok")
    check(G.quote_staleness("2026-11-14T15:00-05:00", "2026-11-14T20:28-05:00", now) is not None, "quote 5.5h behind other contracts -> stale")

def model_tests():
    import postproxy as P
    print("== Model consistency on current data")
    cfg = json.load(open(P.D / "config.json")); live = json.load(open(P.D / "live.json")); today = dt.datetime.now(ET).date()
    r = P.run(cfg, today, live); H = r["headline"]; S = r["series"]; rule = cfg["post_proxy"]["market_weight_rule"]
    check(all(b["blend"] >= a["blend"] - 1e-12 and b["rules"] >= a["rules"] - 1e-12 and b["market"] >= a["market"] - 1e-12 for a, b in zip(S, S[1:])), "cumulative curves (blend, rules, market) never decrease")
    check(all(0 <= s["blend"] <= 0.99 for s in S), "blend within [0, 0.99]")
    check(H["near"]["blend"] <= H["blend"], "Dec 31 number <= Jun 30 number", f"{H['near']['blend']:.3f} <= {H['blend']:.3f}")
    check(rule["min"] <= H["market_weight"] <= rule["max"], "market weight inside its bounds", str(H["market_weight"]))
    check(all(b[1] >= a[1] - 1e-12 for a, b in zip(r["composite"], r["composite"][1:])), "market composite monotone")
    check(not any(m.get("in_fit") for m in r["markets"] if m.get("expired")), "expired contracts excluded from the fit")
    check(abs(sum(cfg["post_proxy"]["month_weights"].values()) - 1) < 1e-9, "rules month weights sum to 1")
    if "--quick" in sys.argv: return
    print("== Thin-book damping replays (watcher history in git)")
    import backtest
    W = backtest.watcher_history(); pp = cfg["post_proxy"]
    if not any(p["t"] < "2026-10-30" for h in W.values() for p in h["points"]):
        print("SKIP damping replays: git history too shallow (run with full history)"); return
    def rows_at(s):
        t = dt.datetime.fromisoformat(s); mh = {k: v for k, v in backtest.snap(W, t).items() if v["points"]}
        return {x["id"]: x for x in P.table(mh, {"days": []}, pp["smart_money"], t, pp)}
    a = rows_at("2026-10-29T17:57-04:00")["polymarket:3585432"]
    check(a["ref"] > a["p_used"] > a["p"], "Oct 29 lone Polymarket Jun 30 drop is partly used, not excluded", f"price {a['p']}, ref {a['ref']}, used {a['p_used']}, kept {a['k']:.0%}")
    b = rows_at("2026-11-11T22:31-05:00")["kalshi:KXCOMPANYACTIONMERGER-27-27APR01"]
    check(b["ref"] < b["p_used"] < b["p"], "Nov 11 lone Kalshi Apr 1 jump is partly used", f"price {b['p']}, ref {b['ref']}, used {b['p_used']}, kept {b['k']:.0%}")
    c = rows_at("2026-11-12T20:36-05:00")["polymarket:2252540"]
    check(c["k"] >= 0.75, "Nov 12 Bloomberg move (all contracts moved) mostly passes through", f"price {c['p']}, used {c['p_used']}, kept {c['k']:.0%}")

if __name__ == "__main__":
    guard_tests(); model_tests()
    print(f"\n{N[0] - len(FAILS)}/{N[0]} checks passed" + (": FAILED " + "; ".join(FAILS) if FAILS else ""))
    sys.exit(1 if FAILS else 0)
