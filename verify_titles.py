#!/usr/bin/env python3
"""
verify_titles.py — Compare stored titles against live Telegram message data.

Mirrors archive.py's message retrieval and type-detection flow exactly, then
extracts the title per message type:

  YouTube messages       → parse msg_text with 🔖/🌀/بعنوان patterns
  Audio document         → DocumentAttributeAudio.title first, then msg_text
  Direct-download / text → msg_text patterns

Output:
    title_verification.json   full record per entry
    title_verification.csv    spreadsheet-friendly (utf-8 BOM)

Usage:
    python verify_titles.py               # verify all
    python verify_titles.py --force       # re-fetch even already-verified entries
    python verify_titles.py --apply       # write confirmed mismatches back to archive
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

# ── exact same regexes as archive.py ──────────────────────────────────────────
YOUTUBE_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/watch\?[^\s]*v=|youtu\.be/)[\w\-]+"
)
DIRECT_DL_RE = re.compile(
    r"https?://\S+\.(?:m4a|mp3|ogg|opus|aac)(?:\?[^\s]*)?"
    r"|https?://[a-z0-9]+\.top4top\.(?:net|io)/[^\s]+"
    r"|https?://(?:www\.)?archive\.org/download/[^\s]+",
    re.IGNORECASE,
)
_INVISIBLE_RE = re.compile(r"[\u00ad\u200b-\u200f\u2060\u2061\ufeff]")

# ── title helpers ──────────────────────────────────────────────────────────────
_QUALIFIER_RE = re.compile(
    r"\s*[-–]\s*(?:رحمه الله|حفظه الله|ورعاه|رضي الله عنه[ا]?|نفع الله به)\s*$"
)
# Box-drawing / audio-player decoration (══ا ¤, 00:00 ─━━ …)
_DECO_RE = re.compile(r"[\u2500-\u257F\u2550-\u256C═─━¤●○►◄▶◀]")
_AR_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _clean(s: str) -> str:
    s = _INVISIBLE_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^[\s::\-–*|]+|[\s::\-–*|]+$", "", s)
    return s


def _has_arabic(s: str) -> bool:
    return bool(re.search(r"[\u0600-\u06ff]", s))


def _strip_qual(s: str) -> str:
    return _QUALIFIER_RE.sub("", s).strip()


def _normalise(s: str) -> str:
    """Normalise for loose comparison."""
    s = _strip_qual(s)
    s = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", s)  # diacritics + tatweel
    s = s.translate(_AR_DIGIT_MAP)
    s = re.sub(r"[^\u0600-\u06ff\w]", "", s)
    return s.lower()


def _is_junk(s: str) -> bool:
    """True if s is clearly not a title (decoration, timestamp, date, bare label)."""
    if not s:
        return True
    if _DECO_RE.search(s):          # box-drawing / audio UI chars
        return True
    if re.match(r"^\d{1,2}:\d{2}", s):  # 00:00 timestamps
        return True
    # Pure date  ١ شوال ١٤٤٤هـ  or  ١٤٤٣/١٠/١٢هـ
    if re.match(r"^[\d٠-٩\s/،.]+(?:هـ|م)?$", s.strip()):
        return True
    # Bare type / credit / instruction words
    if re.fullmatch(
        r"خطبة\s*(?:الجمعة|العيد|الاستسقاء|جمعة)?"
        r"|لفضيلة|للشيخ|الشيخ|بعنوان"
        r"|رحمه\s*الله|حفظه\s*الله|ورعاه",
        s.strip(),
    ):
        return True
    return False


# ── title extraction from msg_text ────────────────────────────────────────────

def _extract_from_text(msg_text: str) -> str:
    """
    Extract the khutba title from the message caption text.
    Tries patterns in order of reliability for this channel's formats.
    """
    if not msg_text:
        return ""

    # Strip URLs and invisible chars (keep Arabic structure intact)
    text = re.sub(r"https?://\S+", "", msg_text)
    text = _INVISIBLE_RE.sub("", text)

    # Pattern A — 🔖 TITLE 🎙  (YouTube / newer formatted messages)
    m = re.search(r"🔖\s*(.+?)(?=🎙|🎧|لفضيلة|للشيخ|\n|$)", text)
    if m:
        cand = _clean(_strip_qual(m.group(1)))
        if not _is_junk(cand) and _has_arabic(cand) and len(cand) >= 5:
            return cand

    # Pattern B — 🌀 TITLE 🌀  or  ◆ TITLE ◆  etc.  (most audio captions)
    m = re.search(r"[🌀◆✿❒✦🎗]\s*(.+?)\s*[🌀◆✿❒✦🎗]", text)
    if m:
        cand = _clean(_strip_qual(m.group(1)))
        if not _is_junk(cand) and _has_arabic(cand) and len(cand) >= 5:
            return cand

    # Pattern C — بعنوان[:] TITLE
    m = re.search(r"بعنوان\s*[:\s]\s*(.+?)(?:\n|🎙|لفضيلة|$)", text)
    if m:
        cand = _clean(_strip_qual(m.group(1)))
        if not _is_junk(cand) and _has_arabic(cand) and len(cand) >= 5:
            return cand

    return ""


def _extract_from_audio_doc(message) -> str:
    """
    Extract title embedded in the audio document's metadata
    (DocumentAttributeAudio.title — most reliable for audio files).
    """
    if not isinstance(message.media, MessageMediaDocument):
        return ""
    for attr in message.media.document.attributes:
        if isinstance(attr, DocumentAttributeAudio):
            return _clean(attr.title or "")
    return ""


def get_telegram_title(message, msg_text: str, msg_type: str) -> tuple[str, str]:
    """
    Return (title, source) using the best available method for each message type.
    Mirrors archive.py's type-detection order.
    """
    if msg_type == "youtube":
        # Title is in the caption text
        title = _extract_from_text(msg_text)
        return (title, "text") if title else ("", "")

    if msg_type in ("downloaded_doc", "direct_dl", "no_media"):
        # Try audio metadata first (most reliable), then caption text
        title = _extract_from_audio_doc(message)
        if title and not _is_junk(title):
            return (title, "audio_metadata")
        title = _extract_from_text(msg_text)
        return (title, "text") if title else ("", "")

    return ("", "")


# ── comparison ─────────────────────────────────────────────────────────────────

def titles_match(stored: str, tg: str) -> bool:
    return bool(tg) and _normalise(stored) == _normalise(tg)


# ── main verification loop ─────────────────────────────────────────────────────

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

        # ── mirror archive.py type detection ──────────────────────────────────
        msg_text = (message.text or message.message or "").strip()
        raw_text_oneline = msg_text.replace("\n", " | ")

        if YOUTUBE_RE.search(msg_text):
            msg_type = "youtube"
        elif isinstance(message.media, MessageMediaDocument):
            msg_type = "downloaded_doc"
        elif DIRECT_DL_RE.search(msg_text):
            msg_type = "direct_dl"
        else:
            msg_type = "no_media"

        print(f"ok [{msg_type}].", end=" ", flush=True)

        # ── extract title ──────────────────────────────────────────────────────
        stored_title = rec.get("title", "")
        tg_title, title_source = get_telegram_title(message, msg_text, msg_type)

        if not tg_title:
            verdict = "unclear"
        elif titles_match(stored_title, tg_title):
            verdict = "match"
        else:
            verdict = "mismatch"

        print(f"{verdict}  stored='{stored_title}'  tg='{tg_title}'")

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


# ── output ─────────────────────────────────────────────────────────────────────

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
    if not ARCHIVE_JSON.exists():
        print("khutba_archive.json not found.")
        return

    with open(ARCHIVE_JSON, encoding="utf-8") as f:
        archive = json.load(f)

    corrections = {
        r["message_id"]: r["telegram_title"]
        for r in results
        if r.get("message_id") and r.get("telegram_title") and r["verdict"] == "mismatch"
    }

    changed = 0
    for rec in archive:
        mid = rec.get("message_id")
        if mid in corrections:
            old, new = rec.get("title", ""), corrections[mid]
            if old != new:
                print(f"  [{rec['index']:03d}] '{old}'  →  '{new}'")
                rec["title"] = new
                changed += 1

    with open(ARCHIVE_JSON, "w", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=2)

    print(f"\nApplied {changed} correction(s) to {ARCHIVE_JSON}")


# ── entry point ────────────────────────────────────────────────────────────────

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
              for v in ("match", "mismatch", "unclear", "error", "not_found")}

    print(
        f"\nSummary: {counts['match']} match | "
        f"{counts['mismatch']} mismatch | "
        f"{counts['unclear']} unclear"
    )

    if counts["mismatch"]:
        print("\nMismatches:")
        for r in results:
            if r["verdict"] == "mismatch":
                print(
                    f"  [{r['index']:03d}] [{r['title_source']}]\n"
                    f"        stored : {r['stored_title']}\n"
                    f"        tg     : {r['telegram_title']}\n"
                    f"        url    : {r['telegram_url']}"
                )

    if apply:
        print("\nApplying corrections …")
        apply_corrections(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch all messages, ignore cached results.")
    parser.add_argument("--apply", action="store_true",
                        help="Write mismatches back to khutba_archive.json.")
    args = parser.parse_args()
    asyncio.run(main(force=args.force, apply=args.apply))
