#!/usr/bin/env python3
"""
MedSky seat availability monitor — two directions.

Outbound (MJI -> MXP): sends a Telegram alert when a flight goes from
sold-out to available. Return (MXP -> MJI): tracked and shown on the
board, but does NOT trigger Telegram alerts (display-only).

Both directions are recorded in the movement history log.
"""

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

# Directions to watch.
#   alert=True  -> a sold-out -> available transition sends Telegram
#   alert=False -> tracked and shown on the board, but no Telegram
LEGS = [
    {"from": "MJI", "to": "MXP", "alert": True},   # outbound — alerts
    {"from": "MXP", "to": "MJI", "alert": False},  # return — display only
]
OFFICE = int(os.environ.get("MEDSKY_OFFICE", "1"))
ALERT_CLASSES = set(os.environ.get("MEDSKY_CLASSES", "Y,R").split(","))
MAX_DAYS_AHEAD = int(os.environ.get("MEDSKY_MAX_DAYS", "0"))

API_URL = "https://portal.medsky.aero/api/FlightBooking/GetAvailability"
STATE_FILE = Path(__file__).parent / "state.json"
HISTORY_FILE = Path(__file__).parent / "docs" / "history.json"
HISTORY_MAX = 200

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://booking.medsky.aero",
    "Referer": "https://booking.medsky.aero/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
}


def fetch_availability(from_code, to_code):
    payload = {"office": OFFICE, "date": date.today().isoformat(), "from": from_code, "to": to_code}
    resp = requests.post(API_URL, json=payload, headers=HEADERS, timeout=30)
    print(f"API status ({from_code}->{to_code}): {resp.status_code}")
    resp.raise_for_status()
    return resp.json().get("journeys", [])


def flights_from_journeys(journeys, from_code, to_code):
    flights = {}
    today = date.today()
    for journey in journeys:
        for leg in journey.get("legs", []):
            dep = leg.get("xsdDepartureDateTime", "")
            key = f"{from_code}{to_code}_{leg.get('flightNumber', '?')}_{dep}"
            if MAX_DAYS_AHEAD > 0 and dep:
                try:
                    if (datetime.fromisoformat(dep).date() - today).days > MAX_DAYS_AHEAD:
                        continue
                except ValueError:
                    pass
            seats_alert = 0
            per_class = {}
            for cls in leg.get("availability", {}).get("classes", []):
                cid = cls.get("id", "?")
                avail = int(cls.get("availability", 0) or 0)
                per_class[cid] = avail
                if cid in ALERT_CLASSES:
                    seats_alert += avail
            flights[key] = {
                "flight": leg.get("flightNumber", "?"),
                "departure": leg.get("departureDateTime", dep),
                "route": f"{from_code} -> {to_code}",
                "dir": f"{from_code}{to_code}",
                "seats": seats_alert,
                "classes": per_class,
            }
    return flights


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(flights):
    state = {
        key: {"seats": info["seats"], "classes": info["classes"],
              "departure": info["departure"], "flight": info["flight"]}
        for key, info in flights.items()
    }
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))


def prev_seats(prev, key):
    v = prev.get(key)
    if isinstance(v, dict):
        return int(v.get("seats", 0) or 0)
    if isinstance(v, (int, float)):
        return int(v)
    return 0


def detect_movements(flights, prev):
    moves = []
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for key, info in flights.items():
        old = prev.get(key)
        old_classes = old.get("classes", {}) if isinstance(old, dict) else {}
        for cid, now_n in info["classes"].items():
            was = int(old_classes.get(cid, 0) or 0)
            if now_n != was:
                moves.append({
                    "ts": stamp, "flight": info["flight"], "departure": info["departure"],
                    "dir": info.get("dir", ""), "cls": cid,
                    "from": was, "to": now_n, "delta": now_n - was,
                })
    return moves


def append_history(moves):
    HISTORY_FILE.parent.mkdir(exist_ok=True)
    log = []
    if HISTORY_FILE.exists():
        try:
            log = json.loads(HISTORY_FILE.read_text())
        except json.JSONDecodeError:
            log = []
    log.extend(moves)
    log = log[-HISTORY_MAX:]
    HISTORY_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2))


def write_page_data(directions):
    docs = Path(__file__).parent / "docs"
    docs.mkdir(exist_ok=True)
    payload = {
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "directions": [
            {
                "from": d["from"], "to": d["to"], "alert": d["alert"],
                "flights": [
                    {"flight": f["flight"], "departure": f["departure"],
                     "seats": f["seats"], "classes": f["classes"]}
                    for _, f in sorted(d["flights"].items())
                ],
            }
            for d in directions
        ],
    }
    (docs / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured; message would have been:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=30)
    print(f"Telegram status: {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text)


def format_alert(newly_available):
    lines = ["🚨 مقاعد متوفرة على MedSky!"]
    for f in newly_available:
        cls_txt = "  ".join(f"{c}:{n}" for c, n in f["classes"].items())
        lines.append("")
        lines.append(f"✈️ {f['flight']}  {f['route']}")
        lines.append(f"🕘 الإقلاع: {f['departure']}")
        lines.append(f"💺 المقاعد: {cls_txt}")
    lines.append("")
    lines.append("احجز توا: https://booking.medsky.aero/")
    return "\n".join(lines)


def main():
    prev = load_state()
    first_run = not STATE_FILE.exists()

    directions = []
    all_flights = {}
    newly_available = []

    for leg in LEGS:
        journeys = fetch_availability(leg["from"], leg["to"])
        flights = flights_from_journeys(journeys, leg["from"], leg["to"])
        print(f"{leg['from']}->{leg['to']}: {len(flights)} flight(s)")
        for _, f in sorted(flights.items()):
            print(f"  {f['departure']}  {f['flight']}  seats={f['seats']}  {f['classes']}")
        directions.append({**leg, "flights": flights})
        all_flights.update(flights)
        if leg["alert"]:
            newly_available += [
                info for key, info in sorted(flights.items())
                if info["seats"] > 0 and prev_seats(prev, key) == 0
            ]

    if first_run:
        print("First run: baseline saved, no alerts sent.")
    elif newly_available:
        send_telegram(format_alert(newly_available))
        print(f"Alert sent for {len(newly_available)} flight(s).")
    else:
        print("No change worth alerting.")

    if not first_run:
        moves = detect_movements(all_flights, prev)
        if moves:
            append_history(moves)
            print(f"Logged {len(moves)} movement(s).")

    save_state(all_flights)
    write_page_data(directions)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as exc:
        print(f"Request failed: {exc}")
        sys.exit(0)
