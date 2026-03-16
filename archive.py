#!/usr/bin/env python3
"""
Telegram Khutba Archive Script

Reads the local .md index files (172 entries), fetches each message from the
public @daririhasan Telegram channel, downloads audio attachments, records
YouTube links, and saves metadata to JSON + CSV.

Usage:
    cp .env.example .env        # fill in TG_API_ID and TG_API_HASH
    pip install -r requirements.txt
    python archive.py

On first run Telegram will ask you to log in (phone + OTP). The session is
saved to khutba_session.session so subsequent runs are automatic.
"""

import asyncio
import csv
import json
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument

load_dotenv()

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]

CHANNEL = "daririhasan"
OUTPUT_DIR = Path("audio")
METADATA_JSON = Path("khutba_archive.json")
METADATA_CSV = Path("khutba_archive.csv")
SESSION_FILE = "khutba_session"

# Polite delay between Telegram API calls (seconds)
REQUEST_DELAY = 0.8

YOUTUBE_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/watch\?[^\s]*v=|youtu\.be/)[\w\-]+"
)

# Direct audio download links found in message text (not Telegram attachments)
# Covers top4top, archive.org, and bare m4a/mp3/ogg/opus/aac links
DIRECT_DL_RE = re.compile(
    r"https?://\S+\.(?:m4a|mp3|ogg|opus|aac)(?:\?[^\s]*)?"
    r"|https?://[a-z0-9]+\.top4top\.(?:net|io)/[^\s]+"
    r"|https?://(?:www\.)?archive\.org/download/[^\s]+",
    re.IGNORECASE,
)

# Map Arabic-Indic digits → ASCII digits
_AR_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
# Invisible/zero-width Unicode characters that sometimes appear in URLs
_INVISIBLE_RE = re.compile(r"[\u00ad\u200b-\u200f\u2060\u2061\ufeff]")


# ---------------------------------------------------------------------------
# Parsing the .md index files
# ---------------------------------------------------------------------------

def _normalize_num(s: str) -> str:
    return s.translate(_AR_DIGIT_MAP)


def parse_md_files(md_dir: Path) -> list[dict]:
    """
    Parse all khutba_*.md files and return a list of dicts, one per entry:
        index, type, title, telegram_url, message_id
    """
    entries = []
    for md_file in sorted(md_dir.glob("khutba_*.md")):
        raw = md_file.read_text(encoding="utf-8")
        # Strip invisible chars that pollute URLs
        raw = _INVISIBLE_RE.sub("", raw)
        lines = [line.strip() for line in raw.splitlines()]

        i = 0
        while i < len(lines):
            # Entry header: "١." or "١٢٣." optionally followed by 📌TYPE
            m = re.match(r"^([٠-٩\d]+)\.\s*📌(.+)$", lines[i])
            if not m:
                i += 1
                continue

            index = int(_normalize_num(m.group(1)))
            sermon_type = m.group(2).strip()

            # Collect the next 1–4 non-empty lines to find title + URL
            title = ""
            telegram_url = ""
            j = i + 1
            consumed = 0
            while j < len(lines) and consumed < 5:
                line = lines[j]
                j += 1
                if not line:
                    continue
                consumed += 1

                # URL line (with or without 🔗 prefix)
                candidate = re.sub(r"^🔗\s*", "", line).strip()
                if re.match(r"https://t\.me/", candidate):
                    telegram_url = candidate
                    break

                # Title line (skip decorative-only lines)
                if not title:
                    cleaned = re.sub(
                        r"^[\s\U0001F300-\U0001F9FF\u2600-\u27BF]+|"
                        r"[\s\U0001F300-\U0001F9FF\u2600-\u27BF]+$",
                        "",
                        line,
                    ).strip()
                    if cleaned:
                        title = cleaned

            if telegram_url:
                mid_m = re.search(r"/(\d+)$", telegram_url)
                entries.append(
                    {
                        "index": index,
                        "type": sermon_type,
                        "title": title,
                        "telegram_url": telegram_url,
                        "message_id": int(mid_m.group(1)) if mid_m else None,
                    }
                )
                i = j
            else:
                i += 1

    entries.sort(key=lambda e: e["index"])
    return entries


# ---------------------------------------------------------------------------
# File-naming helpers
# ---------------------------------------------------------------------------

def safe_filename(index: int, title: str, ext: str) -> str:
    """003_أضرار_القات.m4a — keep Arabic, alphanumeric, underscores."""
    clean = re.sub(r"[^\w\u0600-\u06ff\s]", "", title)
    clean = re.sub(r"\s+", "_", clean.strip())
    return f"{index:03d}_{clean}{ext}"


# ---------------------------------------------------------------------------
# Direct HTTP download helper
# ---------------------------------------------------------------------------

async def download_direct(url: str, dest: Path) -> bool:
    """
    Download a file from a direct HTTP URL to dest.
    Returns True on success.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as http:
            async with http.stream("GET", url) as resp:
                resp.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
        return True
    except Exception as exc:
        print(f"  ↳ Direct download failed: {exc}")
        if dest.exists():
            dest.unlink()
        return False


# ---------------------------------------------------------------------------
# Core archiving logic
# ---------------------------------------------------------------------------

async def process_entries(
    client: TelegramClient, entries: list[dict]
) -> list[dict]:
    OUTPUT_DIR.mkdir(exist_ok=True)

    # Load existing results so the script is safely resumable
    existing: dict[int, dict] = {}
    if METADATA_JSON.exists():
        with open(METADATA_JSON, encoding="utf-8") as f:
            for rec in json.load(f):
                if rec.get("message_id"):
                    existing[rec["message_id"]] = rec

    results: list[dict] = []

    for entry in entries:
        msg_id = entry["message_id"]
        tag = f"[{entry['index']:03d}]"

        if not msg_id:
            print(f"{tag} No message ID — skipping.")
            results.append({**entry, "status": "no_id", "audio_file": None,
                            "youtube_url": None, "date": None})
            continue

        # Resume: skip if already successfully handled
        if msg_id in existing and existing[msg_id].get("status") in (
            "downloaded", "youtube"
        ):
            print(f"{tag} Already archived, skipping.")
            results.append(existing[msg_id])
            continue

        print(f"{tag} Fetching message {msg_id} …", end=" ", flush=True)
        try:
            message = await client.get_messages(CHANNEL, ids=msg_id)
        except Exception as exc:
            print(f"ERROR — {exc}")
            results.append({**entry, "status": "error", "error": str(exc),
                            "audio_file": None, "youtube_url": None, "date": None})
            await asyncio.sleep(REQUEST_DELAY)
            continue

        if not message:
            print("not found.")
            results.append({**entry, "status": "not_found", "audio_file": None,
                            "youtube_url": None, "date": None})
            await asyncio.sleep(REQUEST_DELAY)
            continue

        print("ok.")
        date_str = message.date.isoformat() if message.date else None
        msg_text = (message.text or message.message or "").strip()

        # --- YouTube link? Record and move on. ---
        yt_m = YOUTUBE_RE.search(msg_text)
        if yt_m:
            yt_url = yt_m.group(0)
            print(f"  ↳ YouTube link: {yt_url}")
            results.append({**entry, "status": "youtube", "youtube_url": yt_url,
                            "audio_file": None, "date": date_str})
            await asyncio.sleep(REQUEST_DELAY)
            continue

        # --- Audio/document attachment? ---
        if isinstance(message.media, MessageMediaDocument):
            doc = message.media.document
            ext = ".m4a"  # default
            for attr in doc.attributes:
                fname = getattr(attr, "file_name", None)
                if fname:
                    suffix = Path(fname).suffix
                    if suffix:
                        ext = suffix
                    break

            filename = safe_filename(entry["index"], entry["title"], ext)
            filepath = OUTPUT_DIR / filename

            if filepath.exists():
                print(f"  ↳ Already on disk: {filename}")
            else:
                size_kb = doc.size // 1024
                print(f"  ↳ Downloading {filename} ({size_kb} KB) …")
                await client.download_media(message, file=str(filepath))
                print(f"  ↳ Saved.")

            results.append({**entry, "status": "downloaded",
                            "audio_file": str(filepath), "youtube_url": None,
                            "date": date_str})
        else:
            # No Telegram document — check message text for a direct download URL
            dl_m = DIRECT_DL_RE.search(msg_text)
            if dl_m:
                dl_url = dl_m.group(0).rstrip(".")
                ext = Path(dl_url.split("?")[0]).suffix or ".m4a"
                filename = safe_filename(entry["index"], entry["title"], ext)
                filepath = OUTPUT_DIR / filename
                print(f"  ↳ Direct download link: {dl_url}")
                if filepath.exists():
                    print(f"  ↳ Already on disk: {filename}")
                    results.append({**entry, "status": "downloaded",
                                    "audio_file": str(filepath),
                                    "youtube_url": None, "date": date_str})
                else:
                    print(f"  ↳ Downloading {filename} …")
                    ok = await download_direct(dl_url, filepath)
                    if ok:
                        print(f"  ↳ Saved.")
                        results.append({**entry, "status": "downloaded",
                                        "audio_file": str(filepath),
                                        "youtube_url": None, "date": date_str})
                    else:
                        results.append({**entry, "status": "dl_failed",
                                        "audio_file": None,
                                        "youtube_url": dl_url, "date": date_str})
            else:
                print(f"  ↳ No audio media or download link.")
                results.append({**entry, "status": "no_media", "audio_file": None,
                                "youtube_url": None, "date": date_str})

        await asyncio.sleep(REQUEST_DELAY)

    return results


# ---------------------------------------------------------------------------
# Saving output
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "index", "type", "title", "telegram_url", "message_id",
    "date", "status", "audio_file", "youtube_url",
]


def save_results(results: list[dict]) -> None:
    # JSON
    with open(METADATA_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved → {METADATA_JSON}")

    # CSV (utf-8-sig so Excel opens Arabic correctly)
    with open(METADATA_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    print(f"Saved → {METADATA_CSV}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    entries = parse_md_files(Path("."))
    print(f"Parsed {len(entries)} entries from .md files.\n")

    async with TelegramClient(SESSION_FILE, API_ID, API_HASH) as client:
        results = await process_entries(client, entries)

    save_results(results)

    downloaded = sum(1 for r in results if r["status"] == "downloaded")
    youtube = sum(1 for r in results if r["status"] == "youtube")
    errors = sum(1 for r in results if r["status"] in ("error", "not_found", "no_media"))
    print(
        f"\nSummary: {downloaded} downloaded | "
        f"{youtube} YouTube links | "
        f"{errors} issues"
    )


if __name__ == "__main__":
    asyncio.run(main())
