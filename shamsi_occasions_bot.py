#!/usr/bin/env python3
"""
Shamsi Occasions Telegram Bot
==============================

Sends the Shamsi (Jalali/Persian) calendar occasions for "today" to a
Telegram group chat, every day at a configured time.

Data source
-----------
Occasions come from a hand-maintained local file, occasions.txt, that you
keep alongside this script. It ships pre-populated with ~240 occasions
(national days, historical anniversaries, and religious observances)
compiled from public Persian-calendar datasets. See that file's own
comments for the format, and note that it supports Hijri (lunar) dates
natively: the script converts today's date to the Hijri calendar at
runtime (via the `hijridate` package) and matches against it, so
religious occasions such as Ramadan, Eid, and Ashura shift correctly
every year with no manual re-entry needed.

No network request is made to fetch occasions -- only to send the
Telegram message itself. This means the bot has zero dependency on any
third-party calendar API staying online.

Setup
-----
1. Create a bot with @BotFather on Telegram and copy the bot token.
2. Add the bot to your target group and make it an admin (or at least
   give it permission to post messages).
3. Get the group's chat_id (see get_chat_id() below, or forward a message
   from the group to @userinfobot / use getUpdates).
4. Install dependencies:

       pip install requests jdatetime hijridate

5. Set the environment variables below (or edit the DEFAULT_* constants),
   then run:

       python3 shamsi_occasions_bot.py --once   # send immediately, once
       python3 shamsi_occasions_bot.py          # run forever, daily at SEND_TIME

   For production use, it's usually simpler and more robust to run this
   script with `--once` from a cron job / systemd timer scheduled for
   your desired time, rather than leaving the built-in loop running.

Environment variables
----------------------
TELEGRAM_BOT_TOKEN   Bot token from @BotFather                 (required)
TELEGRAM_CHAT_ID     Target group chat id, e.g. -1001234567890  (required)
SEND_TIME            "HH:MM" 24h, time of day to send (default 08:00)
TIMEZONE             IANA timezone name (default "Asia/Tehran")
"""

import os
import sys
import time
import argparse
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import jdatetime
from hijridate import Gregorian as HijriGregorian

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEND_TIME = os.environ.get("SEND_TIME", "08:00")          # 24h "HH:MM"
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tehran")
OCCASIONS_FILE = os.environ.get(
    "OCCASIONS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "occasions.txt"),
)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("shamsi-occasions-bot")

# Make jdatetime render weekday/month names in Persian (rather than the
# default English transliteration) for %A / %B in strftime.
jdatetime.set_locale(jdatetime.FA_LOCALE)


# --------------------------------------------------------------------------
# Shamsi date helpers
# --------------------------------------------------------------------------

def get_shamsi_today(tz_name: str = TIMEZONE) -> jdatetime.date:
    """Return today's date as a jdatetime.date, in the given timezone."""
    now = datetime.now(ZoneInfo(tz_name))
    return jdatetime.date.fromgregorian(date=now.date())


def format_shamsi_header(jdate: jdatetime.date) -> str:
    """e.g. 'سه‌شنبه ۲۴ شهریور ۱۴۰۵' (weekday, day, month, year in Persian)."""
    return jdate.strftime("%A %d %B %Y")


# --------------------------------------------------------------------------
# Occasions loading (from the local hand-maintained file)
# --------------------------------------------------------------------------

def _parse_occasions_file(path: str):
    """
    Parse occasions.txt into a list of (date_spec, is_holiday, description)
    tuples. date_spec is one of:
        ("shamsi_recurring", month, day)     -- MM/DD
        ("shamsi_exact", year, month, day)   -- YYYY/MM/DD
        ("hijri_recurring", month, day)      -- H:MM/DD
        ("gregorian_recurring", month, day)  -- G:MM/DD
    Malformed lines are skipped with a warning rather than crashing the
    whole run.
    """
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|", 2)
            if len(parts) != 3:
                log.warning("occasions.txt line %d: malformed, skipping: %r", lineno, line)
                continue
            date_part, flag_part, desc = (p.strip() for p in parts)
            is_holiday = flag_part.upper() == "H"

            try:
                if date_part.upper().startswith("H:"):
                    month, day = (int(x) for x in date_part[2:].split("/"))
                    entries.append((("hijri_recurring", month, day), is_holiday, desc))
                elif date_part.upper().startswith("G:"):
                    month, day = (int(x) for x in date_part[2:].split("/"))
                    entries.append((("gregorian_recurring", month, day), is_holiday, desc))
                else:
                    date_bits = date_part.split("/")
                    if len(date_bits) == 2:
                        month, day = (int(x) for x in date_bits)
                        entries.append((("shamsi_recurring", month, day), is_holiday, desc))
                    elif len(date_bits) == 3:
                        year, month, day = (int(x) for x in date_bits)
                        entries.append((("shamsi_exact", year, month, day), is_holiday, desc))
                    else:
                        raise ValueError
            except ValueError:
                log.warning("occasions.txt line %d: bad date %r, skipping", lineno, date_part)
    return entries


def get_todays_occasions(jdate: jdatetime.date, path: str = OCCASIONS_FILE) -> dict:
    """
    Return today's occasions from the local occasions.txt file.

    Shamsi and Gregorian entries are compared directly against jdate (and
    its Gregorian equivalent). Hijri entries are matched by converting
    jdate to today's Hijri (lunar) date at runtime, so religious
    occasions land on the correct day every year automatically.

    Returns a dict like:
        {"is_holiday": bool, "events": [{"description": str,
                                          "is_holiday": bool}, ...]}
    Raises FileNotFoundError / OSError if the file is missing or unreadable.
    """
    entries = _parse_occasions_file(path)

    gdate = jdate.togregorian()
    hijri_today = HijriGregorian(gdate.year, gdate.month, gdate.day).to_hijri()

    events = []
    for date_spec, is_holiday, desc in entries:
        kind = date_spec[0]
        if kind == "shamsi_recurring":
            _, month, day = date_spec
            match = (month == jdate.month and day == jdate.day)
        elif kind == "shamsi_exact":
            _, year, month, day = date_spec
            match = (year == jdate.year and month == jdate.month and day == jdate.day)
        elif kind == "gregorian_recurring":
            _, month, day = date_spec
            match = (month == gdate.month and day == gdate.day)
        elif kind == "hijri_recurring":
            _, month, day = date_spec
            match = (month == hijri_today.month and day == hijri_today.day)
        else:
            match = False
        if match:
            events.append({"description": desc, "is_holiday": is_holiday})

    return {
        "is_holiday": any(ev["is_holiday"] for ev in events),
        "events": events,
    }


# --------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------

def format_message(jdate: jdatetime.date, data: dict) -> str:
    header = format_shamsi_header(jdate)
    events = data.get("events", [])
    is_holiday = data.get("is_holiday", False)

    lines = [f"📅 مناسبت‌های امروز، {header}"]
    if is_holiday:
        lines.append("🔴 امروز تعطیل رسمی است")
    lines.append("")

    if not events:
        lines.append("مناسبت خاصی برای امروز ثبت نشده است.")
    else:
        for ev in events:
            desc = ev.get("description", "").strip()
            if not desc:
                continue
            marker = "🔴" if ev.get("is_holiday") else "▫️"
            lines.append(f"{marker} {desc}")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Telegram sending
# --------------------------------------------------------------------------

def send_telegram_message(token: str, chat_id: str, text: str, timeout: int = 15) -> None:
    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set."
        )
    url = TELEGRAM_API_URL.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API returned an error: {result}")


def get_chat_id(token: str) -> None:
    """
    Utility helper: prints recent chat_ids seen by the bot via getUpdates.
    Send any message in the target group (mentioning the bot, or just any
    message if the bot is already a member) and then run:

        python3 shamsi_occasions_bot.py --get-chat-id
    """
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    seen = set()
    for update in data.get("result", []):
        msg = update.get("message") or update.get("channel_post")
        if not msg:
            continue
        chat = msg.get("chat", {})
        key = (chat.get("id"), chat.get("title") or chat.get("username"))
        if key not in seen:
            seen.add(key)
            print(f"chat_id: {chat.get('id')}   title: {chat.get('title') or chat.get('username')}")
    if not seen:
        print(
            "No chats found yet. Send a message in the target group first "
            "(with the bot already added to it), then re-run this command."
        )


# --------------------------------------------------------------------------
# Core run + scheduling
# --------------------------------------------------------------------------

def run_once() -> None:
    jdate = get_shamsi_today()
    log.info("Looking up occasions for Shamsi date %s", jdate)
    try:
        data = get_todays_occasions(jdate)
    except OSError as exc:
        log.error("Failed to read occasions file (%s): %s", OCCASIONS_FILE, exc)
        return
    text = format_message(jdate, data)
    try:
        send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, text)
    except Exception as exc:
        log.error("Failed to send Telegram message: %s", exc)
        return
    log.info("Occasions sent successfully.")


def seconds_until_next_run(send_time: str, tz_name: str) -> float:
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    hour, minute = (int(p) for p in send_time.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run_forever() -> None:
    log.info(
        "Starting daily loop. Will send every day at %s (%s).",
        SEND_TIME, TIMEZONE,
    )
    while True:
        wait_s = seconds_until_next_run(SEND_TIME, TIMEZONE)
        log.info("Sleeping %.0f seconds until next send.", wait_s)
        time.sleep(wait_s)
        run_once()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Shamsi occasions Telegram bot")
    parser.add_argument("--once", action="store_true", help="Send the occasions once and exit (e.g. for cron).")
    parser.add_argument("--get-chat-id", action="store_true", help="List chat_ids visible to the bot and exit.")
    args = parser.parse_args()

    if args.get_chat_id:
        if not TELEGRAM_BOT_TOKEN:
            sys.exit("Set TELEGRAM_BOT_TOKEN first.")
        get_chat_id(TELEGRAM_BOT_TOKEN)
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        sys.exit(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables "
            "before running (see the module docstring for details)."
        )

    if args.once:
        run_once()
    else:
        run_forever()


if __name__ == "__main__":
    main()
