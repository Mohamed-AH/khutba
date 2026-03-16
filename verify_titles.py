#!/usr/bin/env python3
"""
verify_titles.py — Compare stored titles against live Telegram message data.

Two extraction strategies depending on message type:

  1. Audio document messages  → DocumentAttributeAudio.title  (embedded metadata,
     most reliable; avoids the decorative `══ا ¤` / `00:00 ─━━` UI noise)

  2. Text / YouTube messages  → targeted regex looking for the channel's own
     formatting conventions  (🔖…🎙, ◆…◆, 🌀…🌀, بعنوان:…)

Verdict per entry:
  match    — extracted title confirms the stored one
  mismatch — extracted title is clearly different (real Arabic content)
  unclear  — could not extract a reliable title (audio metadata empty + no
             text pattern matched) — stored title is assumed correct

Output:
    title_verification.json   full record per entry
    title_verification.csv    spreadsheet-friendly (utf-8 BOM)

Usage:
    python verify_titles.py               # verify all
    python verify_titles.py --force       # re-fetch even already-verified entries
    python verify_titles.py --apply       # write corrections back to archive JSON
"""

import argparse
import asyncio
import csv
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeAudio, MessageMediaDocument

load_dotenv()

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]

CHANNEL = "daririhasan"
SESSION_FILE = "khutba_session"
ARCHIVE_JSON = Path("khutba_archive.json")
VERIFY_JSON = Path("title_verification.json")
VERIFY_CSV = Path("title_verification.csv")
REQUEST_DELAY = 0.8

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_INVISIBLE_RE = re.compile(r"[\u00ad\u200b-\u200f\u2060\u2061\ufeff\u202a-\u202e]")

# Trailing Islamic qualifiers that appear after names/titles
_QUALIFIER_RE = re.compile(
    r"\s*[-–]\s*(?:رحمه الله|حفظه الله|ورعاه|رضي الله عنه[ا]?|نفع الله به)\s*$"
)

# Box-drawing / audio-player decoration characters (══ا ¤, 00:00 ─━━ etc.)
# These appear in the body of Telegram audio messages and are NOT titles.
_DECORATION_RE = re.compile(
    r"[\u2500-\u257F\u2550-\u256C\u2014\u2013═─━\u25BA\u25C4¤●○►◄▶◀]"
)

# Arabic-Indic → ASCII digit map
_AR_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _clean(s: str) -> str:
    s = _INVISIBLE_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^[\s::\-–*]+|[\s::\-–*]+$", "", s)
    return s


def _has_arabic(s: str) -> bool:
    return bool(re.search(r"[\u0600-\u06ff]", s))


def _strip_qualifiers(s: str) -> str:
    return _QUALIFIER_RE.sub("", s).strip()


def _normalise(s: str) -> str:
    """Normalise for loose comparison: strip diacritics, punctuation, qualifiers."""
    s = _strip_qualifiers(s)
    # Arabic diacritics (harakat + tatweel)
    s = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", s)
    # Arabic-Indic digits → ASCII
    s = s.translate(_AR_DIGIT_MAP)
    # Drop everything that isn't Arabic letter, ASCII alnum, or ﷺ
    s = re.sub(r"[^\u0600-\u06ff\w]", "", s)
    return s.lower()


def titles_match(stored: str, extracted: str) -> bool:
    return bool(extracted) and _normalise(stored) == _normalise(extracted)


# ---------------------------------------------------------------------------
# Strategy 1 — Audio document metadata
# ---------------------------------------------------------------------------

def get_audio_metadata_title(message) -> str:
    """
    Return the title embedded in the audio document's metadata, or "".
    This is the most reliable source for audio-file messages.
    """
    if not isinstance(message.media, MessageMediaDocument):
        return ""
    for attr in message.media.document.attributes:
        if isinstance(attr, DocumentAttributeAudio):
            return _clean(attr.title or "")
    return ""


# ---------------------------------------------------------------------------
# Strategy 2 — Formatted text extraction (YouTube / text-only messages)
# ---------------------------------------------------------------------------

def extract_title_from_text(text: str) -> str:
    """
    Extract title from formatted message text using the channel's own
    formatting conventions.  Returns "" if nothing reliable found.
    """
    if not text:
        return ""

    # Remove URLs and invisible chars first
    text = re.sub(r"https?://\S+", "", text)
    text = _INVISIBLE_RE.sub("", text)

    # --- Pattern 1: 🔖 TITLE 🎙 ---
    # Very common in this channel: خطبة الجمعة🔖 TITLE 🎙 لفضيلة الشيخ...
    m = re.search(r"🔖\s*(.+?)(?=🎙|🎧|لفضيلة|للشيخ|\n|$)", text)
    if m:
        candidate = _clean(m.group(1))
        candidate = _strip_qualifiers(candidate)
        if _has_arabic(candidate) and len(candidate) >= 5:
            return candidate

    # --- Pattern 2: symmetric decorators ◆…◆  🌀…🌀  ✿…✿  ❒…❒ ---
    m = re.search(r"[◆🌀✿❒✦]\s*(.+?)\s*[◆🌀✿❒✦]", text)
    if m:
        candidate = _clean(m.group(1))
        candidate = _strip_qualifiers(candidate)
        if _has_arabic(candidate) and len(candidate) >= 5:
            return candidate

    # --- Pattern 3: "بعنوان[:] TITLE" ---
    m = re.search(r"بعنوان\s*[:\s]\s*(.+?)(?:\n|🎙|لفضيلة|$)", text)
    if m:
        candidate = _clean(m.group(1))
        candidate = _strip_qualifiers(candidate)
        if _has_arabic(candidate) and len(candidate) >= 5:
            return candidate

    return ""


def _is_garbage(s: str) -> bool:
    """Return True if the candidate title is decorative noise, not a real title."""
    if not s:
        return True
    # Contains box-drawing / audio-player decoration chars
    if _DECORATION_RE.search(s):
        return True
    # Timestamp pattern: 00:00
    if re.match(r"^\d{1,2}:\d{2}", s):
        return True
    # Just a date (١ شوال ١٤٤٤هـ, ١٤٤٣/١٠/١٢هـ)
    if re.match(r"^[\d٠-٩\s/،]+(?:هـ|م)?$", s):
        return True
    # Standalone qualifiers / type words
    if re.match(
        r"^(?:رحمه الله|حفظه الله|ورعاه|رضي الله عنه[ا]?"
        r"|خطبة جمعة|خطبة الجمعة|خطبة العيد"
        r"|للشيخ|لفضيلة|بعنوان)$",
        s.strip(),
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Main verification loop
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "index", "type", "stored_title", "telegram_title", "title_source",
    "verdict", "telegram_url", "message_id", "telegram_raw_text",
]


async def verify(
    client: TelegramClient, records: list[dict], force: bool
) -> list[dict]:
    existing: dict[int, dict] = {}
    if VERIFY_JSON.exists():
        with open(VERIFY_JSON, encoding="utf-8") as f:
            for row in json.load(f):
                if row.get("message_id"):
                    existing[row["message_id"]] = row

    results: list[dict] = []

    for rec in records:
        msg_id = rec.get("message_id")
        tag = f"[{rec['index']:03d}]"

        if not msg_id:
            results.append({**rec, "stored_title": rec.get("title", ""),
                            "telegram_title": "", "title_source": "",
                            "verdict": "no_id", "telegram_raw_text": ""})
            continue

        if not force and msg_id in existing:
            v = existing[msg_id]["verdict"]
            print(f"{tag} Already verified ({v}), skipping.")
            results.append(existing[msg_id])
            continue

        print(f"{tag} Fetching {msg_id} …", end=" ", flush=True)
        try:
            message = await client.get_messages(CHANNEL, ids=msg_id)
        except Exception as exc:
            print(f"ERROR — {exc}")
            results.append({**rec, "stored_title": rec.get("title", ""),
                            "telegram_title": "", "title_source": "",
                            "verdict": "error", "telegram_raw_text": str(exc)})
            await asyncio.sleep(REQUEST_DELAY)
            continue

        if not message:
            print("not found.")
            results.append({**rec, "stored_title": rec.get("title", ""),
                            "telegram_title": "", "title_source": "",
                            "verdict": "not_found", "telegram_raw_text": ""})
            await asyncio.sleep(REQUEST_DELAY)
            continue

        raw_text = (message.text or message.message or "").strip()
        raw_text_oneline = raw_text.replace("\n", " | ")
        stored_title = rec.get("title", "")

        # --- Try Strategy 1: audio metadata ---
        tg_title = get_audio_metadata_title(message)
        title_source = "audio_metadata" if tg_title else ""

        # --- Try Strategy 2: text extraction (always run; fills in for non-audio) ---
        if not tg_title:
            tg_title = extract_title_from_text(raw_text)
            if tg_title:
                title_source = "text_extraction"

        # --- Determine verdict ---
        if _is_garbage(tg_title):
            tg_title = ""
            title_source = ""

        if not tg_title:
            verdict = "unclear"
        elif titles_match(stored_title, tg_title):
            verdict = "match"
        else:
            verdict = "mismatch"

        source_label = f" [{title_source}]" if title_source else ""
        print(f"{verdict}{source_label}  stored='{stored_title}'  →  tg='{tg_title}'")

        results.append({
            **rec,
            "stored_title": stored_title,
            "telegram_title": tg_title,
            "title_source": title_source,
            "verdict": verdict,
            "telegram_raw_text": raw_text_oneline,
        })

        await asyncio.sleep(REQUEST_DELAY)

    return results


# ---------------------------------------------------------------------------
# Saving output
# ---------------------------------------------------------------------------

def save_report(results: list[dict]) -> None:
    with open(VERIFY_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved → {VERIFY_JSON}")

    with open(VERIFY_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    print(f"Saved → {VERIFY_CSV}")


def apply_corrections(results: list[dict]) -> None:
    """Write confirmed telegram_titles back into khutba_archive.json."""
    if not ARCHIVE_JSON.exists():
        print("khutba_archive.json not found — nothing to apply.")
        return

    with open(ARCHIVE_JSON, encoding="utf-8") as f:
        archive = json.load(f)

    corrections = {
        r["message_id"]: r["telegram_title"]
        for r in results
        if r.get("message_id")
        and r.get("telegram_title")
        and r["verdict"] == "mismatch"
    }

    changed = 0
    for rec in archive:
        mid = rec.get("message_id")
        if mid in corrections:
            old = rec.get("title", "")
            new = corrections[mid]
            if old != new:
                print(f"  [{rec['index']:03d}] '{old}'  →  '{new}'")
                rec["title"] = new
                changed += 1

    with open(ARCHIVE_JSON, "w", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=2)

    print(f"\nApplied {changed} title correction(s) to {ARCHIVE_JSON}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(force: bool, apply: bool) -> None:
    if not ARCHIVE_JSON.exists():
        print(f"ERROR: {ARCHIVE_JSON} not found. Run archive.py first.")
        return

    with open(ARCHIVE_JSON, encoding="utf-8") as f:
        records = json.load(f)

    print(f"Loaded {len(records)} records from {ARCHIVE_JSON}\n")

    async with TelegramClient(SESSION_FILE, API_ID, API_HASH) as client:
        results = await verify(client, records, force=force)

    save_report(results)

    counts = {v: sum(1 for r in results if r["verdict"] == v)
              for v in ("match", "mismatch", "unclear", "error", "not_found", "no_id")}

    print(
        f"\nSummary: {counts['match']} match | "
        f"{counts['mismatch']} mismatch | "
        f"{counts['unclear']} unclear (stored title assumed correct)"
    )

    if counts["mismatch"]:
        print("\nMismatches to review:")
        for r in results:
            if r["verdict"] == "mismatch":
                print(
                    f"  [{r['index']:03d}] [{r.get('title_source','')}]\n"
                    f"        stored : {r['stored_title']}\n"
                    f"        tg     : {r['telegram_title']}\n"
                    f"        url    : {r['telegram_url']}"
                )

    if apply:
        print("\nApplying mismatch corrections to archive …")
        apply_corrections(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verify khutba titles against live Telegram messages."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-fetch all messages, ignoring cached verification results."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Write mismatched telegram_titles back to khutba_archive.json."
    )
    args = parser.parse_args()
    asyncio.run(main(force=args.force, apply=args.apply))
