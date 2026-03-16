#!/usr/bin/env python3
"""
verify_titles.py — Compare stored titles against live Telegram message text.

For every entry in khutba_archive.json this script fetches the actual Telegram
message, tries to extract the real title from the message text, and reports
mismatches so they can be reviewed and corrected.

Output:
    title_verification.json  — full record per entry (match / mismatch / unclear)
    title_verification.csv   — same, for easy spreadsheet review

Usage:
    python verify_titles.py               # verify all entries
    python verify_titles.py --apply       # also update titles in khutba_archive.json

The script is safely resumable: if title_verification.json already exists,
entries already marked "match" or "mismatch" are skipped (use --force to
re-fetch everything).
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
# Title extraction from Telegram message text
# ---------------------------------------------------------------------------

# Remove these trailing qualifiers from a candidate title so comparison is fair
_QUALIFIER_RE = re.compile(
    r"\s*[-–]\s*(?:رحمه الله|حفظه الله|رضي الله عنه|رضي الله عنها|نفع الله به)\s*$"
)

# Patterns that mark a "speaker / instructions" segment — not part of the title
_SKIP_RE = re.compile(
    r"لفضيلة|الشيخ|للتحميل|للاستماع|رابط|تحميل|استماع|مشاهدة"
    r"|لفضيله|للمشاهدة|اضغط|انقر|متاح|يمكن"
)

# Patterns that mark the sermon TYPE — not the title
_TYPE_RE = re.compile(
    r"^(?:خطبة\s+الجمعة|خطبة\s+العيد|خطبة\s+الاستسقاء|خطبة|درس|محاضرة)\s*$"
)

# Emoji / punctuation used as separators between inline segments
_SEGMENT_SPLIT_RE = re.compile(
    r"[\U0001F300-\U0001F9FF\u2600-\u27BF\u2702-\u27B0"
    r"\u25A0-\u25FF\u2000-\u206F\u2190-\u21FF]+"
)

# Invisible / zero-width chars
_INVISIBLE_RE = re.compile(r"[\u00ad\u200b-\u200f\u2060\u2061\ufeff\u202a-\u202e]")


def _clean_segment(s: str) -> str:
    s = _INVISIBLE_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Strip leading colons / Arabic colons
    s = re.sub(r"^[\s::\-–]+|[\s::\-–]+$", "", s)
    return s


def extract_title(text: str) -> str:
    """
    Heuristically extract the khutba title from Telegram message text.

    Returns the best candidate, or "" if nothing convincing was found.
    """
    if not text:
        return ""

    # Remove all URLs
    text = re.sub(r"https?://\S+", "", text)
    text = _INVISIBLE_RE.sub("", text)

    # Split into segments: first by newline, then by inline emoji separators
    lines = [ln.strip() for ln in text.splitlines()]
    raw_segments: list[str] = []
    for line in lines:
        parts = _SEGMENT_SPLIT_RE.split(line)
        raw_segments.extend(parts)

    candidates: list[str] = []
    for seg in raw_segments:
        seg = _clean_segment(seg)
        # Must contain Arabic
        if not re.search(r"[\u0600-\u06ff]", seg):
            continue
        # Skip type labels
        if _TYPE_RE.search(seg):
            continue
        # Skip speaker / instruction segments
        if _SKIP_RE.search(seg):
            continue
        # Reasonable title length
        if len(seg) < 5 or len(seg) > 120:
            continue
        candidates.append(seg)

    if not candidates:
        return ""

    # Prefer shorter, purer Arabic segments (titles tend to be concise)
    candidates.sort(key=lambda s: len(s))
    best = candidates[0]
    # Strip trailing qualifiers like " - رحمه الله"
    best = _QUALIFIER_RE.sub("", best).strip()
    return best


def titles_match(stored: str, extracted: str) -> bool:
    """
    Loose comparison: ignore punctuation, diacritics, and qualifiers.
    """
    def normalise(s: str) -> str:
        s = _QUALIFIER_RE.sub("", s)
        # Remove Arabic diacritics (harakat)
        s = re.sub(r"[\u064b-\u065f\u0670]", "", s)
        # Remove all non-Arabic non-alphanumeric
        s = re.sub(r"[^\u0600-\u06ff\w]", "", s)
        return s.strip().lower()

    return normalise(stored) == normalise(extracted)


# ---------------------------------------------------------------------------
# Main verification loop
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "index", "type", "stored_title", "telegram_title", "verdict",
    "telegram_url", "message_id", "telegram_raw_text",
]


async def verify(
    client: TelegramClient, records: list[dict], force: bool
) -> list[dict]:
    # Load existing verification results
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
                            "telegram_title": "", "verdict": "no_id",
                            "telegram_raw_text": ""})
            continue

        if not force and msg_id in existing:
            print(f"{tag} Already verified ({existing[msg_id]['verdict']}), skipping.")
            results.append(existing[msg_id])
            continue

        print(f"{tag} Fetching {msg_id} …", end=" ", flush=True)
        try:
            message = await client.get_messages(CHANNEL, ids=msg_id)
        except Exception as exc:
            print(f"ERROR — {exc}")
            results.append({**rec, "stored_title": rec.get("title", ""),
                            "telegram_title": "", "verdict": "error",
                            "telegram_raw_text": str(exc)})
            await asyncio.sleep(REQUEST_DELAY)
            continue

        if not message:
            print("not found.")
            results.append({**rec, "stored_title": rec.get("title", ""),
                            "telegram_title": "", "verdict": "not_found",
                            "telegram_raw_text": ""})
            await asyncio.sleep(REQUEST_DELAY)
            continue

        raw_text = (message.text or message.message or "").strip()
        # Collapse to single line for the CSV (preserve full version in JSON)
        raw_text_oneline = raw_text.replace("\n", " | ")

        tg_title = extract_title(raw_text)
        stored_title = rec.get("title", "")

        if not tg_title:
            verdict = "unclear"
        elif titles_match(stored_title, tg_title):
            verdict = "match"
        else:
            verdict = "mismatch"

        print(f"{verdict}  stored='{stored_title}'  →  tg='{tg_title}'")

        results.append({
            **rec,
            "stored_title": stored_title,
            "telegram_title": tg_title,
            "verdict": verdict,
            "telegram_raw_text": raw_text_oneline,
        })

        await asyncio.sleep(REQUEST_DELAY)

    return results


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
    """
    Update khutba_archive.json: replace titles for mismatch/unclear entries
    with the telegram_title (only when non-empty).
    """
    if not ARCHIVE_JSON.exists():
        print("khutba_archive.json not found — nothing to apply.")
        return

    with open(ARCHIVE_JSON, encoding="utf-8") as f:
        archive = json.load(f)

    tg_map = {
        r["message_id"]: r
        for r in results
        if r.get("message_id") and r.get("telegram_title")
        and r["verdict"] in ("mismatch", "unclear")
    }

    changed = 0
    for rec in archive:
        mid = rec.get("message_id")
        if mid in tg_map:
            old = rec.get("title", "")
            new = tg_map[mid]["telegram_title"]
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

    mismatches = [r for r in results if r["verdict"] == "mismatch"]
    unclear = [r for r in results if r["verdict"] == "unclear"]
    matches = [r for r in results if r["verdict"] == "match"]

    print(
        f"\nSummary: {len(matches)} match | "
        f"{len(mismatches)} mismatch | "
        f"{len(unclear)} unclear"
    )

    if mismatches:
        print("\nMismatches:")
        for r in mismatches:
            print(
                f"  [{r['index']:03d}] stored : {r['stored_title']}\n"
                f"        tg     : {r['telegram_title']}\n"
                f"        url    : {r['telegram_url']}"
            )

    if apply:
        print("\nApplying corrections to archive …")
        apply_corrections(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify khutba titles against Telegram.")
    parser.add_argument(
        "--force", action="store_true",
        help="Re-fetch all messages even if already verified."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Write corrected titles back into khutba_archive.json."
    )
    args = parser.parse_args()
    asyncio.run(main(force=args.force, apply=args.apply))
