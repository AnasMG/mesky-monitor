#!/usr/bin/env python3
"""
MedSky seat availability monitor — multi-route.

Routes: MJI <-> MXP (Milan) and MJI <-> CDG (Paris, launching 17 Sep).

Per leg:
  alert=True        -> sold-out -> available transition sends Telegram
  alert=False       -> tracked and shown on the board, no Telegram
  new_route_alert   -> if the API had NO flights for this leg before and
                       now returns some, send a "tickets on sale" alert.
                       Remove the flag once the route is live and stable.

All directions are recorded in the movement history log.
If one leg's API call fails, the other legs still run, and the failed
leg's previous state is carried forward (no false alerts next run).
"""

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

# If MedSky ends up flying to Orly instead of CDG, change this one line.
PARIS = "CDG"

LEGS = [
    {"from": "MJI", "to": "MXP", "alert": True},   # Milan outbound — alerts
    {"from": "MXP", "to": "MJI", "alert": False},  # Milan return — display only
    {"from": "MJI", "to": PARIS, "alert": True,  "new_route_alert": True},
    {"from": PARIS, "to": "MJI", "alert": False, "new_route_alert": True},
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

ROUTE_NAMES = {
    "MJI": "طرابلس معيتيقة",
    "MXP": "ميلانو مالبينسا",
    "CDG": "باريس شارل ديغول",
    "ORY": "باريس أورلي",
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


def prev_has_dir(prev, dir_code):
    return any(key.startswith(dir_code + "_") for key in prev)


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
                "failed": d.get("failed", False),
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


def format_new_route_alert(leg, flights):
    name_from = ROUTE_NAMES.get(leg["from"], leg["from"])
    name_to = ROUTE_NAMES.get(leg["to"], leg["to"])
    lines = [f"🆕 خط جديد نزل للبيع على MedSky!",
             f"✈️ {name_from} ← {name_to} ({leg['from']} → {leg['to']})",
             ""]
    for _, f in sorted(flights.items())[:6]:
        lines.append(f"• {f['departure']}  رحلة {f['flight']}  مقاعد: {f['seats']}")
    if len(flights) > 6:
        lines.append(f"… و{len(flights) - 6} رحلات أخرى")
    lines.append("")
    lines.append("احجز توا: https://booking.medsky.aero/")
    return "\n".join(lines)


def main():
    prev = load_state()
    first_run = not STATE_FILE.exists()

    directions = []
    all_flights = {}
    newly_available = []
    new_routes = []

    for leg in LEGS:
        dir_code = leg["from"] + leg["to"]
        try:
            journeys = fetch_availability(leg["from"], leg["to"])
        except requests.RequestException as exc:
            # This leg failed — keep its previous state so the next
            # successful run doesn't see everything as "new seats".
            print(f"{leg['from']}->{leg['to']}: FAILED ({exc}) — carrying previous state forward")
            carried = {
                key: {**val, "route": f"{leg['from']} -> {leg['to']}", "dir": dir_code}
                for key, val in prev.items()
                if key.startswith(dir_code + "_") and isinstance(val, dict)
            }
            all_flights.update(carried)
            directions.append({**leg, "flights": carried, "failed": True})
            continue

        flights = flights_from_journeys(journeys, leg["from"], leg["to"])
        print(f"{leg['from']}->{leg['to']}: {len(flights)} flight(s)")
        for _, f in sorted(flights.items()):
            print(f"  {f['departure']}  {f['flight']}  seats={f['seats']}  {f['classes']}")

        directions.append({**leg, "flights": flights})
        all_flights.update(flights)

        if not first_run and leg.get("new_route_alert") and flights and not prev_has_dir(prev, dir_code):
            new_routes.append((leg, flights))

        if leg["alert"]:
            newly_available += [
                info for key, info in sorted(flights.items())
                if info["seats"] > 0 and prev_seats(prev, key) == 0
            ]

    if first_run:
        print("First run: baseline saved, no alerts sent.")
    else:
        for leg, flights in new_routes:
            send_telegram(format_new_route_alert(leg, flights))
            print(f"New-route alert sent for {leg['from']}->{leg['to']}.")
        # Don't double-alert flights already covered by a new-route alert.
        new_dirs = {leg["from"] + leg["to"] for leg, _ in new_routes}
        newly_available = [f for f in newly_available if f["dir"] not in new_dirs]
        if newly_available:
            send_telegram(format_alert(newly_available))
            print(f"Alert sent for {len(newly_available)} flight(s).")
        else:
            print("No seat-return alert needed.")

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
