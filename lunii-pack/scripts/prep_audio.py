#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Thomas Guillem
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Prepare a downloaded podcast dir for packing:
 - glob audio files (NN_ prefix = play order from podcast-renumber)
 - transcode each to 44.1kHz mono 64kbps MP3 (matches reference packs)
 - derive a clean spoken episode title (text after ' : ', unicode-normalized)
Writes build/<slug>/audio/NN.mp3 and build/<slug>/titles.json
"""
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".flac"}
PREFIX_RE = re.compile(r"^(\d+)_")
AUDIO_LEAD_IN_MS = 500
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def clean_title(stem):
    s = PREFIX_RE.sub("", stem)
    # fold yt-dlp fullwidth sanitizations
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("⧸", "/").replace("⧹", "\\")
    s = " ".join(s.split())
    # episode title = text after the LAST ' : ' separator (the "N/M : Title")
    parts = re.split(r"\s*[:：]\s*", s)
    ep = parts[-1].strip() if len(parts) > 1 else s
    # strip a leading "Show N/M" if it still leads
    return ep


def main():
    if len(sys.argv) != 4:
        sys.exit("usage: prep_audio.py SOURCE_DIR SLUG SHOW_TITLE")
    src = Path(sys.argv[1])
    slug = sys.argv[2]
    show_title = sys.argv[3]
    if not src.is_dir():
        sys.exit("error: not a source directory: %s" % src)
    if not SLUG_RE.fullmatch(slug):
        sys.exit(
            "error: slug must contain only lowercase letters, digits, '.', '_' or '-'"
            " and must start with a letter or digit"
        )
    outdir = Path("build") / slug
    (outdir / "audio").mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in src.iterdir()
                   if p.suffix.lower() in AUDIO_EXTS)
    if not files:
        sys.exit("error: no supported audio files in %s" % src)
    print(f"{len(files)} source files")

    episodes = []
    for i, p in enumerate(files, 1):
        out = outdir / "audio" / f"{i:02d}.mp3"
        # Device (FS) transfer rejects MP3s carrying ID3 tags, so strip them:
        # -map_metadata -1 drops stream/format metadata; -write_id3v2/v1 0
        # stops libmp3lame from re-emitting an ID3 header/footer.
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(p),
               "-map", "0:a:0", "-map_metadata", "-1",
               "-af", f"adelay={AUDIO_LEAD_IN_MS}:all=1",
               "-ar", "44100", "-ac", "1", "-b:a", "64k",
               "-codec:a", "libmp3lame",
               "-id3v2_version", "0", "-write_id3v1", "0", str(out)]
        subprocess.run(cmd, check=True)
        # duration
        dur = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(out)],
            capture_output=True, text=True, check=True).stdout.strip()
        title = clean_title(p.stem)
        episodes.append({"n": i, "src": p.name, "title": title,
                         "audio": str(out), "duration_s": round(float(dur), 1)})
        print(f"  {i:02d}  {float(dur)/60:5.1f}min  {title}")

    meta = {"show_title": show_title, "slug": slug, "episodes": episodes}
    (outdir / "titles.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"wrote {outdir/'titles.json'}")


if __name__ == "__main__":
    main()
