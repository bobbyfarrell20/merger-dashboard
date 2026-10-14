# Tesla 2026 meeting & merger watch

Live: https://bobbyfarrell20.github.io/merger-dashboard/ (a static page plus `data/*.json`, refreshed by GitHub Actions).

## The question
Between Oct 14 and Dec 14, 2026, does Tesla announce its 2026 annual meeting (an 8-K, DEF/PRE 14A or IR post setting the date)? Three outcomes:
- **A, with a merger:** the merger comes in the same filing, or a merger 8-K, letter or committee comes first.
- **B, routine:** a plain meeting date with no merger.
- **None:** still nothing by Dec 14.

## Model (`data/config.json`)
1. **Totals:** P(any announcement by Dec 14) = logistic(logit(`p_any_by_end.base`) + active `p_any` signals). With-merger share = logistic(logit(`a_share.base`) + active `a_share` signals).
   - Defaults: 0.88 and 0.50. Signals: no leaks −0.50, retail auto-vote +0.15, Musk hint +0.10, credit facility +0.05.
   - Result: about 88% for some announcement, with about 45% of that being A.
2. **Daily weights:** weekday × urgency period × blackout or boost. Unlock and launch blackouts apply to A only, since a routine date doesn't care about SpaceX unlocks.
   - Weekdays: Mon 3.0 (includes Sunday night and pre-market), Tue 2.0, Wed 1.2, Thu 1.0, Fri 0.6, Sat 0.05, Sun 0.
   - Urgency rises toward the notice cutoffs: Oct 27 is the last day for an e-proxy notice for a Dec 6 meeting (Rule 14a-16, 40 days), and Nov 26 the last for Texas 10-day notice (BOC 21.353). It drops after Dec 6.
3. **Competing hazards:** daily hazards hA = kA·wA and hB = kB·wB. kA and kB are solved so the cumulative A and B by Dec 14 hit the totals above. Weekly odds, Monday/Tuesday odds and cumulative odds all come from that daily curve.
4. **Weekly view:** odds the announcement lands that week (A, B, either), the Monday and Tuesday odds, the cumulative odds by end of week, and the Friday weekly expiry that covers it (with ATM IV from Yahoo if that expiry is listed).

## Notice rules used
- SEC Rule 14a-16: notice and access requires 40+ days.
- Texas BOC 21.353: notice 10–60 days before the meeting.
- Texas BOC 21.351: after 13 months a shareholder may petition a court, but only after a written request. Not automatic.
- SEC Rule 14a-6(a): a routine annual meeting needs no preliminary proxy.
- A merger vote needs an effective S-4 first, so it can't happen at a Dec 2026 meeting.
- Links are in `config.notice_rules`.

## Daily change: news vs time
`history.json` stores each day's inputs. A run re-evaluates yesterday's inputs as of today:
- **Time** = that re-run minus yesterday's odds.
- **News** = today's odds minus that re-run.

`update.py` alerts only when the news part is nonzero, or when a watched SEC form is filed.

## Files
- `update.py`: refreshes prices, the options expiry list and ATM IV, EDGAR, and the Google News feed, then recomputes `model.json`.
- `judgment.json`: the plain-language "why".
- `news.json`: the daily feed.
- Prediction markets are not on the page. `watch.py` still tracks them for market-move alerts.
- `.github/workflows/update.yml` runs weekdays at 8:30am ET.

## Running locally
```
pip install yfinance requests && python update.py   # --offline to skip fetching
python3 -m http.server 8765
```
Not investment advice.

## Intraday watcher (`watch.py`, `.github/workflows/watch.yml`)
- **Schedule:** every 15 min from 07:00 to 20:00 New York time on weekdays, and hourly at all other times.
  - UTC crons cover both the EDT and EST windows. A New York-time check lets only the right trigger run, so there are no duplicates.
  - That's about 63 runs per weekday and 24 per weekend day.
- **Speed:** it uses only the Python standard library (nothing to install), and a run takes about 3 seconds.
- **Checks:**
  - EDGAR submissions for TSLA (CIK 1318605) and SPCX (CIK 1181412): 8-K/A, DEF/PRE 14A, DEFA14A, DEFM14A/PREM14A, PREC/DEFC14A, S-4, F-4, 425, SC 13D(/A), SC TO-T/C, SC 14D9, SC 13E3, DFAN14A, 8-A12B.
  - Google News RSS (past day) for 6 merger queries.
- **Headline filter:** a headline passes only if all of these hold:
  1. It mentions tesla, spacex, spcx, tsla or musk.
  2. It has a deal term: merger, merge, combine/combination, acquire/acquisition, takeover, buyout, all-stock, stock swap, exchange ratio, bear hug, offer letter, special committee, independent directors, definitive agreement, tender offer, fairness opinion, annual/special (shareholder) meeting, proxy statement, go private, roll-up.
  3. It mentions SpaceX/SPCX, or it's Tesla plus a meeting or proxy term.
  4. It has no noise term: price prediction, stock forecast, should you buy, quote, top N, "X vs Y stock", rumor mill, podcast, livestream, how to watch, opinion/op-ed.
  5. Its source is on the allowlist: Reuters, Bloomberg, WSJ, FT, CNBC, Barron's, AP, NYT, WaPo, Axios, The Information, Semafor, Fortune, Business Insider, Yahoo Finance, MarketWatch, Electrek, Teslarati, The Verge, TechCrunch, Ars Technica, Space.com, SpaceNews, Spaceflight Now, IBD, Seeking Alpha, Benzinga, CNN, BBC, Guardian, Forbes, Fox Business, Puck, InsideEVs, Not a Tesla App.
- **Dedupe:** `data/seen.json` is committed in the repo and holds EDGAR accession numbers and normalized headline titles. If it's missing, the first run only records what it sees and sends no alert.
- **On a new hit:**
  - Appends the item to `data/news.json`, tagged `watch: true`, with an "odds not yet re-assessed" note.
  - POSTs `{"items":[{"title","url","source","time_et","type"}]}` to secret `WAKE_WEBHOOK_URL`, with header `Authorization: <WAKE_WEBHOOK_AUTH>` (sent exactly as stored) if that secret is set. `type` is `filing` or `headline`, and `time_et` is "YYYY-MM-DD HH:MM" in New York time. If the secret isn't set, the POST is skipped.
  - It never recomputes any odds.
- **Local testing:** `python3 watch.py --dry-run` prints the payload and writes nothing. `--bootstrap` marks everything currently visible as seen.
- **Market-move alerts:** each watcher run also fetches every open Polymarket and Kalshi merger contract and keeps 14 days of prices in `data/markets_history.json`. Settings are in `config.market_watch`.
  - It sends a `market_move` item when a contract moves `move_pp` points (default 10) against its price about `window_hours` (24) earlier. If there's less history than that, it compares with the oldest stored price.
  - After an alert, it only alerts again once the price moves another `move_pp` points from the alerted price.
  - Price used: the bid/ask midpoint if the spread is 6¢ or less; otherwise the last trade if 24h volume is at least 50; otherwise the quote counts as thin and is stored but never alerts.
  - Extra payload fields: `contract`, `from`, `to` (0–1), `window_hours`.
