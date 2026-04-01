import os
import re
import sys
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

# ─────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────
CONFIG = {
    "url": os.getenv("BMS_URL"),
    "dates": os.getenv("BMS_DATES", ""),
    "theatre": os.getenv("BMS_THEATRE", ""),
    "time_period": os.getenv("BMS_TIME", ""),
}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ─────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────
TIME_PERIODS = {
    "morning": (600, 1200),
    "afternoon": (1200, 1600),
    "evening": (1600, 1900),
    "night": (1900, 2400),
}

REGION_MAP = {
    "bengaluru": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "bangalore": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
}

# ─────────────────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────────────────
@dataclass
class ShowInfo:
    venue_name: str
    time: str
    time_code: str
    date_code: str


# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────
def parse_bms_url(url):
    parts = urlparse(url).path.split("/")
    return {
        "event_code": next((p for p in parts if p.startswith("ET")), None),
        "region_slug": parts[2] if len(parts) > 2 else None,
    }


def resolve_region(slug):
    return REGION_MAP.get(slug, ("BANG", "bengaluru", "12.972", "77.594", "tdr"))


# ─────────────────────────────────────────────────────────
# FETCH
# ─────────────────────────────────────────────────────────
def fetch(event_code, date_code, region_code, region_slug, lat, lon, geohash):
    url = "https://in.bookmyshow.com/api/movies-data/v4/showtimes-by-event/primary-dynamic"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
    }

    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "lat": lat,
        "lon": lon,
    }

    r = requests.get(url, headers=headers, params=params)
    return r.json() if r.status_code == 200 else None


# ─────────────────────────────────────────────────────────
# PARSE
# ─────────────────────────────────────────────────────────
def parse_shows(data):
    shows = []

    for w in data.get("data", {}).get("showtimeWidgets", []):
        for g in w.get("data", []):
            for card in g.get("data", []):
                vname = card.get("additionalData", {}).get("venueName", "")

                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})

                    shows.append(
                        ShowInfo(
                            venue_name=vname,
                            time=st.get("title", ""),
                            time_code=sa.get("showTimeCode", "0"),
                            date_code=sa.get("showDateCode", ""),
                        )
                    )

    return shows


# ─────────────────────────────────────────────────────────
# FILTER
# ─────────────────────────────────────────────────────────
def filter_shows(shows):
    result = []

    kws = [k.lower() for k in CONFIG["theatre"].split(",") if k]
    periods = [p.lower() for p in CONFIG["time_period"].split(",") if p]
    dates = set(CONFIG["dates"].split(",")) if CONFIG["dates"] else None

    for s in shows:
        if kws and not any(k in s.venue_name.lower() for k in kws):
            continue

        if dates and s.date_code not in dates:
            continue

        if periods:
            tc = int(s.time_code)
            if not any(lo <= tc < hi for p in periods for lo, hi in [TIME_PERIODS[p]]):
                continue

        result.append(s)

    return result


# ─────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────
def send_telegram(shows):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured")
        return

    if not shows:
        print("No matching shows")
        return

    lines = ["🎟 MATCH FOUND!", ""]

    for s in shows[:20]:
        lines.append(f"{s.venue_name} — {s.time} [{s.date_code}]")

    text = "\n".join(lines)

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
    )

    print("Telegram sent")


# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────
def main():
    parsed = parse_bms_url(CONFIG["url"])
    region_code, region_slug, lat, lon, geohash = resolve_region(parsed["region_slug"])

    data = fetch(parsed["event_code"], "", region_code, region_slug, lat, lon, geohash)

    if not data:
        print("No data")
        return

    shows = parse_shows(data)
    filtered = filter_shows(shows)

    print(f"Found {len(filtered)} matching shows")

    send_telegram(filtered)


if __name__ == "__main__":
    main()
