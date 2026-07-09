#!/usr/bin/env python3
"""Build a STUdio/Lunii archive pack (.zip) from a prepped show directory.

Input (under build/<slug>/):
  titles.json        {show_title, episodes:[{n,title,audio}]}
  source/cover.bin   show cover art (any ImageIO-decodable format)
Output:
  build/<slug>/<slug>.zip   importable in studio-web-ui

Navigation graph (menu pattern; validated against STUdio ArchiveStoryPackReader):

  cover  (squareOne; cover.bmp + spoken show title; wheel/ok, no home)
    -OK-> menuIntro ("Choisis une histoire"; autoplay)
           -auto-> MENU action  (one option per episode)
                     -> title_i (numbered bmp + spoken episode title; wheel scrolls)
                          -OK-> story_i (episode audio; no image)
                                  autoplay/OK-> next episode's story (last loops to menu)
                                  HOME-------> back to the menu at episode i

Assets are content-addressed: filename = sha1(bytes).ext, so identical assets
(e.g. the reused cover image) are stored once. MP3s are emitted ID3-free.
"""
import hashlib
import io
import json
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lunii_image as LI

ROOT = Path(__file__).resolve().parent.parent          # lunii-pack/
PIPER = ROOT / "venv" / "bin" / "piper"
MODEL = ROOT / "voices" / "fr_FR-siwis-medium.onnx"
# fixed namespace -> deterministic UUIDs (re-running yields an identical pack)
NS = uuid.UUID("1b671a64-40d5-491e-99b0-da01ff1f3341")


def det_uuid(slug, key):
    return str(uuid.uuid5(NS, f"{slug}/{key}"))


def tts_mp3(text, out_path):
    """Synthesize French `text` with Piper -> 44.1kHz mono ID3-free MP3."""
    wav = out_path.with_suffix(".wav")
    subprocess.run([str(PIPER), "-m", str(MODEL), "-f", str(wav)],
                   input=text.encode("utf-8"), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav),
                    "-ar", "44100", "-ac", "1", "-b:a", "64k",
                    "-codec:a", "libmp3lame",
                    "-id3v2_version", "0", "-write_id3v1", "0", str(out_path)],
                   check=True)
    wav.unlink(missing_ok=True)
    return out_path.read_bytes()


class AssetBag:
    """Content-addressed asset store: name = sha1(bytes).ext, deduped."""
    def __init__(self):
        self.items = {}                       # sha1 -> (name, bytes)

    def add(self, data, ext):
        h = hashlib.sha1(data).hexdigest()
        name = f"{h}.{ext}"
        self.items.setdefault(h, (name, data))
        return name


def controls(wheel, ok, home, pause, autoplay):
    return {"wheel": wheel, "ok": ok, "home": home,
            "pause": pause, "autoplay": autoplay}


def build(slug):
    show_dir = ROOT / "build" / slug
    meta = json.loads((show_dir / "titles.json").read_text())
    show_title = meta["show_title"]
    eps = meta["episodes"]
    work = show_dir / "work"
    work.mkdir(exist_ok=True)
    assets = AssetBag()

    print(f"[{slug}] {show_title} — {len(eps)} episodes")

    # ---- images ----
    cover_src = Image.open(show_dir / "source" / "cover.bin")
    cover_name = assets.add(LI.build_bmp(cover_src, rle=True), "bmp")
    ep_img = []
    for ep in eps:
        badged = LI.add_number_badge(LI.fit_cover(cover_src), ep["n"])
        ep_img.append(assets.add(LI.build_bmp_from_rgb(badged, rle=True), "bmp"))
    print("  images: cover + %d numbered episode covers" % len(eps))

    # ---- audio (TTS titles + story bodies) ----
    show_aud = assets.add(tts_mp3(show_title, work / "showtitle.mp3"), "mp3")
    choose_aud = assets.add(tts_mp3("Choisis une histoire", work / "choose.mp3"), "mp3")
    ep_title_aud, ep_story_aud = [], []
    for ep in eps:
        ep_title_aud.append(assets.add(
            tts_mp3(ep["title"], work / f"eptitle_{ep['n']:02d}.mp3"), "mp3"))
        ep_story_aud.append(assets.add((ROOT / ep["audio"]).read_bytes(), "mp3"))
    print("  audio: show title + menu prompt + %d episode titles + %d stories"
          % (len(eps), len(eps)))

    # ---- node graph ----
    stage_nodes, action_nodes = [], []

    def U(key):
        return det_uuid(slug, key)

    def stage(key, image, audio, ctrl, ok=None, home=None, square=False):
        u = U(key)
        stage_nodes.append({
            "uuid": u, "id": u, "type": "stage", "name": key,
            "position": {"x": 0, "y": 0}, "squareOne": square,
            "image": image, "audio": audio,
            "okTransition": ok, "homeTransition": home,
            "controlSettings": ctrl,
        })

    def action(key, options):
        u = U("A_" + key)
        action_nodes.append({"uuid": u, "id": u, "name": "A_" + key,
                             "position": {"x": 0, "y": 0}, "options": options})
        return u

    def trans(action_uuid, idx):
        return {"actionNode": action_uuid, "optionIndex": idx}

    title_keys = [f"title_{ep['n']}" for ep in eps]
    a_menu = action("menu", [U(k) for k in title_keys])
    a_cover = action("cover", [U("menuIntro")])

    stage("cover", cover_name, show_aud,
          controls(True, True, False, False, False),
          ok=trans(a_cover, 0), square=True)
    stage("menuIntro", cover_name, choose_aud,
          controls(False, False, True, False, True),
          ok=trans(a_menu, 0))

    for i, ep in enumerate(eps):
        n = ep["n"]
        a_title = action(f"title_{n}", [U(f"story_{n}")])
        stage(f"title_{n}", ep_img[i], ep_title_aud[i],
              controls(True, True, True, False, False),
              ok=trans(a_title, 0))
        # after this story: next episode's story, or (last) loop back to menu
        nxt = U(f"story_{eps[i + 1]['n']}") if i + 1 < len(eps) else U(title_keys[0])
        a_story = action(f"story_{n}", [nxt])
        stage(f"story_{n}", None, ep_story_aud[i],
              controls(False, False, True, True, True),
              ok=trans(a_story, 0),
              home=trans(a_menu, i))          # HOME -> menu at this episode slot

    story = {
        "format": "v1",
        "version": 2,
        "title": show_title,
        "description": f"{show_title} — {len(eps)} épisodes",
        "uuid": U("pack"),
        "factoryDisabled": False,
        "nightModeAvailable": False,
        "stageNodes": stage_nodes,
        "actionNodes": action_nodes,
    }

    # ---- thumbnail for the STUdio library (square PNG, no alpha) ----
    tb = io.BytesIO()
    LI.fit_cover(cover_src, 512, 512).convert("RGB").save(tb, "PNG")
    thumb_bytes = tb.getvalue()

    validate(story, assets)

    out_zip = show_dir / f"{slug}.zip"
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("story.json", json.dumps(story, ensure_ascii=False, indent=2))
        z.writestr("thumbnail.png", thumb_bytes)
        for name, data in assets.items.values():
            z.writestr(f"assets/{name}", data)

    total = out_zip.stat().st_size
    print("  assets: %d unique | story nodes: %d | action nodes: %d"
          % (len(assets.items), len(stage_nodes), len(action_nodes)))
    print("  -> %s (%.1f MB)" % (out_zip, total / 1e6))
    return out_zip


def validate(story, assets):
    """Fail loudly on the mistakes STUdio's reader NPEs on."""
    names = {nm for nm, _ in assets.items.values()}
    stages = {s["uuid"]: s for s in story["stageNodes"]}
    actions = {a["uuid"]: a for a in story["actionNodes"]}
    assert story["stageNodes"], "no stage nodes"
    squares = [s for s in story["stageNodes"] if s.get("squareOne")]
    assert len(squares) == 1, f"expected exactly 1 squareOne node, got {len(squares)}"
    for a in story["actionNodes"]:
        assert "id" in a, "action node missing 'id' (reader keys on id)"
        for opt in a["options"]:
            assert opt in stages, f"action option -> unknown stage {opt}"
    for s in story["stageNodes"]:
        cs = s["controlSettings"]
        for k in ("wheel", "ok", "home", "pause", "autoplay"):
            assert k in cs, f"{s['name']}: controlSettings missing {k}"
        for img in (s["image"],):
            assert img is None or img in names, f"{s['name']}: image asset {img} missing"
        assert s["audio"] is None or s["audio"] in names, f"{s['name']}: audio missing"
        for t in (s["okTransition"], s["homeTransition"]):
            if t is not None:
                a = actions.get(t["actionNode"])
                assert a is not None, f"{s['name']}: transition to unknown action"
                assert 0 <= t["optionIndex"] < len(a["options"]), \
                    f"{s['name']}: optionIndex {t['optionIndex']} out of range"
    print("  validation: OK")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "example-show")
