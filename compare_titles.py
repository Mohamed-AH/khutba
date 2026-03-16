#!/usr/bin/env python3
"""
compare_titles.py — Compare titles in filename.txt vs khutba_archive.json

Usage:
    python compare_titles.py

Expects both files in the same directory. Outputs title_comparison.csv.
"""

import csv
import json
import re
from pathlib import Path

ARCHIVE_JSON = Path("khutba_archive.json")
FILENAME_TXT = Path("filename.txt")
OUTPUT_CSV   = Path("title_comparison.csv")

_AR_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def parse_filename_title(raw_path: str) -> str:
    """
    Extract the human title from a filename like:
      041_نعمة الأمن وبيان أسبابه __ الشيخ حسن.mp4  →  نعمة الأمن وبيان أسبابه
      002_سيرة الإمام _ زيد بن محمد - رحمه الله 1435_5_13 هـ.mp4  →  سيرة الإمام زيد بن محمد
      004_صوم_شعبان.m4a  →  صوم شعبان
    """
    stem = Path(raw_path.strip('"')).stem
    stem = re.sub(r"^\d+_", "", stem)                          # remove NNN_ prefix

    # Split at double-underscore (speaker separator)
    stem = re.split(r"\s*__\s*", stem, maxsplit=1)[0]

    # Strip trailing " _ الشيخ..." or " - لفضيلة..."
    stem = re.sub(r"\s+[_\-]\s+(?:لفضيلة|للشيخ|الشيخ|فضيلة).*$", "", stem)

    # Strip trailing date e.g. "1435_5_13 هـ" or "٢٦-٢-١٤٤١"
    stem = re.sub(r"\s+[\d٠-٩]+[_/\-][\d٠-٩]+[_/\-][\d٠-٩]+\s*(?:هـ|م)?\s*$", "", stem)
    stem = re.sub(r"\s+[\d٠-٩]+\s*(?:هـ|م)\s*$", "", stem)

    return re.sub(r"\s+", " ", stem.replace("_", " ")).strip()


def load_filenames() -> dict[int, str]:
    result = {}
    for line in FILENAME_TXT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\"?.*?\\?(\d+)_", line)
        if not m:
            continue
        idx = int(m.group(1))
        result[idx] = parse_filename_title(line)
    return result


def normalise(s: str) -> str:
    s = s.translate(_AR_DIGIT_MAP)
    s = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", s)          # diacritics
    s = re.sub(r"\s*[-–]\s*(?:رحمه الله|حفظه الله|ورعاه|رضي الله عنه[ا]?)\s*$", "", s)
    s = re.sub(r"[^\u0600-\u06ff\w]", "", s)                   # keep Arabic + alnum only
    return s.lower().strip()


def main():
    if not ARCHIVE_JSON.exists():
        print(f"ERROR: {ARCHIVE_JSON} not found.")
        return
    if not FILENAME_TXT.exists():
        print(f"ERROR: {FILENAME_TXT} not found.")
        return

    with open(ARCHIVE_JSON, encoding="utf-8") as f:
        archive: dict[int, dict] = {r["index"]: r for r in json.load(f)}

    filenames: dict[int, str] = load_filenames()

    all_indices = sorted(set(archive) | set(filenames))
    rows = []
    mismatches, matches, only_archive, only_file = [], [], [], []

    for idx in all_indices:
        arc_rec    = archive.get(idx)
        file_title = filenames.get(idx, "")
        arc_title  = arc_rec["title"] if arc_rec else ""

        if not arc_rec:
            verdict = "only_in_file"
            only_file.append(idx)
        elif not file_title:
            verdict = "only_in_archive"
            only_archive.append(idx)
        elif normalise(arc_title) == normalise(file_title):
            verdict = "match"
            matches.append(idx)
        else:
            verdict = "mismatch"
            mismatches.append(idx)

        rows.append({
            "index":         idx,
            "archive_title": arc_title,
            "file_title":    file_title,
            "verdict":       verdict,
            "telegram_url":  arc_rec.get("telegram_url", "") if arc_rec else "",
        })

    # CSV
    fields = ["index", "archive_title", "file_title", "verdict", "telegram_url"]
    with open(OUTPUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()
        csv.DictWriter(f, fieldnames=fields).writerows(rows)
    print(f"Saved → {OUTPUT_CSV}\n")

    # Console summary
    print(f"{'─'*60}")
    print(f"  Total          : {len(all_indices)}")
    print(f"  Match          : {len(matches)}")
    print(f"  Mismatch       : {len(mismatches)}")
    print(f"  Only in archive: {len(only_archive)}  {only_archive or ''}")
    print(f"  Only in file   : {len(only_file)}  {only_file or ''}")
    print(f"{'─'*60}\n")

    if mismatches:
        print("MISMATCHES\n──────────")
        for r in rows:
            if r["verdict"] == "mismatch":
                print(f"  [{r['index']:03d}]  archive: {r['archive_title']}")
                print(f"         file   : {r['file_title']}")
                print(f"         url    : {r['telegram_url']}\n")


if __name__ == "__main__":
    main()
