#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Thomas Guillem
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Offline regression tests. No network, no device, no Lunii hardware.

Run:  python3 -m unittest discover -s tests -v

Each test here exists because the behaviour it pins was once wrong. The
ordering and deletion cases in particular encode real bugs: podcast-renumber
used to read a date like "04/03" as "part 4 of 3" and delete the episodes it
decided were not part of the series.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "lunii-pack" / "scripts"
HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves a class's __module__ through sys.modules, so the
    # module has to be registered before its body runs.
    sys.modules[name] = module
    loader.exec_module(module)
    return module


sys.path.insert(0, str(SCRIPTS))
renumber = load(ROOT / "podcast-renumber", "renumber_under_test")
install_pack = load(SCRIPTS / "install_pack.py", "install_pack_under_test")
lunii_image = load(SCRIPTS / "lunii_image.py", "lunii_image_under_test")


def write_feed(directory, titles):
    items = "\n".join(
        "<item><title>%s</title><guid>g%d</guid>"
        "<pubDate>Sun, 06 Apr 2025 %02d:00:00 GMT</pubDate></item>" % (t, i, 23 - i)
        for i, t in enumerate(titles)
    )
    feed = directory / "feed.xml"
    feed.write_text(
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<title>Show</title>%s</channel></rss>" % items
    )
    return feed


def write_audio(folder, titles, seconds="0.2"):
    folder.mkdir(parents=True, exist_ok=True)
    for title in titles:
        name = title.replace("/", "⧸") + ".mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", "anullsrc=r=44100:cl=mono", "-t", seconds, "-q:a", "9",
             str(folder / name)],
            check=True,
        )


def run_renumber(feed, folder, *extra):
    argv = ["podcast-renumber", "--feed", str(feed), *extra, str(folder)]
    saved, sys.argv = sys.argv, argv
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            renumber.main()
    finally:
        sys.argv = saved


def numbered(folder):
    return sorted(p.name for p in folder.iterdir() if p.suffix == ".mp3")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required to synthesize test audio")
class OrderingTests(unittest.TestCase):
    """A 'N/M' in a title is a part marker only when it really is one."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="renumber-test-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.audio = self.dir / "audio"

    def build(self, titles):
        write_audio(self.audio, titles)
        return write_feed(self.dir, titles)

    def test_dates_are_not_part_numbers(self):
        """A daily feed spanning a month boundary must keep every episode.

        Regression: "du 04/03" parsed as part 4 of 3, so the minority month was
        classified as extras and deleted.
        """
        titles = ["Bulletin du %02d/03" % d for d in range(15, 3, -1)]
        titles += ["Bulletin du %02d/02" % d for d in range(27, 19, -1)]
        run_renumber(self.build(titles), self.audio)
        self.assertEqual(len(numbered(self.audio)), 20)
        self.assertFalse((self.audio / ".excluded").exists())

    def test_early_month_dates_are_not_part_numbers(self):
        """Denominator 3 with numerators {1,2} once satisfied the old rule."""
        titles = ["Bulletin du 02/03", "Bulletin du 01/03", "Bulletin du 28/02"]
        run_renumber(self.build(titles), self.audio)
        self.assertEqual(len(numbered(self.audio)), 3)
        self.assertFalse((self.audio / ".excluded").exists())

    def test_partial_series_keeps_forward_order(self):
        """A feed usually exposes only a window of a long series.

        Regression: requiring nearly every part rejected the window and fell
        back to reversing the feed, which plays the story backwards.
        """
        titles = ["Serie %d/10 : p%d" % (k, k) for k in range(1, 6)]
        run_renumber(self.build(titles), self.audio)
        self.assertTrue(numbered(self.audio)[0].startswith("01_Serie 1"))
        self.assertTrue(numbered(self.audio)[-1].startswith("05_Serie 5"))

    def test_mid_window_series_keeps_forward_order(self):
        titles = ["Serie %d/10 : p%d" % (k, k) for k in range(3, 8)]
        run_renumber(self.build(titles), self.audio)
        self.assertTrue(numbered(self.audio)[0].startswith("01_Serie 3"))

    def test_real_series_orders_parts_and_sets_extras_aside(self):
        titles = ["Histoire 1/3 : a", "Histoire 2/3 : b", "Histoire 3/3 : c",
                  "Histoire : Bonus"]
        run_renumber(self.build(titles), self.audio)
        self.assertTrue(numbered(self.audio)[0].startswith("01_Histoire 1"))
        self.assertEqual(len(numbered(self.audio)), 3)
        self.assertEqual(
            [p.name for p in (self.audio / ".excluded").iterdir()],
            ["Histoire : Bonus.mp3"],
        )

    def test_second_run_is_a_no_op(self):
        titles = ["Histoire 1/3 : a", "Histoire 2/3 : b", "Histoire 3/3 : c"]
        feed = self.build(titles)
        run_renumber(feed, self.audio)
        first = numbered(self.audio)
        run_renumber(feed, self.audio)
        self.assertEqual(first, numbered(self.audio))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required to synthesize test audio")
class NeverDeletesTests(unittest.TestCase):
    """No input, valid or not, may cost the user an audio file."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="renumber-safety-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.audio = self.dir / "audio"

    def test_no_unlink_call_exists(self):
        self.assertNotIn("unlink", (ROOT / "podcast-renumber").read_text())

    def test_every_file_survives(self):
        titles = ["Histoire 1/3 : a", "Histoire 2/3 : b", "Histoire 3/3 : c",
                  "Bande-annonce", "Histoire : Bonus"]
        write_audio(self.audio, titles)
        run_renumber(write_feed(self.dir, titles), self.audio)
        survivors = list(self.audio.rglob("*.mp3"))
        self.assertEqual(len(survivors), len(titles))

    def test_interrupted_rename_preserves_audio(self):
        """Fault-inject after every rename; both streams must survive.

        Regression: rollback recorded each move after making it, so an
        interrupt in that gap let cleanup rename one file over another.
        """
        import mutagen
        from mutagen.easyid3 import EasyID3
        from mutagen.id3 import ID3NoHeaderError

        def build(target):
            target.mkdir(parents=True)
            for name, title, secs in (("01_same.mp3", "Beta", "0.2"),
                                      ("02_same.mp3", "Alpha", "0.4")):
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                     "-i", "anullsrc=r=44100:cl=mono", "-t", secs, "-q:a", "9",
                     str(target / name)], check=True)
                try:
                    tag = EasyID3(target / name)
                except ID3NoHeaderError:
                    tag = EasyID3()
                    tag.save(target / name)
                    tag = EasyID3(target / name)
                tag["title"] = title
                tag.save()

        def durations(folder):
            out = []
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    audio = mutagen.File(path)
                    out.append("%.2f" % audio.info.length if audio else "other")
            return sorted(out)

        feed = self.dir / "swap.xml"
        feed.write_text(
            '<?xml version="1.0"?><rss version="2.0"><channel><title>S</title>'
            "<item><title>Beta</title><guid>b</guid></item>"
            "<item><title>Alpha</title><guid>a</guid></item></channel></rss>")

        reference = None
        for stop_after in range(1, 6):
            folder = self.dir / ("fi%d" % stop_after)
            build(folder)
            if reference is None:
                reference = durations(folder)
            real, seen = Path.rename, {"n": 0}

            def boom(self, target, _real=real, _seen=seen, _k=stop_after):
                _seen["n"] += 1
                result = _real(self, target)     # let the move happen ...
                if _seen["n"] == _k:             # ... then interrupt
                    raise KeyboardInterrupt
                return result

            Path.rename = boom
            try:
                run_renumber(feed, folder)
            except BaseException:
                pass
            finally:
                Path.rename = real
            self.assertEqual(durations(folder), reference,
                             "audio lost when interrupted after rename #%d" % stop_after)


class ImageTests(unittest.TestCase):
    def test_bmp_header_matches_what_the_installer_requires(self):
        from PIL import Image
        data = lunii_image.build_bmp(Image.new("RGB", (600, 600), (10, 20, 30)), rle=True)
        self.assertEqual(data[:2], b"BM")
        width, height = struct.unpack_from("<ii", data, 18)
        bits, compression = struct.unpack_from("<HI", data, 28)
        self.assertEqual((width, height, bits, compression), (320, 240, 4, 2))
        install_pack.validate_bmp(data, "generated")

    def test_three_digit_badge_stays_inside_the_canvas(self):
        from PIL import Image, ImageDraw
        base = lunii_image.fit_cover(Image.new("RGB", (600, 600), (0, 0, 0)))
        for number in (1, 42, 100, 999):
            badged = lunii_image.add_number_badge(base, number)
            self.assertEqual(badged.size, (lunii_image.W, lunii_image.H))
            install_pack.validate_bmp(
                lunii_image.build_bmp_from_rgb(badged, rle=True), "badge %d" % number)


class PackIndexTests(unittest.TestCase):
    def test_pi_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            pi = Path(tmp) / ".pi"
            first = [uuid.uuid4() for _ in range(3)]
            pi.write_bytes(b"".join(u.bytes for u in first))
            self.assertEqual(install_pack.read_pack_index(pi), first)
            second = first + [uuid.uuid4()]
            install_pack.write_pack_index_atomic(pi, second)
            self.assertEqual(install_pack.read_pack_index(pi), second)

    def test_rejects_truncated_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            pi = Path(tmp) / ".pi"
            pi.write_bytes(b"\x00" * 17)
            with self.assertRaises(install_pack.PackError):
                install_pack.read_pack_index(pi)

    def test_archive_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.zip"
            with zipfile.ZipFile(bad, "w") as zf:
                zf.writestr("story.json", json.dumps({"uuid": str(uuid.uuid4())}))
                zf.writestr("assets/../escape.mp3", b"x")
            with self.assertRaises(install_pack.PackError):
                install_pack.read_archive(bad)


if __name__ == "__main__":
    unittest.main()
