"""
BMS + RCB tracker for Railway
Sends Telegram whenever page state changes
"""

import os
import re
import json
import time
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

import requests


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
BMS_URL = os.getenv(
    "BMS_URL",
    "https://in.bookmyshow.com/movies/bengaluru/project-hail-mary/buytickets/ET00481564/20260402"
).strip()

RCB_URL = os.getenv(
    "RCB_URL",
    "https://shop.royalchallengers.com/ticket"
).strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BMS_DATES = os.getenv("BMS_DATES", "").strip()
BMS_THEATRE = os.getenv("BMS_THEATRE", "").strip()
BMS_TIME = os.getenv("BMS_TIME", "").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
STATE_FILE = "tracker_state.json"


# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)

AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT", "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST", "🟠"),
    "3": ("AVAILABLE", "🟢"),
}

TIME_PERIODS = {
    "morning": (600, 1200),
    "afternoon": (1200, 1600),
    "evening": (1600, 1900),
    "night": (1900, 2400),
}

REGION_MAP = {
    "chennai": ("CHEN", "chennai", "13.056", "80.206", "tf3"),
    "mumbai": ("MUMBAI", "mumbai", "19.076", "72.878", "te7"),
    "delhi-ncr": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "delhi": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "bengaluru": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "bangalore": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "hyderabad": ("HYD", "hyderabad", "17.385", "78.487", "tep"),
    "kolkata": ("KOLK", "kolkata", "22.573", "88.364", "tun"),
    "pune": ("PUNE", "pune", "18.520", "73.856", "te2"),
    "kochi": ("KOCH", "kochi", "9.932", "76.267", "t9z"),
}


# ─────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────
@dataclass
class CatInfo:
    name: str
    price: str
    status: str


@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    categories: list[CatInfo] = field(default_factory=list)


# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"bms_hash": "", "rcb_hash": ""}
            data.setdefault("bms_hash", "")
            data.setdefault("rcb_hash", "")
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"bms_hash": "", "rcb_hash": ""}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def send_telegram_text(text: str):
    print("TELEGRAM TOKEN SET:", bool(TELEGRAM_BOT_TOKEN))
    print("TELEGRAM CHAT ID:", TELEGRAM_CHAT_ID if TELEGRAM_CHAT_ID else "MISSING")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram skipped: token/chat id missing")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4000],
        "disable_web_page_preview": False,
    }

    try:
        r = requests.post(url, json=payload, timeout=20)
        print("Telegram status:", r.status_code)
        print("Telegram response:", r.text[:1000])
    except requests.RequestException as e:
        print("Telegram error:", str(e))


def strip_html(html: str):
    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    text = re.sub(r"(?is)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def md5_text(text: str):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def cat_status_label(status: str):
    return AVAIL_STATUS_MAP.get(status, ("UNKNOWN", "⚪"))[0]


def parse_bms_url(url: str):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")

    result = {
        "event_code": None,
        "date_code": None,
        "region_slug": None,
    }

    for p in parts:
        if re.match(r"^ET\d{8,}$", p):
            result["event_code"] = p
        elif re.match(r"^\d{8}$", p):
            result["date_code"] = p

    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            result["region_slug"] = parts[idx + 1]

    return result


def resolve_region(slug: str):
    key = (slug or "").lower().strip()
    if key in REGION_MAP:
        return REGION_MAP[key]
    return (key.upper()[:6], key, "0", "0", "")


# ─────────────────────────────────────────────
# BMS
# ─────────────────────────────────────────────
def fetch_bms(event_code, date_code, region_code, region_slug, lat, lon, geohash):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": f"https://in.bookmyshow.com/movies/{region_slug}/buytickets/{event_code}/",
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
        "x-location-selection": "manual",
        "x-lsid": "",
    }

    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "",
        "lsId": "",
        "subCode": "",
        "lat": lat,
        "lon": lon,
    }

    try:
        resp = requests.get(API_URL, headers=headers, params=params, timeout=20)
        if resp.status_code == 200:
            return resp.json()
        print("BMS HTTP:", resp.status_code)
        print(resp.text[:500])
    except requests.RequestException as e:
        print("BMS request failed:", str(e))

    return None


def parse_movie_info(data):
    info = {"name": "Unknown Movie"}

    bs = data.get("data", {}).get("bottomSheetData", {})
    for w in bs.get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = d.get("text", info["name"])

    return info


def parse_shows(data):
    shows = []

    for w in data.get("data", {}).get("showtimeWidgets", []):
        if w.get("type") != "groupList":
            continue

        for g in w.get("data", []):
            if g.get("type") != "venueGroup":
                continue

            for card in g.get("data", []):
                if card.get("type") != "venue-card":
                    continue

                addl = card.get("additionalData", {})
                venue_name = addl.get("venueName", "Unknown")
                venue_code = addl.get("venueCode", "")

                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})
                    date_code = str(
                        sa.get("showDateCode", "") or sa.get("dateCode", "")
                    ).strip()

                    if not date_code and re.match(r"^\d{8}", sa.get("cutOffDateTime", "")):
                        date_code = sa["cutOffDateTime"][:8]

                    show = ShowInfo(
                        venue_code=venue_code,
                        venue_name=venue_name,
                        session_id=sa.get("sessionId", ""),
                        date_code=date_code,
                        time=st.get("title", ""),
                        time_code=sa.get("showTimeCode", ""),
                        screen_attr=(st.get("screenAttr", "") or sa.get("attributes", "")),
                    )

                    for cat in sa.get("categories", []):
                        show.categories.append(
                            CatInfo(
                                name=cat.get("priceDesc", ""),
                                price=str(cat.get("curPrice", "0")),
                                status=str(cat.get("availStatus", "")),
                            )
                        )

                    shows.append(show)

    return shows


def filter_shows(shows, theatre_filter, time_periods, date_codes):
    result = []

    kws = [k.strip().lower() for k in theatre_filter.split(",") if k.strip()] if theatre_filter else []
    periods = [p.strip().lower() for p in time_periods.split(",") if p.strip()] if time_periods else []
    dates_set = set(d.strip() for d in date_codes.split(",") if d.strip()) if date_codes else set()

    for s in shows:
        if kws and not any(k in s.venue_name.lower() for k in kws):
            continue

        if dates_set and s.date_code and s.date_code not in dates_set:
            continue

        if periods:
            try:
                tc = int(s.time_code)
            except ValueError:
                tc = 0

            matched = False
            for p in periods:
                if p in TIME_PERIODS:
                    lo, hi = TIME_PERIODS[p]
                    if lo <= tc < hi:
                        matched = True
                        break
            if not matched:
                continue

        result.append(s)

    return result


def build_bms_signature(shows):
    rows = []

    for s in shows:
        for c in s.categories:
            rows.append(
                f"{s.venue_name}|{s.time}|{s.date_code}|{s.screen_attr}|{c.name}|{c.price}|{c.status}"
            )

    rows.sort()
    return "\n".join(rows)


def make_bms_message(movie_name, shows):
    lines = [
        f"🎬 {movie_name}",
        BMS_URL,
        "",
        "BMS CHANGE DETECTED",
        ""
    ]

    count = 0
    for s in shows:
        cats = " | ".join(
            f"{c.name} Rs.{c.price} ({cat_status_label(c.status)})"
            for c in s.categories
        )

        screen = f" [{s.screen_attr}]" if s.screen_attr else ""
        lines.append(f"{s.venue_name} — {s.time}{screen}")
        lines.append(cats)
        lines.append("")

        count += 1
        if count >= 10:
            break

    if count == 0:
        lines.append("No matching shows found.")

    return "\n".join(lines)


def run_bms_check(state):
    print(f"[{now_str()}] BMS check started")

    parsed = parse_bms_url(BMS_URL)
    print("Parsed BMS URL:", parsed)

    event_code = parsed["event_code"]
    region_slug = parsed["region_slug"]
    url_date = parsed.get("date_code", "")

    if not event_code or not region_slug:
        print("Invalid BMS_URL")
        return state

    region_code, region_slug_r, lat, lon, geohash = resolve_region(region_slug)

    if BMS_DATES:
        date_list = [d.strip() for d in BMS_DATES.split(",") if d.strip()]
    elif url_date:
        date_list = [url_date]
    else:
        date_list = [""]

    print("BMS date list:", date_list)

    all_shows = []
    movie_name = "Unknown Movie"

    for dc in date_list:
        data = fetch_bms(event_code, dc, region_code, region_slug_r, lat, lon, geohash)
        if not data:
            print("No BMS data for:", dc)
            continue

        if movie_name == "Unknown Movie":
            movie_name = parse_movie_info(data).get("name", movie_name)

        fetched = parse_shows(data)
        print(f"BMS shows fetched for {dc}: {len(fetched)}")
        all_shows.extend(fetched)

    print("BMS total shows fetched:", len(all_shows))

    filtered = filter_shows(all_shows, BMS_THEATRE, BMS_TIME, BMS_DATES)
    print("BMS filtered shows:", len(filtered))

    signature = build_bms_signature(filtered)
    new_hash = md5_text(signature)
    old_hash = state.get("bms_hash", "")

    print("BMS old hash:", old_hash)
    print("BMS new hash:", new_hash)

    if not old_hash:
        print("BMS first run. Saving baseline only.")
        state["bms_hash"] = new_hash
        save_state(state)
        return state

    if new_hash != old_hash:
        print("BMS changed -> sending Telegram")
        send_telegram_text(make_bms_message(movie_name, filtered))
        state["bms_hash"] = new_hash
        save_state(state)
    else:
        print("No BMS change")

    return state


# ─────────────────────────────────────────────
# RCB
# ─────────────────────────────────────────────
def fetch_rcb_page():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-IN,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    resp = requests.get(RCB_URL, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.text


def build_rcb_signature(html: str):
    text = strip_html(html).lower()

    keep_words = []
    for part in text.split():
        if len(part) > 2:
            keep_words.append(part)

    cleaned = " ".join(keep_words[:2000])
    return cleaned


def make_rcb_message():
    return f"🏏 RCB PAGE CHANGE DETECTED\n{RCB_URL}"


def run_rcb_check(state):
    print(f"[{now_str()}] RCB check started")

    if not RCB_URL:
        print("RCB_URL missing")
        return state

    try:
        html = fetch_rcb_page()
    except Exception as e:
        print("RCB fetch failed:", str(e))
        return state

    signature = build_rcb_signature(html)
    new_hash = md5_text(signature)
    old_hash = state.get("rcb_hash", "")

    print("RCB old hash:", old_hash)
    print("RCB new hash:", new_hash)

    if not old_hash:
        print("RCB first run. Saving baseline only.")
        state["rcb_hash"] = new_hash
        save_state(state)
        return state

    if new_hash != old_hash:
        print("RCB changed -> sending Telegram")
        send_telegram_text(make_rcb_message())
        state["rcb_hash"] = new_hash
        save_state(state)
    else:
        print("No RCB change")

    return state


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print(f"\n==== Tracker cycle at {now_str()} ====")
    state = load_state()
    state = run_bms_check(state)
    state = run_rcb_check(state)
    print("Cycle done")


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            print("Fatal error:", str(e))

        print(f"Sleeping {CHECK_INTERVAL} seconds...\n")
        time.sleep(CHECK_INTERVAL)
