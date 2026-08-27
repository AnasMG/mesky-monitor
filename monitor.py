#!/usr/bin/env python3
"""
MedSky seat availability monitor.
Polls GetAvailability and sends a Telegram alert when a flight
goes from sold-out (0 seats) to available (>0 seats).

State is kept in state.json (committed back by the GitHub Actions workflow)
so alerts fire only on the transition, not on every run.
"""

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------
# Configuration (override via environment variables if needed)
# ---------------------------------------------------------------
FROM_CODE = os.environ.get("MEDSKY_FROM", "MJI")   # Mitiga
TO_CODE = os.environ.get("MEDSKY_TO", "MXP")       # Milano Malpensa
OFFICE = int(os.environ.get("MEDSKY_OFFICE", "1"))
# Which booking classes count as "a seat I can buy".
# Y = economy, R = discounted economy, C = business.
ALERT_CLASSES = set(os.environ.get("MEDSKY_CLASSES", "Y,R").split(","))
# Only alert for flights departing within this many days (0 = no limit).
MAX_DAYS_AHEAD = int(os.environ.get("MEDSKY_MAX_DAYS", "0"))

API_URL = "https://portal.medsky.aero/api/FlightBooking/GetAvailability"
STATE_FILE = Path(__file__).parent / "state.json"

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


def fetch_availability() -> list[dict]:
    payload = {
        "office": OFFICE,
        "date": date.today().isoformat(),
        "from": FROM_CODE,
        "to": TO_CODE,
    }
    resp = requests.post(API_URL, json=payload, headers=HEADERS, timeout=30)
    print(f"API status: {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    return data.get("journeys", [])


def flights_from_journeys(journeys: list[dict]) -> dict[str, dict]:
    """Return {flight_key: info} for every leg found."""
    flights = {}
    today = date.today()
    for journey in journeys:
        for leg in journey.get("legs", []):
            dep = leg.get("xsdDepartureDateTime", "")
            key = f"{leg.get('flightNumber', '?')}_{dep}"

            if MAX_DAYS_AHEAD > 0 and dep:
                try:
                    dep_date = datetime.fromisoformat(dep).date()
                    if (dep_date - today).days > MAX_DAYS_AHEAD:
                        continue
                except ValueError:
                    pass

            seats_alert = 0     # seats in the classes we care about
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
                "route": f"{FROM_CODE} -> {TO_CODE}",
                "seats": seats_alert,
                "classes": per_class,
            }
    return flights


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(flights: dict[str, dict]) -> None:
    state = {key: info["seats"] for key, info in flights.items()}
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured; message would have been:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=30,
    )
    print(f"Telegram status: {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text)


def format_alert(newly_available: list[dict]) -> str:
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


def main() -> int:
    journeys = fetch_availability()
    flights = flights_from_journeys(journeys)
    print(f"Flights in window: {len(flights)}")
    for key, f in sorted(flights.items()):
        print(f"  {f['departure']}  {f['flight']}  seats={f['seats']}  {f['classes']}")

    prev = load_state()
    first_run = not STATE_FILE.exists()

    newly_available = [
        info
        for key, info in sorted(flights.items())
        if info["seats"] > 0 and prev.get(key, 0) == 0
    ]

    if first_run:
        # Don't alert on the very first run — just record the baseline.
        print("First run: baseline saved, no alerts sent.")
    elif newly_available:
        send_telegram(format_alert(newly_available))
        print(f"Alert sent for {len(newly_available)} flight(s).")
    else:
        print("No change worth alerting.")

    save_state(flights)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as exc:
        # Network/API failure: log and exit 0 so the workflow doesn't
        # spam failure emails; next run will retry anyway.
        print(f"Request failed: {exc}")
        sys.exit(0)
