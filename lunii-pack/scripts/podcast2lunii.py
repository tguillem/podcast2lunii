#!/usr/bin/env python3
"""podcast2lunii — turn a downloaded podcast folder into a Lunii/STUdio pack .zip.

One deterministic pass, no LLM in the loop:
  folder of numbered audio  ->  transcode + title-voice TTS + numbered covers
                            ->  story.json menu graph  ->  <slug>.zip

The only input the script cannot derive is the cover art. Supply it yourself
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
import unicodedata
import uuid
import zipfile
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lunii_image as LI

ROOT = Path(__file__).resolve().parent.parent
PIPER = ROOT / "venv" / "bin" / "piper"
MODEL = ROOT / "voices" / "fr_FR-siwis-medium.onnx"
YTDLP = ROOT / "venv" / "bin" / "yt-dlp"
YTDLP_PODCAST = ROOT.parent / "yt-dlp-podcast"      # the user's existing script
NS = uuid.UUID("1b671a64-40d5-491e-99b0-da01ff1f3341")

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".ogg", ".oga", ".opus", ".flac"}
PREFIX_RE = re.compile(r"^\d+_")


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


def download_feed(url, download_dir, extra):
    """Run the user's yt-dlp-podcast to fetch+renumber a feed; return its folder.

    The album folder name is computed with the SAME yt-dlp template
    (%(playlist_title)S) that yt-dlp-podcast uses, so the two agree.
    """
    download_dir = Path(download_dir).expanduser()
    download_dir.mkdir(parents=True, exist_ok=True)
    name = subprocess.run(
        [str(YTDLP), "--flat-playlist", "-I1", "--print",
         "%(playlist_title)S", url],
        capture_output=True, text=True, check=True).stdout.splitlines()
    name = (name[0].strip() if name else "")
    if not name:
        sys.exit("could not read playlist title from %s" % url)
    script = YTDLP_PODCAST if YTDLP_PODCAST.exists() else shutil.which("yt-dlp-podcast")
    if not script:
        sys.exit("yt-dlp-podcast not found (looked at %s and PATH)" % YTDLP_PODCAST)
    # make the venv's yt-dlp visible to the (POSIX-sh) yt-dlp-podcast
    env = dict(os.environ,
               PATH=str(ROOT / "venv" / "bin") + os.pathsep + os.environ.get("PATH", ""))
    print("downloading %r -> %s" % (name, download_dir))
    subprocess.run([str(script), url, *extra], cwd=str(download_dir),
                   env=env, check=True)
    folder = download_dir / name
    if not folder.is_dir():
        sys.exit("expected download folder not found: %s" % folder)
    return folder


def transcode(src, out):
    """-> 44.1kHz mono 64kbps ID3-free MP3 (device-ready)."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                    "-map", "0:a:0", "-map_metadata", "-1",
                    "-ar", "44100", "-ac", "1", "-b:a", "64k",
                    "-codec:a", "libmp3lame",
                    "-id3v2_version", "0", "-write_id3v1", "0", str(out)],
                   check=True)


def tts_mp3(text, out):
    wav = out.with_suffix(".wav")
    subprocess.run([str(PIPER), "-m", str(MODEL), "-f", str(wav)],
                   input=text.encode("utf-8"), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav),
                    "-ar", "44100", "-ac", "1", "-b:a", "64k",
                    "-codec:a", "libmp3lame",
                    "-id3v2_version", "0", "-write_id3v1", "0", str(out)],
                   check=True)
    wav.unlink(missing_ok=True)
    return out.read_bytes()


def resolve_cover(args, title, dest):
    """Return cover art supplied by a local path or URL."""
    if args.cover_file:
        return Image.open(args.cover_file)
    url = args.cover_url
    if url is None:
        sys.exit(
            "No cover art for %r. Pass --cover-file PATH or --cover-url URL."
            % title
        )
    data = requests.get(url, timeout=40).content
    dest.write_bytes(data)
    return Image.open(io.BytesIO(data))


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
    if str(args.src).startswith(("http://", "https://")):
        src = download_feed(args.src, args.download_dir, args.dl_extra)
    else:
        src = Path(args.src)
    title = args.title or src.name
    slug = args.slug or slugify(title)
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
    ep_title_aud, ep_story_aud = [], []
    for i, f in enumerate(files, 1):
        story = work / ("story_%02d.mp3" % i)
        transcode(f, story)
        ep_story_aud.append(assets.add(story.read_bytes(), "mp3"))
        et = clean_title(f.stem)
        ep_title_aud.append(None if args.no_episode_tts else
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
        nxt = U(f"story_{n+1}") if n < len(files) else U(tkeys[0])
        a_s = action(f"story_{n}", [nxt])
        stage(f"story_{n}", None, ep_story_aud[i], controls(0, 0, 1, 1, 1),
              ok=tr(a_s, 0), home=tr(a_menu, i))

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
    assert sum(bool(s.get("squareOne")) for s in story["stageNodes"]) == 1
    for a in story["actionNodes"]:
        assert "id" in a and a["options"]
        for o in a["options"]:
            assert o in stages, "dangling option"
    for s in story["stageNodes"]:
        for k in ("wheel", "ok", "home", "pause", "autoplay"):
            assert k in s["controlSettings"], f"{s['name']} missing {k}"
        assert s["image"] in (None, *names), f"{s['name']} image missing"
        assert s["audio"] in (None, *names), f"{s['name']} audio missing"
        for t in (s["okTransition"], s["homeTransition"]):
            if t:
                a = actions[t["actionNode"]]
                assert 0 <= t["optionIndex"] < len(a["options"])


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
    build(ap.parse_args())


if __name__ == "__main__":
    main()
