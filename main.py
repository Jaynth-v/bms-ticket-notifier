"""
BMS tracker for Railway

What it tracks:
- theatre name
- show time
- date

What it ignores:
- seat category changes
- price changes
- availability text changes

Behavior:
- sends Telegram only when a NEW matching showtime appears
- does not spam the same showtime repeatedly
- if a showtime disappears and later comes back, it can alert again
"""

import os
import re
import json
import time
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BMS_DATES = os.getenv("BMS_DATES", "").strip()
BMS_THEATRE = os.getenv("BMS_THEATRE", "").strip()
BMS_TIME = os.getenv("BMS_TIME", "").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
STATE_FILE = "bms_state.json"


# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)

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
                return {"seen_show_keys": []}
            data.setdefault("seen_show_keys", [])
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen_show_keys": []}


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


def parse_bms_url(url: str):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")

    result = {
        "event_code": None,
        "region_slug": None,
    }

    for p in parts:
        if re.match(r"^ET\d{8,}$", p):
            result["event_code"] = p

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


def get_bms_date_list():
    if BMS_DATES:
        return [d.strip() for d in BMS_DATES.split(",") if d.strip()]
    return [""]


def show_key(show: ShowInfo):
    return f"{show.venue_name}|{show.time}|{show.date_code}"


# ─────────────────────────────────────────────
# BMS FETCH
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


# ─────────────────────────────────────────────
# FILTERING
# ─────────────────────────────────────────────
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


def keep_only_shows_that_exist(shows):
    """
    We only care whether the showtime exists.
    We ignore category/price/status changes.
    """
    unique = {}
    for s in shows:
        key = show_key(s)
        if key not in unique:
            unique[key] = ShowInfo(
                venue_code=s.venue_code,
                venue_name=s.venue_name,
                session_id=s.session_id,
                date_code=s.date_code,
                time=s.time,
                time_code=s.time_code,
                screen_attr=s.screen_attr,
                categories=[],
            )
    return list(unique.values())


def make_bms_message(movie_name, shows):
    lines = [
        f"🎬 {movie_name}",
        BMS_URL,
        "",
        "NEW SHOWTIMES FOUND",
        f"Date filter: {BMS_DATES or 'default'}",
        f"Time filter: {BMS_TIME or 'all'}",
        f"Theatre filter: {BMS_THEATRE or 'all'}",
        "",
    ]

    count = 0
    for s in shows:
        screen = f" [{s.screen_attr}]" if s.screen_attr else ""
        lines.append(f"{s.venue_name} — {s.time}{screen} [{s.date_code}]")
        count += 1
        if count >= 15:
            break

    return "\n".join(lines)


# ─────────────────────────────────────────────
# MAIN CHECK
# ─────────────────────────────────────────────
def run_bms_check(state):
    print(f"[{now_str()}] BMS check started")
    print("BMS_DATES:", BMS_DATES)
    print("BMS_TIME:", BMS_TIME)
    print("BMS_THEATRE:", BMS_THEATRE)

    parsed = parse_bms_url(BMS_URL)
    print("Parsed BMS URL:", parsed)

    event_code = parsed["event_code"]
    region_slug = parsed["region_slug"]

    if not event_code or not region_slug:
        print("Invalid BMS_URL")
        return state

    region_code, region_slug_r, lat, lon, geohash = resolve_region(region_slug)
    date_list = get_bms_date_list()

    print("BMS date list:", date_list)

    all_shows = []
    movie_name = "Unknown Movie"

    for dc in date_list:
        data = fetch_bms(event_code, dc, region_code, region_slug_r, lat, lon, geohash)
        if not data:
            print("No BMS data for:", dc if dc else "(default)")
            continue

        if movie_name == "Unknown Movie":
            movie_name = parse_movie_info(data).get("name", movie_name)

        fetched = parse_shows(data)
        print(f"BMS shows fetched for {dc if dc else '(default)'}: {len(fetched)}")
        all_shows.extend(fetched)

    print("BMS total shows fetched:", len(all_shows))

    filtered = filter_shows(all_shows, BMS_THEATRE, BMS_TIME, BMS_DATES)
    print("BMS filtered shows:", len(filtered))

    final_shows = keep_only_shows_that_exist(filtered)
    print("BMS unique matching showtimes:", len(final_shows))

    seen = set(state.get("seen_show_keys", []))
    current = {show_key(s) for s in final_shows}
    new_shows = [s for s in final_shows if show_key(s) not in seen]

    if new_shows:
        print("New showtimes found -> sending Telegram")
        send_telegram_text(make_bms_message(movie_name, new_shows))
    else:
        print("No new showtimes to notify")

    state["seen_show_keys"] = list(current)
    save_state(state)
    return state


def main():
    print(f"\n==== Tracker cycle at {now_str()} ====")
    state = load_state()
    state = run_bms_check(state)
    print("Cycle done")


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            print("Fatal error:", str(e))

        print(f"Sleeping {CHECK_INTERVAL} seconds...\n")
        time.sleep(CHECK_INTERVAL)
