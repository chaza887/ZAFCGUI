#!/usr/bin/env python3
"""
Fitness First Kings Cross Platinum — Weekly Class Digest
Fetches the timetable, formats it, and sends via Gmail (or saves to file).
"""

import os
import sys
import json
import logging
import re
import time
import smtplib
import base64
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

TIMETABLE_URL = (
    "https://www.fitnessfirst.com.au/classes/club-timetable/kings-cross-platinum/"
)
SYDNEY_TZ = ZoneInfo("Australia/Sydney")
MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "")
GMAIL_SENDER = os.getenv("GMAIL_SENDER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", ".")


# ---------------------------------------------------------------------------
# Step 1 — Fetch timetable (API discovery → Playwright fallback)
# ---------------------------------------------------------------------------

def _week_bounds():
    """Return (monday, sunday) for the current week in Sydney time."""
    today = datetime.now(SYDNEY_TZ).date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _try_api_discovery(session: requests.Session) -> list | None:
    """
    Attempt to find an embedded API endpoint in the page HTML and call it.
    Returns a list of raw class dicts if successful, else None.
    """
    log.info("Attempting API discovery via raw HTML fetch …")
    try:
        resp = session.get(TIMETABLE_URL, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Raw HTML fetch failed: %s", exc)
        return None

    html = resp.text

    # Look for JSON blobs embedded in <script> tags
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        text = script.string or ""
        # Look for Mindbody / schedule JSON patterns
        for pattern in (
            r"https?://[^\s\"']+(?:schedule|timetable|classes|appointments)[^\s\"']*",
            r"https?://[^\s\"']+/api/[^\s\"']+",
        ):
            matches = re.findall(pattern, text)
            for url in matches:
                url = url.rstrip("\\,;")
                log.info("Found candidate API URL: %s", url)
                try:
                    api_resp = session.get(url, timeout=20)
                    if api_resp.headers.get("content-type", "").startswith(
                        "application/json"
                    ):
                        data = api_resp.json()
                        classes = _parse_api_response(data)
                        if classes is not None:
                            log.info(
                                "API discovery succeeded — %d classes found.", len(classes)
                            )
                            return classes
                except Exception as exc:
                    log.debug("Candidate URL failed: %s — %s", url, exc)

        # Also look for inline JSON assigned to window.__* variables
        json_matches = re.findall(
            r"window\.__[A-Z_]+\s*=\s*(\{.*?\});", text, re.DOTALL
        )
        for blob in json_matches:
            try:
                data = json.loads(blob)
                classes = _parse_api_response(data)
                if classes:
                    log.info(
                        "Found inline JSON — %d classes extracted.", len(classes)
                    )
                    return classes
            except json.JSONDecodeError:
                pass

    log.info("No usable API discovered; will fall back to Playwright.")
    return None


def _parse_api_response(data) -> list | None:
    """
    Try to extract a list of class dicts from an arbitrary API response shape.
    Returns None if the shape isn't recognisable.
    """
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data  # bare array of objects
    if isinstance(data, dict):
        for key in ("classes", "sessions", "appointments", "items", "data", "results"):
            if key in data and isinstance(data[key], list):
                return data[key]
    return None


def _fetch_via_playwright() -> list:
    """
    Load the timetable page with a headless Chromium browser via Playwright
    and extract class data from the rendered HTML.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.error(
            "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        )
        raise

    log.info("Launching headless Chromium via Playwright …")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(TIMETABLE_URL, wait_until="networkidle", timeout=60_000)

        # Try a range of likely CSS selectors for the timetable
        selectors = [
            ".class-item",
            ".timetable-item",
            ".timetable__item",
            ".schedule-item",
            "[class*='classItem']",
            "[class*='timetable']",
            "table.timetable",
        ]
        for sel in selectors:
            try:
                page.wait_for_selector(sel, timeout=10_000)
                log.info("Timetable found with selector: %s", sel)
                break
            except PWTimeout:
                continue
        else:
            log.warning(
                "No known timetable selector matched — extracting full page HTML."
            )

        html = page.content()
        browser.close()

    return _parse_playwright_html(html)


def _parse_playwright_html(html: str) -> list:
    """Parse rendered HTML into a list of class dicts."""
    soup = BeautifulSoup(html, "html.parser")
    classes = []

    # Generic heuristic: look for elements whose class attribute contains
    # timetable / schedule / class-item / session keywords.
    candidate_selectors = [
        {"class": re.compile(r"class.?item|timetable.?item|schedule.?item", re.I)},
        {"class": re.compile(r"session|booking", re.I)},
    ]

    items = []
    for attrs in candidate_selectors:
        items = soup.find_all(True, attrs)
        if items:
            break

    for item in items:
        text = item.get_text(" ", strip=True)
        # Attempt to extract time (e.g. "6:30am", "6:30 AM")
        time_match = re.search(r"\b(\d{1,2}:\d{2}\s*(?:am|pm))\b", text, re.I)
        # Duration (e.g. "45 min", "1 hr")
        dur_match = re.search(r"(\d+)\s*(min|hr)\b", text, re.I)
        # Day names
        day_match = re.search(
            r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
            text,
            re.I,
        )

        classes.append(
            {
                "raw_text": text,
                "time": time_match.group(1).lower().replace(" ", "") if time_match else "",
                "duration": (
                    f"{dur_match.group(1)} {dur_match.group(2)}"
                    if dur_match
                    else ""
                ),
                "day": day_match.group(1).capitalize() if day_match else "",
                "name": _guess_class_name(text),
                "instructor": _guess_instructor(text),
            }
        )

    log.info("Playwright parse: %d raw class items found.", len(classes))
    return classes


def _guess_class_name(text: str) -> str:
    """Heuristically extract a class name from a text snippet."""
    # Strip common noise words then take the longest all-caps token or first
    # title-cased phrase after the time
    caps = re.findall(r"\b[A-Z]{3,}\b", text)
    if caps:
        return " ".join(caps[:3])
    title = re.search(r"[A-Z][a-z]+ (?:[A-Z][a-z]+ ?)+", text)
    return title.group(0).strip() if title else ""


def _guess_instructor(text: str) -> str:
    """Heuristically extract an instructor name from a text snippet."""
    match = re.search(
        r"(?:with|instructor|by)[:\s]+([A-Z][a-z]+(?: [A-Z][a-z.]+)?)", text
    )
    return match.group(1) if match else ""


def fetch_timetable(retries: int = MAX_RETRIES) -> list:
    """
    Top-level fetch with retry logic.
    Returns a list of class dicts, empty list if nothing found.
    Raises RuntimeError after exhausting retries.
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (compatible; FitnessDigestBot/1.0; "
                "+https://github.com/chaza887/ZAFCGUI)"
            )
        }
    )

    for attempt in range(1, retries + 1):
        log.info("Fetch attempt %d/%d …", attempt, retries)
        try:
            classes = _try_api_discovery(session)
            if classes is None:
                classes = _fetch_via_playwright()
            return classes
        except Exception as exc:
            log.error("Attempt %d failed: %s", attempt, exc)
            if attempt < retries:
                wait = 2 ** attempt
                log.info("Retrying in %ds …", wait)
                time.sleep(wait)

    raise RuntimeError(f"Timetable fetch failed after {retries} attempts.")


# ---------------------------------------------------------------------------
# Step 2 — Parse / normalise class data
# ---------------------------------------------------------------------------

DAYS_ORDER = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
]


def _parse_time(time_str: str) -> datetime | None:
    """Convert '6:30am' / '06:30 AM' style strings to a datetime.time."""
    time_str = time_str.strip().lower().replace(" ", "")
    for fmt in ("%I:%M%p", "%H:%M"):
        try:
            return datetime.strptime(time_str, fmt).time()
        except ValueError:
            pass
    return None


def _day_date(day_name: str, monday: date) -> date:
    idx = DAYS_ORDER.index(day_name) if day_name in DAYS_ORDER else 0
    return monday + timedelta(days=idx)


def normalise_classes(raw: list) -> dict[str, list]:
    """
    Normalise raw class dicts into a {day: [class, ...]} mapping.
    Filters out past classes and sorts each day by time.
    """
    monday, sunday = _week_bounds()
    now = datetime.now(SYDNEY_TZ)

    grouped: dict[str, list] = {d: [] for d in DAYS_ORDER}

    for item in raw:
        day = item.get("day", "")
        if day not in DAYS_ORDER:
            continue

        class_date = _day_date(day, monday)
        if class_date < monday or class_date > sunday:
            continue

        time_str = item.get("time", "") or item.get("StartTime", "") or item.get("start_time", "")
        t = _parse_time(str(time_str))

        # Discard past classes
        if t:
            class_dt = datetime.combine(class_date, t, tzinfo=SYDNEY_TZ)
            if class_dt < now:
                continue

        duration = (
            item.get("duration", "")
            or item.get("Duration", "")
            or item.get("duration_minutes", "")
        )
        if isinstance(duration, (int, float)):
            duration = f"{int(duration)} min"

        name = (
            item.get("name", "")
            or item.get("ClassName", "")
            or item.get("class_name", "")
            or item.get("Name", "")
        )
        instructor = (
            item.get("instructor", "")
            or item.get("InstructorName", "")
            or item.get("instructor_name", "")
            or item.get("Staff", {}).get("Name", "") if isinstance(item.get("Staff"), dict) else ""
        )

        grouped[day].append(
            {
                "time": time_str,
                "time_obj": t,
                "duration": str(duration),
                "name": str(name),
                "instructor": str(instructor),
            }
        )

    # Sort each day by time
    for day in DAYS_ORDER:
        grouped[day].sort(key=lambda c: c["time_obj"] or datetime.min.time())

    # Remove days with no classes
    return {d: v for d, v in grouped.items() if v}


# ---------------------------------------------------------------------------
# Step 3 — Format the email
# ---------------------------------------------------------------------------

DIVIDER = "─" * 34


def format_email(grouped: dict[str, list]) -> tuple[str, str]:
    """Return (subject, body) for the digest email."""
    monday, _ = _week_bounds()
    week_label = monday.strftime("%a %d %b").lstrip("0")

    subject = f"🏋️ Kings Cross Platinum — Class Schedule, Week of {week_label}"

    total = sum(len(v) for v in grouped.values())
    lines = [
        "Hi Charlie,",
        "",
        "Here's your weekly class timetable for Fitness First Kings Cross Platinum.",
        "",
    ]

    if not grouped:
        lines.append(
            "No classes were found for this week. "
            "The timetable may not have been updated yet."
        )
    else:
        for day in DAYS_ORDER:
            if day not in grouped:
                continue
            day_date = _day_date(day, monday)
            day_label = day_date.strftime("%d %b").lstrip("0")
            lines += [
                DIVIDER,
                f"{day.upper()}, {day_label}",
                DIVIDER,
            ]
            for cls in grouped[day]:
                parts = [cls["time"]]
                if cls["name"]:
                    dur = f"  ({cls['duration']})" if cls["duration"] else ""
                    parts.append(f"  {cls['name']}{dur}")
                if cls["instructor"]:
                    parts.append(f"  •  {cls['instructor']}")
                lines.append("  •  ".join(filter(None, [cls["time"], cls["name"] + (f" ({cls['duration']})" if cls["duration"] else ""), cls["instructor"]])))
            lines.append("")

    lines += [
        DIVIDER,
        f"Total classes this week: {total}",
        DIVIDER,
        "",
        "Book your spot via the Fitness First app or at:",
        TIMETABLE_URL,
        "",
        "Have a great week!",
    ]

    return subject, "\n".join(lines)


def format_failure_email() -> tuple[str, str]:
    subject = "⚠️ Fitness First timetable fetch failed"
    body = (
        "Hi Charlie,\n\n"
        "⚠️ The Fitness First timetable fetch failed this week — "
        "please check manually:\n"
        f"{TIMETABLE_URL}\n\n"
        "Have a great week anyway!"
    )
    return subject, body


# ---------------------------------------------------------------------------
# Step 4 — Send via Gmail (SMTP + App Password) or save to file
# ---------------------------------------------------------------------------

def send_via_gmail(subject: str, body: str) -> bool:
    """
    Send an email using Gmail SMTP with an App Password.
    Returns True on success, False on failure.
    """
    if not all([GMAIL_SENDER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]):
        log.warning(
            "Gmail credentials not fully configured "
            "(GMAIL_SENDER / GMAIL_APP_PASSWORD / RECIPIENT_EMAIL). "
            "Falling back to file output."
        )
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_SENDER
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_SENDER, RECIPIENT_EMAIL, msg.as_string())
        log.info("Email sent to %s.", RECIPIENT_EMAIL)
        return True
    except smtplib.SMTPException as exc:
        log.error("Gmail send failed: %s", exc)
        return False


def save_to_file(subject: str, body: str) -> str:
    """Save the formatted digest to a text file. Returns the file path."""
    today_str = date.today().strftime("%Y-%m-%d")
    filename = os.path.join(OUTPUT_DIR, f"timetable_{today_str}.txt")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(filename, "w", encoding="utf-8") as fh:
        fh.write(f"Subject: {subject}\n\n{body}\n")
    log.info("Digest saved to %s", filename)
    return filename


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== Fitness First Weekly Digest — %s ===", date.today().isoformat())

    try:
        raw_classes = fetch_timetable()
    except RuntimeError as exc:
        log.error("Fatal: %s", exc)
        subject, body = format_failure_email()
        if not send_via_gmail(subject, body):
            path = save_to_file(subject, body)
            print(f"\nFailure notice saved to: {path}\n")
            print(body)
        sys.exit(1)

    grouped = normalise_classes(raw_classes)
    subject, body = format_email(grouped)

    print(f"\nSubject: {subject}\n")
    print(body)

    sent = send_via_gmail(subject, body)
    if not sent:
        path = save_to_file(subject, body)
        print(f"\nDigest saved to: {path}")


if __name__ == "__main__":
    main()
