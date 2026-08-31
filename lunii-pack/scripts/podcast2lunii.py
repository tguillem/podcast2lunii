#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Thomas Guillem
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""podcast2lunii — turn a downloaded podcast folder into a Lunii/STUdio pack .zip.

One deterministic pass, no LLM in the loop:
  folder of numbered audio  ->  transcode + title-voice TTS + numbered covers
                            ->  story.json menu graph  ->  <slug>.zip

The only input the script cannot derive is the cover art. A URL feed uses the
feed's own RSS artwork when it declares any; otherwise pass the image yourself
with --cover-file or --cover-url.

usage:
  podcast2lunii SRC_DIR [--title T] [--slug S] [-o OUTDIR]
                [--cover-url URL | --cover-file PATH]
                [--no-episode-tts] [--menu-prompt TEXT]

SRC_DIR is a folder of audio files already ordered by a NN_ filename prefix
(as produced by yt-dlp-podcast + podcast-renumber).
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
import warnings
import zipfile
from pathlib import Path

import requests
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lunii_image as LI

ROOT = Path(__file__).resolve().parent.parent
PIPER = ROOT / "venv" / "bin" / "piper"
MODEL = ROOT / "voices" / "fr_FR-siwis-medium.onnx"
YTDLP = ROOT / "venv" / "bin" / "yt-dlp"
YTDLP_PODCAST = ROOT.parent / "yt-dlp-podcast"      # the user's existing script
NS = uuid.UUID("1b671a64-40d5-491e-99b0-da01ff1f3341")
ITUNES_IMAGE = "{http://www.itunes.com/dtds/podcast-1.0.dtd}image"

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".ogg", ".oga", ".opus", ".flac"}
PREFIX_RE = re.compile(r"^\d+_")
AUDIO_LEAD_IN_MS = 500
MAX_FEED_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
COVER_FETCH_ATTEMPTS = 3
COVER_RETRY_DELAY_SECONDS = 1
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class PackBuildError(ValueError):
    """Raised when local input would produce an invalid archive graph."""


def require(condition, message):
    if not condition:
        raise PackBuildError(message)


def fetch_bytes(url, *, limit, params=None):
    """Fetch an HTTP resource without buffering more than ``limit`` bytes."""
    with requests.get(
        url, params=params, timeout=(10, 40), stream=True,
        headers={"User-Agent": "lunii-podcast-tools/1.0"},
    ) as response:
        response.raise_for_status()
        length = response.headers.get("Content-Length")
        try:
            declared_length = int(length) if length is not None else None
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > limit:
            raise ValueError(
                "response from %s is too large (%d bytes; limit %d)"
                % (response.url, declared_length, limit)
            )
        data = bytearray()
        for chunk in response.iter_content(64 * 1024):
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError(
                    "response from %s exceeds %d bytes" % (response.url, limit)
                )
        return bytes(data)


def fetch_cover_bytes(url, *, limit, params=None):
    """Fetch RSS artwork data, retrying only transient request failures."""
    for attempt in range(1, COVER_FETCH_ATTEMPTS + 1):
        try:
            return fetch_bytes(url, limit=limit, params=params)
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            retryable = status is None or status in (408, 429) or status >= 500
            if not retryable or attempt == COVER_FETCH_ATTEMPTS:
                raise
            print(
                "  cover: request failed (%s); retrying in %d second "
                "(attempt %d/%d)"
                % (
                    exc,
                    COVER_RETRY_DELAY_SECONDS,
                    attempt + 1,
                    COVER_FETCH_ATTEMPTS,
                ),
                file=sys.stderr,
            )
            time.sleep(COVER_RETRY_DELAY_SECONDS)


def decode_image(data, source):
    """Decode an image while treating Pillow decompression-bomb warnings as errors."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data))
            image.load()
            return image
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("cover is not a safe readable image from %s: %s" % (source, exc)) from exc


# ---------------------------------------------------------------- helpers
def slugify(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def clean_title(stem):
    s = unicodedata.normalize("NFKC", PREFIX_RE.sub("", stem))
    s = s.replace("⧸", "/").replace("⧹", "\\")
    s = " ".join(s.split())
    parts = re.split(r"\s*[:：]\s*", s)      # "Show N/M : Episode" -> "Episode"
    return parts[-1].strip() if len(parts) > 1 else s


def det_uuid(slug, key):
    return str(uuid.uuid5(NS, f"{slug}/{key}"))


def has_audio_files(folder):
    return folder.is_dir() and any(p.suffix.lower() in AUDIO_EXTS for p in folder.iterdir())


def download_feed(url, download_dir, extra):
    """Run the user's yt-dlp-podcast to fetch+renumber a feed; return its folder.

    The album folder name is computed with the SAME yt-dlp template
    (%(playlist_title)S) that yt-dlp-podcast uses, so the two agree.
    """
    download_dir = Path(download_dir).expanduser()
    download_dir.mkdir(parents=True, exist_ok=True)
    output = subprocess.run(
        [str(YTDLP), "--flat-playlist", "-I1", "--print",
         "%(playlist_title)S", "--output-na-placeholder", "", url],
        capture_output=True, text=True, check=True).stdout
    # Match the shell wrapper's `head -n1`: split only on the output LF, not on
    # Unicode separators that are valid filename characters, and keep spaces.
    name = output.partition("\n")[0]
    if not name.strip():
        sys.exit("could not read playlist title from %s" % url)
    download_root = download_dir.resolve()
    folder = (download_root / name).resolve()
    try:
        folder.relative_to(download_root)
    except ValueError:
        sys.exit("refusing playlist title that escapes the download directory: %r" % name)
    if folder == download_root:
        sys.exit("refusing playlist title that is the download directory itself: %r" % name)
    if has_audio_files(folder) and not os.access(download_dir, os.W_OK):
        print("download dir is read-only; using existing folder: %s" % folder)
        return folder
    script = YTDLP_PODCAST if YTDLP_PODCAST.exists() else shutil.which("yt-dlp-podcast")
    if not script:
        sys.exit("yt-dlp-podcast not found (looked at %s and PATH)" % YTDLP_PODCAST)
    # make the venv's yt-dlp visible to the (POSIX-sh) yt-dlp-podcast
    env = dict(os.environ,
               PATH=str(ROOT / "venv" / "bin") + os.pathsep + os.environ.get("PATH", ""))
    print("downloading %r -> %s" % (name, download_dir))
    subprocess.run([str(script), url, *extra], cwd=str(download_dir),
                   env=env, check=True)
    if not folder.is_dir():
        sys.exit("expected download folder not found: %s" % folder)
    return folder


def transcode(src, out):
    """-> 44.1kHz mono 64kbps ID3-free MP3 (device-ready)."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                    "-map", "0:a:0", "-map_metadata", "-1",
                    "-af", f"adelay={AUDIO_LEAD_IN_MS}:all=1",
                    "-ar", "44100", "-ac", "1", "-b:a", "64k",
                    "-codec:a", "libmp3lame",
                    "-id3v2_version", "0", "-write_id3v1", "0", str(out)],
                   check=True)


def require_voice():
    """Fail before any downloading or transcoding if TTS is not installed."""
    missing = [str(path) for path in (PIPER, MODEL) if not path.exists()]
    if missing:
        sys.exit("text-to-speech is not installed; missing:\n  %s\n"
                 "Run: python3 tools/bootstrap.py --with-voice"
                 % "\n  ".join(missing))


def tts_mp3(text, out):
    text = (text or "").strip()
    if not text:
        # clean_title can legitimately reduce a title to nothing ("01_" or
        # "01_Le debat :"); piper exits 1 on empty input.
        return silent_mp3(out)
    wav = out.with_suffix(".wav")
    result = subprocess.run([str(PIPER), "-m", str(MODEL), "-f", str(wav)],
                            input=text.encode("utf-8"), capture_output=True)
    if result.returncode != 0:
        sys.exit("piper failed (exit %d) synthesizing %r: %s"
                 % (result.returncode, text[:60],
                    result.stderr.decode("utf-8", "replace").strip()))
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav),
                    "-af", f"adelay={AUDIO_LEAD_IN_MS}:all=1",
                    "-ar", "44100", "-ac", "1", "-b:a", "64k",
                    "-codec:a", "libmp3lame",
                    "-id3v2_version", "0", "-write_id3v1", "0", str(out)],
                   check=True)
    wav.unlink(missing_ok=True)
    return out.read_bytes()


def silent_mp3(out, ms=AUDIO_LEAD_IN_MS):
    """A short silent MP3.

    Every stage must carry audio: STUdio substitutes a built-in blank MP3 for a
    null, but install_pack.py does not embed one, so a null-audio stage builds a
    pack that fails at install time. --no-episode-tts uses this instead.
    """
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "anullsrc=r=44100:cl=mono", "-t", "%.3f" % (ms / 1000.0),
                    "-b:a", "64k", "-codec:a", "libmp3lame",
                    "-id3v2_version", "0", "-write_id3v1", "0", str(out)],
                   check=True)
    return out.read_bytes()


def rss_cover_url(feed_url):
    """Best-effort artwork URL from an RSS feed."""
    try:
        root = ET.fromstring(fetch_cover_bytes(feed_url, limit=MAX_FEED_BYTES))
    except (
        requests.RequestException,
        ET.ParseError,
        DefusedXmlException,
        RecursionError,
        ValueError,
    ) as e:
        print("  cover: RSS artwork unavailable: %s" % e, file=sys.stderr)
        return None

    channel = root.find("channel")
    if channel is None:
        return None

    node = channel.find(ITUNES_IMAGE)
    if node is not None and node.get("href"):
        return node.get("href").strip()

    image = channel.find("image")
    if image is not None:
        url = image.findtext("url")
        if url and url.strip():
            return url.strip()

    for item in channel.findall("item"):
        node = item.find(ITUNES_IMAGE)
        if node is not None and node.get("href"):
            return node.get("href").strip()

    return None


def resolve_cover(args, title, dest):
    """Return cover art from RSS or an explicitly supplied path or URL."""
    if args.cover_file:
        cover_path = Path(args.cover_file)
        if not cover_path.is_file():
            sys.exit("Cover file not found: %s" % cover_path)
        if cover_path.stat().st_size > MAX_IMAGE_BYTES:
            sys.exit(
                "Cover file is too large (%d bytes; limit %d): %s"
                % (cover_path.stat().st_size, MAX_IMAGE_BYTES, cover_path)
            )
        try:
            return decode_image(cover_path.read_bytes(), cover_path)
        except ValueError as e:
            sys.exit(str(e))
    url = args.cover_url
    if url is None and str(args.src).startswith(("http://", "https://")):
        url = rss_cover_url(args.src)
        if url:
            print("  cover: from RSS artwork")

    if url is None:
        sys.exit(
            "No cover art for %r: the feed declares none, or the source is a "
            "local folder.\nPass --cover-file PATH or --cover-url URL." % title
        )

    try:
        data = fetch_cover_bytes(url, limit=MAX_IMAGE_BYTES)
        img = decode_image(data, url)
    except (requests.RequestException, ValueError) as e:
        sys.exit("Cover download failed for %s: %s" % (url, e))
    dest.write_bytes(data)
    return img


# ---------------------------------------------------------------- assets/graph
class AssetBag:
    def __init__(self):
        self.items = {}

    def add(self, data, ext):
        h = hashlib.sha1(data).hexdigest()
        self.items.setdefault(h, (f"{h}.{ext}", data))
        return f"{h}.{ext}"


def controls(w, o, h, p, a):
    return {"wheel": w, "ok": o, "home": h, "pause": p, "autoplay": a}


def build(args):
    require_voice()          # before any downloading or transcoding
    if str(args.src).startswith(("http://", "https://")):
        src = download_feed(args.src, args.download_dir, args.dl_extra)
    else:
        src = Path(args.src)
    require(src.is_dir(), "source is not a directory: %s" % src)
    title = args.title or src.name
    slug = args.slug or slugify(title)
    require(bool(SLUG_RE.fullmatch(slug)),
            "slug must contain only lowercase letters, digits, '.', '_' or '-'"
            " and must start with a letter or digit")
    outdir = Path(args.outdir or (ROOT / "build" / slug))
    work = outdir / "work"
    work.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in src.iterdir() if p.suffix.lower() in AUDIO_EXTS)
    if not files:
        sys.exit("no audio files in %s" % src)
    print("%s — %d episodes -> %s" % (title, len(files), outdir))

    cover_src = resolve_cover(args, title, work / "cover.bin")
    assets = AssetBag()

    # images
    cover_name = assets.add(LI.build_bmp(cover_src, rle=True), "bmp")
    ep_img = [assets.add(LI.build_bmp_from_rgb(
        LI.add_number_badge(LI.fit_cover(cover_src), i + 1), rle=True), "bmp")
        for i in range(len(files))]

    # audio: transcode stories, synthesize titles
    show_aud = assets.add(tts_mp3(title, work / "showtitle.mp3"), "mp3")
    choose_aud = assets.add(tts_mp3(args.menu_prompt, work / "choose.mp3"), "mp3")
    silent_aud = (assets.add(silent_mp3(work / "silence.mp3"), "mp3")
                  if args.no_episode_tts else None)
    ep_title_aud, ep_story_aud = [], []
    for i, f in enumerate(files, 1):
        story = work / ("story_%02d.mp3" % i)
        transcode(f, story)
        ep_story_aud.append(assets.add(story.read_bytes(), "mp3"))
        et = clean_title(f.stem)
        ep_title_aud.append(silent_aud if args.no_episode_tts else
                            assets.add(tts_mp3(et, work / ("t_%02d.mp3" % i)), "mp3"))
        print("  %02d  %s" % (i, et))

    # ---- node graph (menu pattern) ----
    stages, actions = [], []
    U = lambda k: det_uuid(slug, k)

    def stage(k, image, audio, ctrl, ok=None, home=None, square=False):
        u = U(k)
        stages.append({"uuid": u, "id": u, "type": "stage", "name": k,
                       "position": {"x": 0, "y": 0}, "squareOne": square,
                       "image": image, "audio": audio,
                       "okTransition": ok, "homeTransition": home,
                       "controlSettings": ctrl})

    def action(k, options):
        u = U("A_" + k)
        actions.append({"uuid": u, "id": u, "name": "A_" + k,
                        "position": {"x": 0, "y": 0}, "options": options})
        return u

    def tr(a, i):
        return {"actionNode": a, "optionIndex": i}

    tkeys = [f"title_{i+1}" for i in range(len(files))]
    a_menu = action("menu", [U(k) for k in tkeys])
    a_cover = action("cover", [U("menuIntro")])
    stage("cover", cover_name, show_aud, controls(1, 1, 0, 0, 0),
          ok=tr(a_cover, 0), square=True)
    stage("menuIntro", cover_name, choose_aud, controls(0, 0, 1, 0, 1),
          ok=tr(a_menu, 0))
    for i in range(len(files)):
        n = i + 1
        a_t = action(f"title_{n}", [U(f"story_{n}")])
        stage(f"title_{n}", ep_img[i], ep_title_aud[i], controls(1, 1, 1, 0, 0),
              ok=tr(a_t, 0))
        if n < len(files):
            ok_next = tr(action(f"story_{n}", [U(f"story_{n+1}")]), 0)
        else:
            ok_next = tr(a_menu, 0)   # wrap through the menu, not a 1-option action
        stage(f"story_{n}", None, ep_story_aud[i], controls(0, 0, 1, 1, 1),
              ok=ok_next, home=tr(a_menu, i))

    story = {"format": "v1", "version": 2, "title": title,
             "description": "%s — %d épisodes" % (title, len(files)),
             "uuid": U("pack"), "factoryDisabled": False,
             "nightModeAvailable": False,
             "stageNodes": stages, "actionNodes": actions}
    validate(story, assets)

    tb = io.BytesIO()
    LI.fit_cover(cover_src, 512, 512).convert("RGB").save(tb, "PNG")

    zip_path = outdir / f"{slug}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("story.json", json.dumps(story, ensure_ascii=False, indent=2))
        z.writestr("thumbnail.png", tb.getvalue())
        for name, data in assets.items.values():
            z.writestr(f"assets/{name}", data)
    print("OK -> %s (%.1f MB, %d assets)" %
          (zip_path, zip_path.stat().st_size / 1e6, len(assets.items)))
    return zip_path


def validate(story, assets):
    names = {nm for nm, _ in assets.items.values()}
    stages = {s["uuid"]: s for s in story["stageNodes"]}
    actions = {a["uuid"]: a for a in story["actionNodes"]}
    require(sum(bool(s.get("squareOne")) for s in story["stageNodes"]) == 1,
            "archive graph must contain exactly one squareOne stage")
    for a in story["actionNodes"]:
        require("id" in a and bool(a["options"]),
                f"{a.get('name', 'action')}: missing id or options")
        for o in a["options"]:
            require(o in stages, f"{a.get('name', 'action')}: dangling option {o}")
    for s in story["stageNodes"]:
        for k in ("wheel", "ok", "home", "pause", "autoplay"):
            require(k in s["controlSettings"], f"{s['name']} missing {k}")
        require(s["image"] is None or s["image"] in names,
                f"{s['name']} image missing")
        require(s["audio"] is None or s["audio"] in names,
                f"{s['name']} audio missing")
        for t in (s["okTransition"], s["homeTransition"]):
            if t:
                require(t["actionNode"] in actions,
                        f"{s['name']}: transition points to unknown action")
                a = actions[t["actionNode"]]
                require(0 <= t["optionIndex"] < len(a["options"]),
                        f"{s['name']}: transition option index is out of range")


def main():
    ap = argparse.ArgumentParser(
        description="Build a Lunii pack from a podcast folder OR an https feed URL",
        epilog="With a URL, downloads via yt-dlp-podcast first. Forward extra "
               "yt-dlp options after '--', e.g.:  podcast2lunii URL -- --playlist-items 1:5")
    ap.add_argument("src", help="podcast folder, or https RSS feed URL")
    ap.add_argument("dl_extra", nargs="*",
                    help="(URL mode) extra args forwarded to yt-dlp-podcast, after --")
    ap.add_argument("--download-dir", default="~/Downloads/podcasts",
                    help="(URL mode) where to download (default: ~/Downloads/podcasts)")
    ap.add_argument("--title")
    ap.add_argument("--slug")
    ap.add_argument("-o", "--outdir")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--cover-url")
    g.add_argument("--cover-file")
    ap.add_argument("--menu-prompt", default="Choisis une histoire")
    ap.add_argument("--no-episode-tts", action="store_true")
    args = ap.parse_args()
    try:
        build(args)
    except PackBuildError as exc:
        ap.error(str(exc))


if __name__ == "__main__":
    main()
