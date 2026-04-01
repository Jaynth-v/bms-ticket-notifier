"""
BMS + RCB Ticket Checker for Railway

Environment variables:
- BMS_URL
- BMS_DATES
- BMS_THEATRE
- BMS_TIME
- RCB_URL
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
- RESEND_API_KEY (optional)
- RESEND_TO_EMAIL (optional)
- RESEND_FROM_EMAIL (optional)
- CHECK_INTERVAL
"""

import os
import re
import sys
import json
import time
from html import escape
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests


# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
BMS_URL = os.getenv("BMS_URL", "").strip()
if not BMS_URL:
    raise ValueError("BMS_URL is not set in environment variables")

CONFIG = {
    "url": BMS_URL,
    "dates": os.getenv("BMS_DATES", "").strip(),
    "theatre": os.getenv("BMS_THEATRE", "").strip(),
    "time_period": os.getenv("BMS_TIME", "").strip(),
}

RCB_URL = os.getenv("RCB_URL", "https://shop.royalchallengers.com/ticket").strip()

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "").strip()
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "aviiciii@resend.dev").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "20"))
STATE_FILE = "bms_state.json"


# ──────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────
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


# ──────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ──────────────────────────────────────────────────────────────────────
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


# ──────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────
def parse_bms_url(url: str):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    result = {"event_code": None, "date_code": None, "region_slug": None}

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
            if "bms" not in state:
                state["bms"] = {}
            if "rcb" not in state:
                state["rcb"] = {}
            return state
    except (FileNotFoundError, json.JSONDecodeError):
        return {"bms": {}, "rcb": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _cat_status_label(status):
    return AVAIL_STATUS_MAP.get(status, ("UNKNOWN", ""))[0]


def send_telegram_text(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Skipping Telegram: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing")
        return

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text[:4000],
            },
            timeout=15,
        )
        if resp.status_code == 200:
            print("Telegram sent")
        else:
            print(f"Telegram failed: {resp.status_code} {resp.text}")
    except requests.RequestException as e:
        print(f"Telegram failed: {e}")


def send_email(subject, changes, shows, movie_info):
    if not RESEND_API_KEY or not RESEND_TO_EMAIL:
        print("Skipping email: RESEND_API_KEY or RESEND_TO_EMAIL missing")
        return

    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    movie_name = movie_info.get("name", "Movie")

    changes_html = ""
    if changes:
        rows = "".join(
            f'<li style="padding:3px 0;font-size:14px;">{escape(c)}</li>'
            for c in changes
        )
        changes_html = f"""
        <h3 style="margin:0 0 8px 0;font-size:15px;font-weight:bold;color:#333;">
            Changes Detected
        </h3>
        <ul style="margin:0 0 20px 0;padding-left:20px;line-height:1.6;color:#333;">
            {rows}
        </ul>"""

    venue_groups = {}
    for s in shows:
        venue_groups.setdefault(s.venue_name, []).append(s)

    shows_html = ""
    for vname, vshows in venue_groups.items():
        show_rows = ""
        for s in vshows:
            cats = " | ".join(
                f"{escape(c.name)} Rs.{escape(c.price)} ({_cat_status_label(c.status)})"
                for c in s.categories
            )
            fmt = f" [{escape(s.screen_attr)}]" if s.screen_attr else ""
            show_rows += (
                f"<tr>"
                f"<td style='padding:5px 8px;border-bottom:1px solid #ddd;font-size:13px;vertical-align:top;'>{escape(s.time)}{fmt}</td>"
                f"<td style='padding:5px 8px;border-bottom:1px solid #ddd;font-size:13px;vertical-align:top;'>{cats}</td>"
                f"</tr>"
            )

        shows_html += f"""
        <p style="margin:14px 0 4px 0;font-size:14px;font-weight:bold;color:#333;">
            {escape(vname)}
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <tr style="background:#f5f5f5;">
                <th style="padding:5px 8px;text-align:left;border-bottom:1px solid #ddd;font-weight:bold;">Time</th>
                <th style="padding:5px 8px;text-align:left;border-bottom:1px solid #ddd;font-weight:bold;">Categories</th>
            </tr>
            {show_rows}
        </table>"""

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#333;background:#fff;">
    <h2 style="margin:0 0 4px 0;font-size:18px;color:#111;">BMS Alert: {escape(movie_name)}</h2>
    <p style="margin:0 0 20px 0;font-size:13px;color:#666;">{escape(now_str)}</p>
    <hr style="border:none;border-top:1px solid #ddd;margin:0 0 20px 0;">
    {changes_html}
    <h3 style="margin:0 0 8px 0;font-size:15px;font-weight:bold;color:#333;">Current Showtimes</h3>
    {shows_html}
</body>
</html>"""

    plain_lines = [subject, "", f"Checked at: {now_str}", ""]
    if changes:
        plain_lines.append("Changes Detected:")
        plain_lines.extend(f"  - {c}" for c in changes)
        plain_lines.append("")
    plain_lines.append("Current Showtimes:")
    for vname, vshows in venue_groups.items():
        plain_lines.append(f"\n{vname}")
        for s in vshows:
            cats = " | ".join(
                f"{c.name} Rs.{c.price} ({_cat_status_label(c.status)})"
                for c in s.categories
            )
            fmt = f" [{s.screen_attr}]" if s.screen_attr else ""
            plain_lines.append(f"  {s.time}{fmt} - {cats}")

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": RESEND_FROM_EMAIL,
                "to": [RESEND_TO_EMAIL],
                "subject": subject,
                "text": "\n".join(plain_lines),
                "html": html,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print("Email sent")
        else:
            print(f"Email failed: {resp.status_code} {resp.text}")
    except requests.RequestException as e:
        print(f"Email failed: {e}")


# ──────────────────────────────────────────────────────────────────────
# BMS FETCH/PARSE
# ──────────────────────────────────────────────────────────────────────
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)


def fetch_bms(event_code, date_code, region_code, region_slug, lat, lon, geohash):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
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
        resp = requests.get(API_URL, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        print(f"BMS HTTP {resp.status_code}")
    except requests.RequestException as e:
        print(f"BMS request failed: {e}")
    return None


def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for c in row.get("components", []):
                        if "•" in c.get("text", ""):
                            info["language"] = c["text"].strip()

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
                vname = addl.get("venueName", "Unknown")
                vcode = addl.get("venueCode", "")

                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})
                    date_code = str(
                        sa.get("showDateCode", "") or sa.get("dateCode", "")
                    ).strip()

                    if not date_code and re.match(r"^\d{8}", sa.get("cutOffDateTime", "")):
                        date_code = sa["cutOffDateTime"][:8]

                    show = ShowInfo(
                        venue_code=vcode,
                        venue_name=vname,
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
                                price=cat.get("curPrice", "0"),
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
            name_lower = s.venue_name.lower()
            if not any(k in name_lower for k in kws):
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


def build_bms_state(shows, dates):
    show_state = {}
    for s in shows:
        for c in s.categories:
            key = f"{s.venue_code}|{s.session_id}|{s.date_code}|{c.name}"
            show_state[key] = {
                "venue": s.venue_name,
                "time": s.time,
                "date": s.date_code,
                "cat": c.name,
                "price": c.price,
                "status": c.status,
            }

    date_state = {d.date_code: d.status for d in dates}
    return {"shows": show_state, "dates": date_state}


def detect_bms_changes(old_state, new_state):
    changes = []

    old_dates = old_state.get("dates", {})
    new_dates = new_state.get("dates", {})
    for dc, status in new_dates.items():
        old_status = old_dates.get(dc)
        if old_status == "NOT_OPEN" and status in ("BOOKABLE", "AVAILABLE"):
            changes.append(f"📅 NEW DATE OPENED: {dc}")

    old_shows = old_state.get("shows", {})
    new_shows = new_state.get("shows", {})

    for key in set(new_shows) - set(old_shows):
        s = new_shows[key]
        changes.append(
            f"🆕 NEW: {s['venue']} {s['time']} [{s['date']}] — {s['cat']} ₹{s['price']}"
        )

    for key, new_s in new_shows.items():
        old_s = old_shows.get(key)
        if old_s and old_s["status"] == "0" and new_s["status"] != "0":
            lbl, ico = AVAIL_STATUS_MAP.get(new_s["status"], ("UNKNOWN", "⚪"))
            changes.append(
                f"{ico} BACK: {new_s['venue']} {new_s['time']} [{new_s['date']}] — {new_s['cat']} → {lbl}"
            )

    return changes


def send_bms_telegram(changes, shows, movie_info):
    movie_name = movie_info.get("name", "Movie")

    lines = [f"🎟 {movie_name}", ""]
    if changes:
        lines.append("Changes detected:")
        for c in changes:
            lines.append(f"• {c}")
        lines.append("")

    lines.append("Current shows:")
    for s in shows[:20]:
        cats = " | ".join(
            f"{c.name} Rs.{c.price} ({_cat_status_label(c.status)})"
            for c in s.categories
        )
        screen = f" [{s.screen_attr}]" if s.screen_attr else ""
        lines.append(f"{s.venue_name} — {s.time}{screen}")
        lines.append(cats)
        lines.append("")

    send_telegram_text("\n".join(lines))


# ──────────────────────────────────────────────────────────────────────
# RCB CHECK
# ──────────────────────────────────────────────────────────────────────
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


def strip_html(html: str):
    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    text = re.sub(r"(?is)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_rcb_state(html: str):
    text = strip_html(html).lower()
    unavailable = all(marker in text for marker in RCB_UNAVAILABLE_MARKERS)
    status = "unavailable" if unavailable else "changed"
    return {
        "status": status,
        "snippet": text[:1000],
    }


def detect_rcb_changes(old_state, new_state):
    if not old_state:
        return []

    old_status = old_state.get("status")
    new_status = new_state.get("status")

    if old_status != new_status:
        if new_status != "unavailable":
            return [f"🏏 RCB ticket page changed: tickets may be live\n{RCB_URL}"]
        return [f"🏏 RCB page changed back to unavailable\n{RCB_URL}"]

    return []


# ──────────────────────────────────────────────────────────────────────
# RUNNERS
# ──────────────────────────────────────────────────────────────────────
def run_bms_check(state):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BMS Ticket Checker")

    parsed = parse_bms_url(CONFIG["url"])
    event_code = parsed["event_code"]
    region_slug = parsed["region_slug"]
    url_date = parsed.get("date_code", "")

    if not event_code or not region_slug:
        print("Invalid BMS_URL. Could not extract event/region.")
        return state

    region_code, region_slug_r, lat, lon, geohash = resolve_region(region_slug)

    raw_dates = CONFIG["dates"]
    if raw_dates:
        date_list = [d.strip() for d in raw_dates.split(",") if d.strip()]
    elif url_date:
        date_list = [url_date]
    else:
        date_list = [""]

    print(f"Event: {event_code} Region: {region_code} Dates: {date_list}")

    all_shows = []
    all_dates = []
    movie_info = {"name": "Unknown", "language": ""}

    for dc in date_list:
        data = fetch_bms(event_code, dc, region_code, region_slug_r, lat, lon, geohash)
        if not data:
            print(f"No data for date {dc or '(default)'}")
            continue

        if movie_info["name"] == "Unknown":
            movie_info = parse_movie_info(data)

        all_dates.extend(parse_dates(data))
        all_shows.extend(parse_shows(data))

    if not all_shows:
        print("No BMS showtimes found.")
        return state

    print(f"Movie: {movie_info['name']} {movie_info['language']}")

    filtered = filter_shows(
        all_shows,
        CONFIG["theatre"],
        CONFIG["time_period"],
        CONFIG["dates"],
    )

    print(f"{len(filtered)} BMS showtime(s) after filters")

    new_bms_state = build_bms_state(filtered, all_dates)
    old_bms_state = state.get("bms", {})
    changes = detect_bms_changes(old_bms_state, new_bms_state) if old_bms_state else []

    state["bms"] = new_bms_state
    save_state(state)

    if changes:
        print(f"{len(changes)} BMS change(s) detected")
        for c in changes:
            print(c)

        subject = f"BMS Alert: {movie_info['name']} - {len(changes)} change(s)"
        send_email(subject, changes, filtered, movie_info)
        send_bms_telegram(changes, filtered, movie_info)
    else:
        print("No BMS changes since last check.")

    return state


def run_rcb_check(state):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] RCB Ticket Checker")

    if not RCB_URL:
        print("RCB_URL missing, skipping RCB")
        return state

    try:
        html = fetch_rcb_page()
        new_rcb_state = build_rcb_state(html)
    except requests.RequestException as e:
        print(f"RCB request failed: {e}")
        return state
    except Exception as e:
        print(f"RCB parsing failed: {e}")
        return state

    old_rcb_state = state.get("rcb", {})
    changes = detect_rcb_changes(old_rcb_state, new_rcb_state)

    state["rcb"] = new_rcb_state
    save_state(state)

    print(f"RCB status: {new_rcb_state['status']}")

    if changes:
        for c in changes:
            print(c)
        send_telegram_text(changes[0])
    else:
        print("No RCB changes since last check.")

    return state


def main():
    state = load_state()
    state = run_bms_check(state)
    state = run_rcb_check(state)
    print("Done.\n")


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            print(f"Fatal error: {e}")
        time.sleep(CHECK_INTERVAL)
