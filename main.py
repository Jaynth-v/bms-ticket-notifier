"""
BMS + RCB Ticket Checker for Railway
Telegram only
"""

import os
import re
import json
import time
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
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
STATE_FILE = "state.json"


# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────
AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT", "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST", "🟠"),
    "3": ("AVAILABLE", "🟢"),
}

DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default": "AVAILABLE",
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

RCB_UNAVAILABLE_MARKERS = [
    "tickets not available",
    "await further announcements",
]


# ─────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────
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


@dataclass
class DateInfo:
    date_code: str
    status: str


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
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


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            if not isinstance(state, dict):
                return {"bms": {}, "rcb": {}}
            state.setdefault("bms", {})
            state.setdefault("rcb", {})
            return state
    except (FileNotFoundError, json.JSONDecodeError):
        return {"bms": {}, "rcb": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def send_telegram_text(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram skipped: token/chat id missing")
        return

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text[:4000],
            },
            timeout=20,
        )
        print("Telegram:", resp.status_code, resp.text)
    except requests.RequestException as e:
        print("Telegram failed:", e)


def strip_html(html: str):
    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    text = re.sub(r"(?is)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def cat_status_label(status: str):
    return AVAIL_STATUS_MAP.get(status, ("UNKNOWN", "⚪"))[0]


# ─────────────────────────────────────────────────────────────
# BMS
# ─────────────────────────────────────────────────────────────
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)


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
        print("BMS HTTP", resp.status_code, resp.text[:500])
    except requests.RequestException as e:
        print("BMS request failed:", e)

    return None


def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}

    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for c in row.get("components", []):
                        if "•" in c.get("text", ""):
                            info["language"] = c.get("text", "").strip()

    bs = data.get("data", {}).get("bottomSheetData", {})
    for w in bs.get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = d.get("text", info["name"])

    return info


def parse_dates(data):
    dates = []
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") != "horizontal-block-list":
            continue
        for item in w.get("data", []):
            style = item.get("styleId", "")
            dates.append(
                DateInfo(
                    date_code=item.get("id", ""),
                    status=DATE_STYLE_MAP.get(style, "UNKNOWN"),
                )
            )
    return dates


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
        if kws:
            if not any(k in s.venue_name.lower() for k in kws):
                continue

        if dates_set and s.date_code and s.date_code not in dates_set:
            continue

        if periods:
            try:
                tc = int(s.time_code)
            except ValueError:
                tc = 0

            ok = False
            for p in periods:
                if p in TIME_PERIODS:
                    lo, hi = TIME_PERIODS[p]
                    if lo <= tc < hi:
                        ok = True
                        break
            if not ok:
                continue

        result.append(s)

    return result


def build_bms_state(shows):
    show_state = {}

    for s in shows:
        cats = []
        available_found = False

        for c in s.categories:
            cats.append({
                "name": c.name,
                "price": c.price,
                "status": c.status,
            })
            if c.status != "0":
                available_found = True

        key = f"{s.venue_code}|{s.session_id}|{s.date_code}"
        show_state[key] = {
            "venue_name": s.venue_name,
            "time": s.time,
            "date_code": s.date_code,
            "screen_attr": s.screen_attr,
            "categories": cats,
            "has_available": available_found,
        }

    return {"shows": show_state}


def detect_bms_changes(old_state, new_state):
    changes = []

    old_shows = old_state.get("shows", {})
    new_shows = new_state.get("shows", {})

    for key, new_s in new_shows.items():
        old_s = old_shows.get(key)

        if not old_s:
            if new_s["has_available"]:
                changes.append(
                    f"🆕 NEW BMS SHOW AVAILABLE\n"
                    f"{new_s['venue_name']} — {new_s['time']} [{new_s['date_code']}]"
                )
            continue

        if (not old_s.get("has_available")) and new_s.get("has_available"):
            changes.append(
                f"🎟 BMS TICKETS AVAILABLE NOW\n"
                f"{new_s['venue_name']} — {new_s['time']} [{new_s['date_code']}]"
            )

    return changes


def make_bms_message(movie_info, shows, changes):
    lines = [f"🎬 {movie_info.get('name', 'Movie')}", BMS_URL, ""]

    if changes:
        lines.append("Changes:")
        for c in changes:
            lines.append(c)
        lines.append("")

    lines.append("Current matching shows:")

    count = 0
    for s in shows:
        available_cats = []
        for c in s.categories:
            if c.status != "0":
                available_cats.append(
                    f"{c.name} Rs.{c.price} ({cat_status_label(c.status)})"
                )

        if available_cats:
            screen = f" [{s.screen_attr}]" if s.screen_attr else ""
            lines.append(f"{s.venue_name} — {s.time}{screen}")
            lines.append(" | ".join(available_cats))
            lines.append("")
            count += 1

        if count >= 15:
            break

    if count == 0:
        lines.append("No available categories found right now.")

    return "\n".join(lines)


def run_bms_check(state):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BMS check")

    parsed = parse_bms_url(BMS_URL)
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

    all_shows = []
    movie_info = {"name": "Unknown", "language": ""}

    for dc in date_list:
        data = fetch_bms(event_code, dc, region_code, region_slug_r, lat, lon, geohash)
        if not data:
            continue

        if movie_info["name"] == "Unknown":
            movie_info = parse_movie_info(data)

        all_shows.extend(parse_shows(data))

    if not all_shows:
        print("No BMS shows found")
        return state

    filtered = filter_shows(all_shows, BMS_THEATRE, BMS_TIME, BMS_DATES)
    print("Filtered BMS shows:", len(filtered))

    new_bms_state = build_bms_state(filtered)
    old_bms_state = state.get("bms", {})

    changes = detect_bms_changes(old_bms_state, new_bms_state) if old_bms_state else []

    state["bms"] = new_bms_state
    save_state(state)

    if changes:
        print("BMS changes detected:", len(changes))
        send_telegram_text(make_bms_message(movie_info, filtered, changes))
    else:
        print("No BMS changes")

    return state


# ─────────────────────────────────────────────────────────────
# RCB
# ─────────────────────────────────────────────────────────────
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


def build_rcb_state(html: str):
    text = strip_html(html).lower()

    unavailable = all(marker in text for marker in RCB_UNAVAILABLE_MARKERS)
    status = "unavailable" if unavailable else "available"

    return {
        "status": status,
    }


def detect_rcb_changes(old_state, new_state):
    old_status = old_state.get("status")
    new_status = new_state.get("status")

    if old_status == "unavailable" and new_status == "available":
        return [f"🏏 RCB TICKETS MAY BE LIVE\n{RCB_URL}"]

    return []


def run_rcb_check(state):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] RCB check")

    if not RCB_URL:
        print("RCB_URL missing")
        return state

    try:
        html = fetch_rcb_page()
        new_rcb_state = build_rcb_state(html)
    except Exception as e:
        print("RCB failed:", e)
        return state

    old_rcb_state = state.get("rcb", {})
    changes = detect_rcb_changes(old_rcb_state, new_rcb_state) if old_rcb_state else []

    state["rcb"] = new_rcb_state
    save_state(state)

    print("RCB status:", new_rcb_state["status"])

    if changes:
        send_telegram_text(changes[0])
    else:
        print("No RCB changes")

    return state


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    state = load_state()
    state = run_bms_check(state)
    state = run_rcb_check(state)
    print("Done\n")


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            print("Fatal error:", e)
        time.sleep(CHECK_INTERVAL)
