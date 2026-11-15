#!/usr/bin/env python3
"""Backtest the prediction-market composite: legacy (volume-weighted) vs liquidity_v2, replayed on every
watcher snapshot in git history (bid/ask/volume known) since Oct 13, plus the Aug 20+ price backfill.
Usage: python backtest.py [--out data/calibration.json]   (read-only unless --out is given)"""
import json, subprocess, sys, statistics, datetime as dt
from pathlib import Path
import postproxy as pp_mod
R = Path(__file__).parent; D = R / "data"
F = dt.datetime.fromisoformat

def watcher_history():
    revs = subprocess.check_output(["git", "-C", str(R), "log", "--format=%H", "--", "data/markets_history.json"]).decode().split()
    allp, names = {}, {}
    for r in revs:
        try: mh = json.loads(subprocess.check_output(["git", "-C", str(R), "show", f"{r}:data/markets_history.json"], stderr=subprocess.DEVNULL))
        except Exception: continue
        for cid, h in mh.items():
            names[cid] = h["name"]
            for p in h["points"]: allp.setdefault(cid, {})[p["t"]] = p
    cur = json.loads((D / "markets_history.json").read_text())
    for cid, h in cur.items():
        names[cid] = h["name"]
        for p in h["points"]: allp.setdefault(cid, {})[p["t"]] = p
    return {cid: {"name": names[cid], "points": sorted(d.values(), key=lambda p: F(p["t"]))} for cid, d in allp.items()}

def snap(W, T):
    return {cid: {"name": h["name"], "points": [p for p in h["points"] if T - dt.timedelta(days=14) <= F(p["t"]) <= T]} for cid, h in W.items()}

def run_at(W, T, pp):
    mh = {k: v for k, v in snap(W, T).items() if v["points"]}
    sm = pp["smart_money"]; today = T.date()
    rows = pp_mod.table(mh, {"days": []}, sm, T, pp)
    curve = pp_mod.composite(rows, pp, today)
    w, L = pp_mod.market_weight(pp, mh, T, today)
    at = lambda d: pp_mod.interp(curve, d, today)
    return {"t": T.isoformat(timespec="minutes"), "dec31": at("2026-12-31"), "jun30": at("2027-06-30"), "w": w, "depth": L,
            "direct": {r["id"]: r["p"] for r in rows}}

def tv(xs): return sum(abs(b - a) for a, b in zip(xs, xs[1:]))
def reversals(xs, ts, thr=0.03):
    """moves >= thr between snapshots that are >=50% undone within the next 24h"""
    n = k = 0
    for i in range(1, len(xs)):
        m = xs[i] - xs[i - 1]
        if abs(m) < thr: continue
        n += 1
        fut = [xs[j] for j in range(i + 1, len(xs)) if F(ts[j]) - F(ts[i]) <= dt.timedelta(hours=24)]
        if fut and any((x - xs[i]) * m < 0 and abs(x - xs[i]) >= 0.5 * abs(m) for x in fut): k += 1
    return n, k

def main():
    cfg = json.loads((D / "config.json").read_text()); new = cfg["post_proxy"]
    old = {**new, "market": {k: v for k, v in new["market"].items() if k != "method"}, "market_weight": 0.4}
    old.pop("market_weight_rule", None)
    W = watcher_history()
    ts = sorted({F(p["t"]) for h in W.values() for p in h["points"] if F(p["t"]) >= dt.datetime(2026, 10, 20, 12, tzinfo=pp_mod.ET)})
    A = [run_at(W, T, old) for T in ts]; B = [run_at(W, T, new) for T in ts]
    T0 = dt.datetime(2026, 11, 12, 15, 9, tzinfo=pp_mod.ET)
    out = {"snapshots": len(ts), "from": ts[0].isoformat(timespec="minutes"), "to": ts[-1].isoformat(timespec="minutes"), "periods": {}}
    for nm, sel in (("pre_bloomberg", lambda t: t < T0), ("post_bloomberg", lambda t: t >= T0), ("all", lambda t: True)):
        idx = [i for i, t in enumerate(ts) if sel(t)]
        res = {}
        for key in ("dec31", "jun30"):
            a = [A[i][key] for i in idx]; b = [B[i][key] for i in idx]; tt = [A[i]["t"] for i in idx]
            res[key] = {"legacy": {"path_pts": round(tv(a) * 100, 1), "net_pts": round((a[-1] - a[0]) * 100, 1), "big_moves_reversed": reversals(a, tt),
                                   "max_step_pts": round(max(abs(y - x) for x, y in zip(a, a[1:])) * 100, 1)},
                        "liquidity_v2": {"path_pts": round(tv(b) * 100, 1), "net_pts": round((b[-1] - b[0]) * 100, 1), "big_moves_reversed": reversals(b, tt),
                                         "max_step_pts": round(max(abs(y - x) for x, y in zip(b, b[1:])) * 100, 1)},
                        "mean_abs_gap_pts": round(statistics.mean(abs(x - y) for x, y in zip(a, b)) * 100, 2)}
        res["weight_v2"] = {"min": min(B[i]["w"] for i in idx), "max": max(B[i]["w"] for i in idx), "median": statistics.median(B[i]["w"] for i in idx)}
        out["periods"][nm] = res
    pick = lambda s: next(i for i, t in enumerate(ts) if t >= F(s))
    out["episodes"] = {}
    for lab, a_, b_ in (("Oct 29 Polymarket Jun 30 drop", "2026-10-29T13:00-04:00", "2026-10-29T17:50-04:00"),
                        ("Nov 11 evening Kalshi Apr 1 jump (before Bloomberg)", "2026-11-11T19:00-05:00", "2026-11-11T22:00-05:00"),
                        ("Nov 12 Bloomberg report", "2026-11-12T13:00-05:00", "2026-11-12T18:00-05:00")):
        i, j = pick(a_), pick(b_)
        out["episodes"][lab] = {"from": ts[i].isoformat(timespec="minutes"), "to": ts[j].isoformat(timespec="minutes"),
                               **{f"{m}_{k}": [round(X[i][k] * 100, 1), round(X[j][k] * 100, 1)] for m, X in (("legacy", A), ("v2", B)) for k in ("dec31", "jun30")}}
    out["series"] = [{"t": a["t"], "legacy_dec31": round(a["dec31"], 4), "v2_dec31": round(b["dec31"], 4), "legacy_jun30": round(a["jun30"], 4),
                      "v2_jun30": round(b["jun30"], 4), "w_v2": b["w"], "depth": round(b["depth"] or 0)} for a, b in zip(A, B)]
    # dashboard vs markets since the post-proxy model started (recorded daily readings)
    hist = [p for p in json.loads((D / "odds_history.json").read_text()) if p.get("rules") is not None]
    out["dashboard_vs_market_jun30"] = [{"date": p["date"], "dashboard": p["X"], "rules": p["rules"], "market": p["market"]} for p in hist]
    print(json.dumps({k: v for k, v in out.items() if k != "series"}, indent=1))
    if "--out" in sys.argv:
        Path(sys.argv[sys.argv.index("--out") + 1]).write_text(json.dumps(out, indent=0))

if __name__ == "__main__":
    main()
