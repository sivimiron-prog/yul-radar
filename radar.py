#!/usr/bin/env python3
"""
YUL Error Fare Radar
Scans round trips from Montréal (YUL), one-way flights from YUL, and one-way
flights back to YUL, learns each route's normal price,
flags fares far below normal, has Claude review them, sends alerts,
and builds a phone-friendly page in the site/ folder.

Uses only the Python standard library. Settings come from a .env file
next to this script, or from environment variables (GitHub Actions).

  python radar.py            run one scan
  python radar.py --sample   run one scan with simulated fares (no keys needed)
  python radar.py --test-alert   send a test message to check Telegram/email
"""
import json, os, sys, sqlite3, statistics, smtplib, ssl, random, html, gzip, time
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone, date
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- settings
def load_env():
    p = HERE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env()
def env(k, d=""): return os.environ.get(k, d).strip()
def envf(k, d):
    try: return float(env(k, str(d)))
    except ValueError: return d
def envi(k, d):
    try: return int(env(k, str(d)))
    except ValueError: return d

ORIGIN          = env("ORIGIN", "YUL").upper()
CURRENCY        = env("CURRENCY", "cad").lower()
MONTHS_AHEAD    = envi("MONTHS_AHEAD", 10)
DEAL_DROP       = envf("DEAL_THRESHOLD", 0.40)   # 40% below normal = deal
ERROR_DROP      = envf("ERROR_THRESHOLD", 0.55)  # 55% below normal = likely error
MAX_PRICE       = envf("MAX_PRICE", 0)           # 0 = no cap
MIN_TRIP_DAYS   = envi("MIN_TRIP_DAYS", 3)
MAX_TRIP_DAYS   = envi("MAX_TRIP_DAYS", 30)
MAX_VERIFY      = envi("MAX_GOOGLE_CHECKS_PER_SCAN", 3)
LANG            = env("ALERT_LANGUAGE", "en").lower()  # en or fr
REALERT_DAYS    = envi("REALERT_AFTER_DAYS", 3)

TP_TOKEN        = env("TRAVELPAYOUTS_TOKEN")
SERP_KEY        = env("SERPAPI_KEY")
CLAUDE_KEY      = env("ANTHROPIC_API_KEY")
CLAUDE_MODEL    = env("CLAUDE_MODEL", "claude-haiku-5-5")
TG_TOKEN        = env("TELEGRAM_BOT_TOKEN")
TG_CHAT         = env("TELEGRAM_CHAT_ID")
SMTP_HOST       = env("SMTP_HOST"); SMTP_PORT = envi("SMTP_PORT", 587)
SMTP_USER       = env("SMTP_USER"); SMTP_PASS = env("SMTP_PASSWORD")
EMAIL_TO        = env("EMAIL_TO")

DB_PATH     = HERE / "data" / "radar.db"

def log(*a): print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)

# ---------------------------------------------------------------- http
def http_json(url, headers=None, data=None, timeout=40):
    h = {"Accept-Encoding": "gzip", "User-Agent": "yul-radar/1.0"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))

# ---------------------------------------------------------------- city names
FALLBACK_CITIES = {
 "PAR":"Paris","LIS":"Lisbon","ROM":"Rome","BCN":"Barcelona","ATH":"Athens","LON":"London",
 "DUB":"Dublin","REK":"Reykjavík","TYO":"Tokyo","SEL":"Seoul","BKK":"Bangkok","DXB":"Dubai",
 "CUN":"Cancún","PUJ":"Punta Cana","SJO":"San José","CTG":"Cartagena","LIM":"Lima",
 "BUE":"Buenos Aires","SAO":"São Paulo","HNL":"Honolulu","LAX":"Los Angeles","MIA":"Miami",
 "YVR":"Vancouver","CAS":"Casablanca","SYD":"Sydney","MAD":"Madrid","OPO":"Porto",
 "AMS":"Amsterdam","BER":"Berlin","PTP":"Pointe-à-Pitre","FDF":"Fort-de-France","HAV":"Havana",
 "VRA":"Varadero","NYC":"New York","FLL":"Fort Lauderdale","MEX":"Mexico City","BOG":"Bogotá",
 "MRS":"Marseille","NCE":"Nice","TLS":"Toulouse","LYS":"Lyon","BOD":"Bordeaux","ALG":"Algiers",
 "TUN":"Tunis","DPS":"Bali","HKT":"Phuket","SGN":"Ho Chi Minh City","HAN":"Hanoi","MNL":"Manila",
}
_places = None
def places():
    """{code: {name, name_fr, country, country_fr}} from Travelpayouts' public data files, cached on disk."""
    global _places
    if _places is None:
        _places = {k: {"name": v} for k, v in FALLBACK_CITIES.items()}
        cache = HERE / "data" / "places.json"
        try:
            stale = cache.exists() and '"cc"' not in cache.read_text(encoding="utf-8")[:4000]
            if (stale or not cache.exists()) and TP_TOKEN:
                get = lambda p: http_json("https://api.travelpayouts.com/data/" + p)
                cen, cfr = get("en/cities.json"), get("fr/cities.json")
                nen = {c["code"]: c.get("name") for c in get("en/countries.json")}
                nfr = {c["code"]: c.get("name") for c in get("fr/countries.json")}
                fr = {c["code"]: c.get("name") for c in cfr}
                out = {c["code"]: {"name": c.get("name") or c["code"], "name_fr": fr.get(c["code"]) or "",
                                   "country": nen.get(c.get("country_code")) or "", "country_fr": nfr.get(c.get("country_code")) or "", "cc": c.get("country_code") or ""}
                       for c in cen}
                cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            if cache.exists():
                _places.update(json.loads(cache.read_text(encoding="utf-8")))
        except Exception as e:
            log("City names unavailable, using codes:", e)
    return _places

EUROPE = set("AD AL AT BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GB GI GR HR HU IE IS IT LI LT LU LV MC MD ME MK MT NL NO PL PT RO RS SE SI SK SM UA VA XK".split())
REGIONS = {
    "europe": EUROPE | {"TR", "GE", "AM", "AZ"},
    "asia": set("AE AF AM AZ BD BH BN BT CN GE HK ID IL IN IQ IR JO JP KG KH KR KW KZ LA LB LK MM MN MO MV MY NP OM PH PK PS QA SA SG SY TH TJ TL TM TR TW UZ VN YE".split()),
    "sun": set("AG AI AW BB BL BQ BS BZ CR CU CW DM DO GD GP GT HN HT JM KN KY LC MF MQ MX NI PA PR SV SX TC TT VC VG VI".split()),
    "north": {"US", "CA", "PM", "BM"},
    "south": set("AR BO BR CL CO EC FK GF GY PE PY SR UY VE".split()),
    "africa": set("AO BF BI BJ BW CD CF CG CI CM CV DJ DZ EG ER ET GA GH GM GN GQ GW KE KM LR LS LY MA MG ML MR MU MW MZ NA NE NG RE RW SC SD SL SN SO SS ST SZ TD TG TN TZ UG YT ZA ZM ZW".split()),
    "oceania": set("AU NZ FJ PF NC WS TO VU PG GU MP CK".split()),
}
def regions_of(code):
    cc = (places().get(code) or {}).get("cc")
    return [r for r, ccs in REGIONS.items() if cc in ccs]

KIND_LABEL = {"rt": "round trip", "out": "one way", "back": "one way"}
def key_of(kind, place): return place if kind == "rt" else f"{kind}:{place}"
def route_text(kind, city):
    return {"rt": f"Montréal ⇄ {city}", "out": f"Montréal → {city}", "back": f"{city} → Montréal"}[kind]

def city_name(code):
    p = places().get(code) or {}
    return (p.get("name_fr") if LANG == "fr" else "") or p.get("name") or code

# ---------------------------------------------------------------- fare sources
def months_to_scan():
    d = date.today().replace(day=1)
    out = []
    for _ in range(MONTHS_AHEAD):
        out.append(d.strftime("%Y-%m"))
        d = (d + timedelta(days=32)).replace(day=1)
    return out

def _tp_get(path, q):
    url = "https://api.travelpayouts.com" + path + "?" + urllib.parse.urlencode(q)
    try:
        return http_json(url, headers={"X-Access-Token": TP_TOKEN}).get("data") or []
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit("Travelpayouts refused the token. Check TRAVELPAYOUTS_TOKEN.")
        log(f"  HTTP {e.code} for {path}, skipped"); return []
    except Exception as e:
        log(f"  {e} for {path}, skipped"); return []

def _fare(kind, r):
    """Normalize one Travelpayouts row (prices_for_dates or get_latest_prices shape)."""
    price = r.get("price") or r.get("value")
    dep = (r.get("departure_at") or r.get("depart_date") or "")[:10]
    if not price or not dep:
        return None
    ret = (r.get("return_at") or r.get("return_date") or "")[:10] if kind == "rt" else None
    if kind == "rt" and not ret:
        return None
    if kind == "back":
        place, airport = r["origin"], r.get("origin_airport") or r["origin"]
    else:
        place, airport = r["destination"], r.get("destination_airport") or r["destination"]
    stops = r.get("transfers", r.get("number_of_changes")) or 0
    if kind == "rt":
        stops += r.get("return_transfers") or 0
    return {"kind": kind, "place": place, "airport": airport, "key": key_of(kind, place),
            "depart": dep, "ret": ret, "price": float(price), "airline": r.get("airline", ""), "stops": stops,
            "link": ("https://www.aviasales.com" + r["link"]) if r.get("link") else ""}

def fetch_travelpayouts():
    """Each month: cheapest round trips from YUL, one-way from YUL, one-way to YUL."""
    fares = []
    plan = [("rt",   {"origin": ORIGIN, "one_way": "false"}),
            ("out",  {"origin": ORIGIN, "one_way": "true"}),
            ("back", {"destination": ORIGIN, "one_way": "true"})]
    for m in months_to_scan():
        counts = []
        for kind, extra in plan:
            q = {"departure_at": m, "currency": CURRENCY, "sorting": "price", "unique": "false",
                 "limit": "1000", "page": "1", **extra}
            rows = _tp_get("/aviasales/v3/prices_for_dates", q)
            if kind != "rt" and len(rows) < 20:
                # one-way via prices_for_dates can be sparse; add the latest-prices feed for that month
                lq = {"currency": CURRENCY, "period_type": "month", "beginning_of_period": m + "-01",
                      "one_way": "true", "sorting": "price", "limit": "1000", "page": "1",
                      "show_to_affiliates": "true", "trip_class": "0"}
                lq.update({"origin": ORIGIN} if kind == "out" else {"destination": ORIGIN})
                rows = rows + _tp_get("/aviasales/v3/get_latest_prices", lq)
            got = [f for f in (_fare(kind, r) for r in rows) if f]
            fares += got
            counts.append(f"{kind} {len(got)}")
            time.sleep(0.3)
        log(f"  {m}: " + ", ".join(counts))
    fares += fetch_watchlist()
    return fares

# Airports checked directly on every scan, so they show up even when few people search them.
# Override with WATCH_AIRPORTS="LIS,MVD,..." in .env or a GitHub variable.
WATCH_DEFAULT = {
    "Europe": "LON DUB PAR BRU AMS LUX BER MUC ZRH GVA VIE ROM MIL MAD BCN LIS OPO ATH MLA LCA ZAG DBV LJU BUD PRG "
              "BTS WAW KRK BUH CLJ SOF BEG SJJ TGD TIA SKP KIV VNO RIX TLL HEL STO OSL CPH REK IST NCE MRS BOD",
    "Latin America": "MEX GUA SAL SAP MGA SJO PTY BOG MDE UIO LIM LPB SCL BUE MVD ASU SAO RIO",
    "Asia": "TYO SEL BJS SHA HKG TPE BKK SGN HAN SIN KUL MNL DPS DEL BOM CMB KTM DXB DOH AMM",
}
def watch_airports():
    custom = env("WATCH_AIRPORTS")
    codes = custom.replace(",", " ").split() if custom else " ".join(WATCH_DEFAULT.values()).split()
    return list(dict.fromkeys(c.upper() for c in codes if c.upper() != ORIGIN))

def _watch_one(code):
    """Cheapest round trip, one way out, and one way back for one airport, over all upcoming dates."""
    out = []
    for kind, a, b, ow in (("rt", ORIGIN, code, "false"), ("out", ORIGIN, code, "true"), ("back", code, ORIGIN, "true")):
        q = {"origin": a, "destination": b, "one_way": ow, "currency": CURRENCY, "sorting": "price",
             "unique": "false", "limit": "100", "page": "1"}
        rows = _tp_get("/aviasales/v3/prices_for_dates", q)
        if not rows:
            lq = {"origin": a, "destination": b, "one_way": ow, "currency": CURRENCY, "period_type": "year",
                  "sorting": "price", "limit": "100", "page": "1", "show_to_affiliates": "true", "trip_class": "0"}
            rows = _tp_get("/aviasales/v3/get_latest_prices", lq)
        out += [f for f in (_fare(kind, r) for r in rows) if f]
    return out

def fetch_watchlist():
    from concurrent.futures import ThreadPoolExecutor
    codes = watch_airports()
    if not codes:
        return []
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_watch_one, codes))
    fares = [f for r in results for f in r]
    hit = sum(1 for r in results if r)
    log(f"  watch list: {len(codes)} airports checked, {hit} with fares, {len(fares)} fares")
    return fares

SAMPLE_ROUTES = {"PAR":950,"LIS":900,"ROM":1050,"BCN":980,"ATH":1150,"LON":880,"DUB":850,"REK":700,
 "TYO":1600,"SEL":1500,"BKK":1700,"DXB":1500,"CUN":650,"PUJ":700,"SJO":750,"CTG":800,"LIM":900,
 "BUE":1400,"SAO":1300,"HNL":1100,"LAX":450,"MIA":380,"YVR":450,"CAS":1100,"SYD":2100}
SAMPLE_AIRLINES = ["AC","TS","AF","TP","WS","KL","LH","BA","QR","EK","AV","LA"]

def fetch_sample(seed):
    rnd = random.Random(seed)
    fares = []
    errors = rnd.sample(list(SAMPLE_ROUTES), 4)
    for kind, factor in (("rt", 1.0), ("out", 0.62), ("back", 0.6)):
        for place, base in SAMPLE_ROUTES.items():
            for i in range(14):
                dep = date.today() + timedelta(days=14 + rnd.randint(0, 280))
                ret = (dep + timedelta(days=rnd.randint(5, 16))).isoformat() if kind == "rt" else None
                season = 1 + 0.12 * ((dep.month in (7, 8, 12)) - (dep.month in (1, 2, 11)))
                p = base * factor * season * rnd.uniform(0.85, 1.25)
                if place == errors[["rt", "out", "back"].index(kind)] and i == 0:
                    p = base * factor * rnd.uniform(0.28, 0.5)
                fares.append({"kind": kind, "place": place, "airport": place, "key": key_of(kind, place),
                              "depart": dep.isoformat(), "ret": ret, "price": round(p),
                              "airline": rnd.choice(SAMPLE_AIRLINES), "stops": rnd.choice([0, 1, 1, 2]), "link": ""})
    return fares

# ---------------------------------------------------------------- storage
def db(sample=False):
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH.with_name("sample.db") if sample else DB_PATH)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS obs(ts TEXT, dest TEXT, month TEXT, depart TEXT, ret TEXT, price REAL, airline TEXT, stops INT);
    CREATE INDEX IF NOT EXISTS obs_dm ON obs(dest, month);
    CREATE TABLE IF NOT EXISTS alerts(ts TEXT, dest TEXT, depart TEXT, ret TEXT, price REAL, verdict TEXT);
    CREATE TABLE IF NOT EXISTS finds(ts TEXT, dest TEXT, month TEXT, data TEXT);
    """)
    return c

def baseline(c, dest, month, scan_prices):
    """Normal price for a destination and travel month.
    Uses the last 90 days of stored observations once there are enough,
    otherwise the median of this scan's prices for that destination."""
    since = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    hist = [r[0] for r in c.execute(
        "SELECT price FROM obs WHERE dest=? AND month=? AND ts>=?", (dest, month, since))]
    if len(hist) >= 8:
        return statistics.median(hist), "90-day history", len(hist)
    hist_all = [r[0] for r in c.execute("SELECT price FROM obs WHERE dest=? AND ts>=?", (dest, since))]
    if len(hist_all) >= 12:
        return statistics.median(hist_all), "90-day history, all months", len(hist_all)
    if len(scan_prices) >= 5:
        return statistics.median(scan_prices), "this scan only", len(scan_prices)
    return None, "not enough data", len(scan_prices)

# ---------------------------------------------------------------- google flights check
def google_check(f):
    if not SERP_KEY:
        return None
    a, b = (f["airport"], ORIGIN) if f["kind"] == "back" else (ORIGIN, f["airport"])
    q = {"engine": "google_flights", "departure_id": a, "arrival_id": b, "outbound_date": f["depart"],
         "currency": CURRENCY.upper(), "hl": "en", "gl": "ca", "api_key": SERP_KEY}
    if f["kind"] == "rt": q["return_date"] = f["ret"]
    else: q["type"] = "2"   # one way
    try:
        r = http_json("https://serpapi.com/search.json?" + urllib.parse.urlencode(q), timeout=60)
    except Exception as e:
        log("  Google Flights check failed:", e); return None
    flights = (r.get("best_flights") or []) + (r.get("other_flights") or [])
    prices = [x["price"] for x in flights if x.get("price")]
    pi = r.get("price_insights") or {}
    return {"lowest_now": min(prices) if prices else None,
            "typical_range": pi.get("typical_price_range"),
            "price_level": pi.get("price_level")}

# ---------------------------------------------------------------- claude review
def claude_review(cand):
    lang = "French (Québec)" if LANG == "fr" else "English"
    if not CLAUDE_KEY:
        return rule_review(cand)
    prompt = f"""You review cheap airfare found by an automated scanner for travellers in Montréal.
Decide if this fare is a likely airline ERROR FARE, a genuine STRONG DEAL, or a FALSE ALARM
(stale cached price, data glitch, or baseline too thin to trust).

Signals for an error fare: price far below normal for that season, business priced like economy,
missing fuel surcharge pattern (price close to taxes alone), price matching a wrong currency
conversion, odd routing that prices below the direct flight.
A Google Flights check, when present, is the most current price: if it no longer shows anything
near the scanned price, the fare has probably expired.

Fare data (prices in {CURRENCY.upper()}; "trip" says round trip or one way, and its direction):
{json.dumps(cand, ensure_ascii=False, indent=1)}

Reply with JSON only:
{{"verdict": "likely_error" | "strong_deal" | "false_alarm",
  "confidence": 0-100,
  "reason": "one or two plain sentences, in {lang}",
  "alert": "a short alert in {lang} like a deal-newsletter post: route with direction, price, round trip or one way, normal price, travel dates, airline, why it's special, and the advice to book soon but wait a few days before non-refundable hotels. No markdown, no emoji except one ✈️ at the start."}}"""
    try:
        r = http_json("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01"},
                      data={"model": CLAUDE_MODEL, "max_tokens": 600,
                            "messages": [{"role": "user", "content": prompt}]}, timeout=60)
        text = "".join(b.get("text", "") for b in r.get("content", []))
        text = text[text.find("{"): text.rfind("}") + 1]
        out = json.loads(text)
        out["by"] = "Claude"
        return out
    except urllib.error.HTTPError as e:
        log("  Claude review failed:", e.code, e.read()[:200]); return rule_review(cand)
    except Exception as e:
        log("  Claude review failed:", e); return rule_review(cand)

def rule_review(c):
    drop = c["drop_pct"]
    g = c.get("google_check") or {}
    if g.get("lowest_now") and g["lowest_now"] > c["price"] * 1.5:
        verdict, why = "false_alarm", "Google Flights no longer shows this price, so it has probably expired."
    elif c["baseline_source"] == "this scan only" and drop < 65:
        verdict, why = "strong_deal", f"{drop}% under other prices seen today. Price history is still building."
    elif drop >= ERROR_DROP * 100:
        verdict, why = "likely_error", f"{drop}% under the normal price for these dates, far more than a typical sale."
    else:
        verdict, why = "strong_deal", f"{drop}% under the normal price for these dates."
    fr = LANG == "fr"
    head = ("✈️ POSSIBLE PRIX ERRONÉ" if verdict == "likely_error" else "✈️ AUBAINE") if fr else \
           ("✈️ POSSIBLE ERROR FARE" if verdict == "likely_error" else "✈️ DEAL")
    rt = c["kind"] == "rt"
    when_fr = f"Du {c['depart']} au {c['return']}" if rt else f"Le {c['depart']}"
    when_en = f"{c['depart']} to {c['return']}" if rt else f"{c['depart']}"
    route_fr = route_text(c["kind"], c["city"])
    alert = (f"{head} : {route_fr}\n{c['price']:.0f} $ {'aller-retour' if rt else 'aller simple'} (normalement {c['normal_price']:.0f} $, -{drop} %)\n"
             f"{when_fr} · {c['airline'] or 'compagnie variée'} · {c['stops']} escale(s)\n"
             "Réservez vite, mais attendez quelques jours avant de payer un hôtel non remboursable.") if fr else \
            (f"{head}: {route_text(c['kind'], c['city'])}\n${c['price']:.0f} {KIND_LABEL[c['kind']]} (normally ${c['normal_price']:.0f}, -{drop}%)\n"
             f"{when_en} · {c['airline'] or 'various airlines'} · {c['stops']} stop(s)\n"
             "Book soon, but wait a few days before paying for non-refundable hotels.")
    return {"verdict": verdict, "confidence": 60, "reason": why, "alert": alert, "by": "rules"}

# ---------------------------------------------------------------- alerts
def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        return False
    try:
        http_json(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  data={"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True})
        return True
    except Exception as e:
        log("  Telegram failed:", e); return False

def send_email(subject, text):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_TO):
        return False
    try:
        m = EmailMessage(); m["Subject"] = subject; m["From"] = SMTP_USER; m["To"] = EMAIL_TO
        m.set_content(text)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls(context=ssl.create_default_context()); s.login(SMTP_USER, SMTP_PASS); s.send_message(m)
        return True
    except Exception as e:
        log("  Email failed:", e); return False

def google_link(f):
    if f["kind"] == "rt":
        q = f"Flights from {ORIGIN} to {f['airport']} on {f['depart']} through {f['ret']}"
    elif f["kind"] == "out":
        q = f"One way flights from {ORIGIN} to {f['airport']} on {f['depart']}"
    else:
        q = f"One way flights from {f['airport']} to {ORIGIN} on {f['depart']}"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)

def already_alerted(c, f, verdict):
    since = (datetime.now(timezone.utc) - timedelta(days=REALERT_DAYS)).isoformat()
    row = c.execute("SELECT MIN(price) FROM alerts WHERE dest=? AND substr(depart,1,7)=? AND ts>=? AND verdict!='false_alarm'",
                    (f["key"], f["depart"][:7], since)).fetchone()
    return row[0] is not None and f["price"] >= row[0] * 0.95   # re-alert only if 5%+ cheaper

# ---------------------------------------------------------------- phone site
SITE_DIR = HERE / "site"

def _png(size, pixel):
    """Tiny PNG writer (no extra packages) for the home-screen icon."""
    import zlib, struct
    raw = b"".join(b"\x00" + bytes(c for x in range(size) for c in pixel(x, y)) for y in range(size))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))

def _icon_pixel(size):
    import math
    bg, fg = (31, 79, 209), (255, 255, 255)
    c = size / 2
    def mix(a, b, t): return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    def px(x, y):
        dx, dy = x + .5 - c, y + .5 - c
        r = math.hypot(dx, dy) / c
        t = 0.0
        for ring in (0.62, 0.34):
            t = max(t, 0.45 * max(0, 1 - abs(r - ring) * c / 2.2))
        # sweep line toward upper right
        ang = math.atan2(-dy, dx)
        if 0 < r < 0.78 and abs(ang - 0.6) < 0.035 / max(r, 0.05): t = max(t, 1)
        bx, by = c + 0.42 * c, c - 0.25 * c
        if math.hypot(x + .5 - bx, y + .5 - by) < size * 0.06: t = 1
        return mix(bg, fg, min(t, 1))
    return px

def build_site(c, scanned, n_dest, sample, now):
    SITE_DIR.mkdir(exist_ok=True)
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    latest = {}
    for ts, dest, month, data in c.execute("SELECT ts, dest, month, data FROM finds WHERE ts>=? ORDER BY ts", (since,)):
        d = json.loads(data); d["found"] = ts; d["in_latest"] = ts == now
        d.setdefault("kind", "rt"); d.setdefault("key", dest); d.setdefault("place", d.get("dest", dest))
        d["regions"] = regions_of(d["place"])
        prev = latest.get(dest)
        if prev and prev["_ts"] == ts and prev["drop_pct"] >= d["drop_pct"]:
            continue   # same scan, keep the bigger drop
        d["_ts"] = ts
        if prev and prev["price"] == d["price"] and prev["depart"] == d["depart"]: d["found"] = prev["found"]
        latest[dest] = d
    finds = sorted(latest.values(), key=lambda d: (not d["in_latest"], d["verdict"] != "likely_error", -d["drop_pct"]))
    dests = {d["key"] for d in finds}
    since30 = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    history = {}
    for dest in dests:
        rows = c.execute("SELECT ts, MIN(price) FROM obs WHERE dest=? AND ts>=? GROUP BY ts ORDER BY ts", (dest, since30)).fetchall()
        history[dest] = [[t, p] for t, p in rows][-40:]
    prices = []
    since90 = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    rows = c.execute("SELECT dest, price, depart, ret, airline, stops FROM obs WHERE ts=? ORDER BY price", (now,)).fetchall()
    best = {}
    for key, price, depart, ret, airline, stops in rows:
        best.setdefault(key, (price, depart, ret, airline, stops))
    for key, (price, depart, ret, airline, stops) in best.items():
        kind, place = (key.split(":", 1) if ":" in key else ("rt", key))
        hist = [r[0] for r in c.execute("SELECT price FROM obs WHERE dest=? AND ts>=?", (key, since90))]
        p = places().get(place) or {}
        prices.append({"key": key, "kind": kind, "dest": place, "city": city_name(place), "regions": regions_of(place),
                       "alt": " ".join(filter(None, [p.get("name"), p.get("name_fr"), p.get("country"), p.get("country_fr")])),
                       "country": (p.get("country_fr") if LANG == "fr" else "") or p.get("country") or "",
                       "price": price, "normal": round(statistics.median(hist)) if hist else None,
                       "depart": depart, "return": ret, "airline": airline, "stops": stops,
                       "google_link": google_link({"kind": kind, "airport": place, "depart": depart, "ret": ret})})
    prices.sort(key=lambda x: x["price"])
    # one-way fares with dates, for the trip combos view: up to 2 per city per month, 10 per city
    oneways = []
    per = {}
    for key, price, depart, airline, stops in c.execute(
            "SELECT dest, price, depart, airline, stops FROM obs WHERE ts=? AND (dest LIKE 'out:%' OR dest LIKE 'back:%') ORDER BY price", (now,)):
        kind, place = key.split(":", 1)
        n_city = per.setdefault(key, {})
        month = depart[:7]
        if sum(n_city.values()) >= 10 or n_city.get(month, 0) >= 2 or any(o["k"] == kind and o["c"] == place and o["d"] == depart for o in oneways[-12:]):
            continue
        n_city[month] = n_city.get(month, 0) + 1
        oneways.append({"k": kind, "c": place, "n": city_name(place), "d": depart, "p": price, "s": stops,
                        "a": airline, "r": regions_of(place)})
    data = {"generated": now, "prices": prices, "oneways": oneways, "repo": env("GITHUB_REPOSITORY"), "sample": sample, "origin": ORIGIN, "lang": LANG, "currency": CURRENCY.upper(),
            "deal_pct": round(DEAL_DROP * 100), "error_pct": round(ERROR_DROP * 100),
            "fares": scanned, "destinations": n_dest, "finds": finds, "history": history}
    tpl = (HERE / "site_template.html").read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (SITE_DIR / "index.html").write_text(tpl.replace("__DATA__", blob), encoding="utf-8")
    (SITE_DIR / "manifest.webmanifest").write_text(json.dumps({
        "name": "YUL Error Fare Radar", "short_name": "YUL Radar", "start_url": ".", "display": "standalone",
        "background_color": "#f3f5f8", "theme_color": "#1f4fd1",
        "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}), encoding="utf-8")
    (SITE_DIR / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><circle cx="16" cy="16" r="16" fill="#1f4fd1"/>'
        '<circle cx="16" cy="16" r="10" fill="none" stroke="#fff" stroke-opacity=".45" stroke-width="1.5"/><circle cx="16" cy="16" r="5" fill="none" stroke="#fff" stroke-opacity=".45" stroke-width="1.5"/>'
        '<path d="M16 16 L26 9" stroke="#fff" stroke-width="2" stroke-linecap="round"/><circle cx="22" cy="12" r="2.2" fill="#fff"/></svg>', encoding="utf-8")
    for size in (180, 192, 512):
        p = SITE_DIR / f"icon-{size}.png"
        if not p.exists():
            p.write_bytes(_png(size, _icon_pixel(size)))
    (SITE_DIR / ".nojekyll").write_text("")

# ---------------------------------------------------------------- main scan
def run(sample=False):
    if not sample and not TP_TOKEN:
        sys.exit("No TRAVELPAYOUTS_TOKEN found. Add it to the .env file, or run: python radar.py --sample")
    c = db(sample)
    log(f"Scanning round trips and one-way flights for {ORIGIN} ({'sample data' if sample else 'Travelpayouts'})")
    fares = fetch_sample(int(time.time()) // 60) if sample else fetch_travelpayouts()
    if not fares:
        log("No fares returned. Try again later."); return
    # trip length filter for round trips; drop duplicates of the same flight and price
    fares = [f for f in fares if f["kind"] != "rt" or
             MIN_TRIP_DAYS <= (date.fromisoformat(f["ret"]) - date.fromisoformat(f["depart"])).days <= MAX_TRIP_DAYS]
    uniq = {}
    for f in fares:
        uniq.setdefault((f["key"], f["depart"], f["ret"], f["price"]), f)
    fares = list(uniq.values())

    by_key = {}
    for f in fares:
        by_key.setdefault(f["key"], []).append(f)

    candidates = []
    for key, fs in by_key.items():
        by_month = {}
        for f in fs: by_month.setdefault(f["depart"][:7], []).append(f)
        for month, mfs in by_month.items():
            best = min(mfs, key=lambda f: f["price"])
            others = [f["price"] for f in fs if f is not best]
            base, src, n = baseline(c, key, month, others)
            if not base: continue
            drop = 1 - best["price"] / base
            if drop >= DEAL_DROP and (not MAX_PRICE or best["price"] <= MAX_PRICE):
                candidates.append((drop, best, base, src, n))

    # save this scan's observations after computing baselines, so a fare never counts toward its own normal price
    now = datetime.now(timezone.utc).isoformat()
    c.executemany("INSERT INTO obs VALUES(?,?,?,?,?,?,?,?)",
                  [(now, f["key"], f["depart"][:7], f["depart"], f["ret"], f["price"], f["airline"], f["stops"]) for f in fares])
    c.commit()

    candidates.sort(key=lambda x: -x[0])
    seen = set()   # keep only the best fare per route and direction
    candidates = [x for x in candidates if not (x[1]["key"] in seen or seen.add(x[1]["key"]))]
    kinds = {k: sum(1 for f in fares if f["kind"] == k) for k in ("rt", "out", "back")}
    log(f"{len(fares)} fares (round trip {kinds['rt']}, one way from YUL {kinds['out']}, one way to YUL {kinds['back']}), "
        f"{len(by_key)} routes, {len(candidates)} below the {DEAL_DROP*100:.0f}% threshold")

    found, checks = [], 0
    for drop, f, base, src, n in candidates[:20]:
        city = city_name(f["place"])
        cand = {"trip": f"{KIND_LABEL[f['kind']]}, {route_text(f['kind'], city)}", "kind": f["kind"],
                "city": city, "place": f["place"], "depart": f["depart"], "return": f["ret"], "price": f["price"],
                "normal_price": round(base), "drop_pct": round(drop * 100), "baseline_source": src, "baseline_samples": n,
                "airline": f["airline"], "stops": f["stops"], "currency": CURRENCY.upper(),
                "link": f["link"], "google_link": google_link(f)}
        if checks < MAX_VERIFY and SERP_KEY and not sample:
            cand["google_check"] = google_check(f); checks += 1
        cand["review"] = claude_review({k: v for k, v in cand.items() if k not in ("link", "google_link")})
        v = cand["review"].get("verdict", "strong_deal")
        if v not in ("likely_error", "strong_deal", "false_alarm"): v = cand["review"]["verdict"] = "strong_deal"
        found.append(cand)
        if v != "false_alarm":
            c.execute("INSERT INTO finds VALUES(?,?,?,?)", (now, f["key"], f["depart"][:7], json.dumps({
                "key": f["key"], "kind": f["kind"], "place": f["place"], "dest": f["place"], "city": city,
                "depart": f["depart"], "return": f["ret"], "price": f["price"],
                "normal_price": cand["normal_price"], "drop_pct": cand["drop_pct"], "airline": f["airline"], "stops": f["stops"],
                "verdict": v, "reason": cand["review"].get("reason", ""), "alert": cand["review"].get("alert", ""),
                "google_link": cand["google_link"], "link": f["link"]}, ensure_ascii=False)))
            c.commit()
        log(f"  {route_text(f['kind'], city):<32} ${f['price']:>6.0f}  normal ${base:>6.0f}  -{cand['drop_pct']}%  → {v} ({cand['review']['by']})")

        if v == "false_alarm" or already_alerted(c, f, v):
            continue
        text = cand["review"]["alert"].strip() + "\n\n" + cand["google_link"] + (("\n" + cand["link"]) if cand["link"] else "")
        if sample: text = "[SAMPLE DATA, NOT A REAL FARE]\n" + text
        sent = send_telegram(text) | send_email(f"{'Error fare' if v=='likely_error' else 'Deal'}: {route_text(f['kind'], city)} ${f['price']:.0f}", text)
        c.execute("INSERT INTO alerts VALUES(?,?,?,?,?,?)", (now, f["key"], f["depart"], f["ret"], f["price"], v)); c.commit()
        log("    alert sent" if sent else "    alert saved (no Telegram/email set up)")

    build_site(c, len(fares), len(by_key), sample, now)
    log("Phone page written to site/index.html")

def test_alert():
    msg = "✈️ YUL Error Fare Radar is connected. Alerts will arrive here."
    ok_t = send_telegram(msg); ok_e = send_email("YUL Error Fare Radar test", msg)
    print("Telegram:", "sent" if ok_t else "not set up or failed")
    print("Email:   ", "sent" if ok_e else "not set up or failed")

def telegram_id():
    if not TG_TOKEN:
        sys.exit("Add TELEGRAM_BOT_TOKEN to .env first, then send any message to your bot and run this again.")
    r = http_json(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates")
    ids = {str(u["message"]["chat"]["id"]): u["message"]["chat"].get("first_name", "") for u in r.get("result", []) if "message" in u}
    if not ids:
        print("No messages yet. Open your bot in Telegram, press Start, send 'hi', then run this again.")
    for i, n in ids.items():
        print(f"TELEGRAM_CHAT_ID={i}    ({n})")

if __name__ == "__main__":
    if "--telegram-id" in sys.argv: telegram_id()
    elif "--test-alert" in sys.argv: test_alert()
    else: run(sample="--sample" in sys.argv)
