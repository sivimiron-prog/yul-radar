#!/usr/bin/env python3
"""Coverage test: ask Travelpayouts directly for specific YUL routes and count what comes back."""
import sqlite3, time
import radar

AIRPORTS = ["MVD","KTM","NBO","CMB","ACC","LPB","ASU","UIO","GUA","SAL","MGA","PTY","KIN","POS","GEO","CCS",
            "ADD","DAR","KGL","LOS","DKR","ABJ","JNB","TNR","MRU","ULN","PNH","VTE","RGN","DAC","ISB","AMM"]

def count(path, q):
    rows = radar._tp_get(path, q)
    prices = [r.get("price") or r.get("value") for r in rows if (r.get("price") or r.get("value"))]
    return len(prices), (min(prices) if prices else None)

db = sqlite3.connect(radar.DB_PATH)
seen = {d.split(":")[-1] for (d,) in db.execute("SELECT DISTINCT dest FROM obs")}
base = {"currency": radar.CURRENCY, "sorting": "price", "limit": "100", "page": "1"}
totals = {"rt": 0, "out": 0, "back": 0, "any": 0, "new": 0}
print(f"{'code':<5}{'city':<18}{'regular scan':<14}{'round trip':<16}{'one way out':<16}{'one way back':<16}")
for code in AIRPORTS:
    res = {}
    for kind, extra in (("rt",   {"origin": radar.ORIGIN, "destination": code, "one_way": "false"}),
                        ("out",  {"origin": radar.ORIGIN, "destination": code, "one_way": "true"}),
                        ("back", {"origin": code, "destination": radar.ORIGIN, "one_way": "true"})):
        n, p = count("/aviasales/v3/prices_for_dates", {**base, **extra})
        if n == 0:   # second source: latest prices over the coming year
            lq = {"currency": radar.CURRENCY, "period_type": "year", "one_way": extra["one_way"], "sorting": "price",
                  "limit": "100", "page": "1", "show_to_affiliates": "true", "trip_class": "0",
                  "origin": extra["origin"], "destination": extra["destination"]}
            n, p = count("/aviasales/v3/get_latest_prices", lq)
        res[kind] = (n, p)
        time.sleep(0.25)
    got = [k for k in res if res[k][0]]
    for k in got: totals[k] += 1
    if got: totals["any"] += 1
    if got and code not in seen: totals["new"] += 1
    fmt = lambda t: f"{t[0]} (${t[1]:.0f})" if t[0] else "-"
    print(f"{code:<5}{radar.city_name(code)[:16]:<18}{'yes' if code in seen else 'no':<14}{fmt(res['rt']):<16}{fmt(res['out']):<16}{fmt(res['back']):<16}")
n = len(AIRPORTS)
print(f"\nSUMMARY {n} airports: any fare {totals['any']}, round trip {totals['rt']}, one way out {totals['out']}, "
      f"one way back {totals['back']}, never seen in regular scans but found now {totals['new']}")
