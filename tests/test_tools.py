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

    def _swap_fixture(self, target):
        """Two files whose plan swaps their prefixes, with distinct audio."""
        from mutagen.easyid3 import EasyID3
        from mutagen.id3 import ID3NoHeaderError
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
        feed = target.parent / ("swap-%s.xml" % target.name)
        feed.write_text(
            '<?xml version="1.0"?><rss version="2.0"><channel><title>S</title>'
            "<item><title>Beta</title><guid>b</guid></item>"
            "<item><title>Alpha</title><guid>a</guid></item></channel></rss>")
        return feed

    @staticmethod
    def _durations(folder):
        import mutagen
        out = []
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                audio = mutagen.File(path)
                out.append("%.2f" % audio.info.length if audio else "other:" + path.name)
        return sorted(out)

    def test_clean_run_actually_swaps_the_two_files(self):
        """Control: without this, the interrupt test passes with no renaming."""
        folder = self.audio
        feed = self._swap_fixture(folder)
        run_renumber(feed, folder)
        import mutagen
        self.assertEqual(
            {p.name: "%.2f" % mutagen.File(p).info.length
             for p in folder.iterdir() if p.suffix == ".mp3"},
            {"01_same.mp3": "0.44", "02_same.mp3": "0.24"},
            "the plan must actually swap the two prefixes",
        )

    def test_interrupted_rename_preserves_audio(self):
        """Fault-inject after every rename; no audio may be lost.

        Regression: rollback recorded each move after making it, so an
        interrupt in that gap let cleanup rename one file over another.
        """
        reference = None
        fired_at_least_once = False
        for stop_after in range(1, 6):
            folder = self.dir / ("fi%d" % stop_after)
            feed = self._swap_fixture(folder)
            if reference is None:
                reference = self._durations(folder)
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

            if seen["n"] >= stop_after:
                fired_at_least_once = True
            self.assertEqual(self._durations(folder), reference,
                             "audio lost when interrupted after rename #%d" % stop_after)
            self.assertEqual(
                [p.name for p in folder.glob(".renum.*")], [],
                "staging file stranded after interrupt #%d" % stop_after)
        self.assertTrue(fired_at_least_once,
                        "the fault injector never fired: the test proves nothing")

    def test_three_way_rename_cycle_rewinds_completely(self):
        """An interlocking cycle needs more than one rewind pass."""
        import mutagen
        from mutagen.easyid3 import EasyID3
        from mutagen.id3 import ID3NoHeaderError
        folder = self.dir / "cycle"
        folder.mkdir(parents=True)
        order = [("01_x.mp3", "C", "0.2"), ("02_x.mp3", "A", "0.4"), ("03_x.mp3", "B", "0.6")]
        for name, title, secs in order:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                            "-i", "anullsrc=r=44100:cl=mono", "-t", secs,
                            "-q:a", "9", str(folder / name)], check=True)
            try:
                tag = EasyID3(folder / name)
            except ID3NoHeaderError:
                tag = EasyID3(); tag.save(folder / name); tag = EasyID3(folder / name)
            tag["title"] = title
            tag.save()
        feed = self.dir / "cycle.xml"
        feed.write_text(
            '<?xml version="1.0"?><rss version="2.0"><channel><title>S</title>'
            "<item><title>C</title><guid>c</guid></item>"
            "<item><title>B</title><guid>b</guid></item>"
            "<item><title>A</title><guid>a</guid></item></channel></rss>")
        reference = self._durations(folder)
        for stop_after in range(1, 7):
            work = self.dir / ("cyc%d" % stop_after)
            shutil.copytree(folder, work)
            real, seen = Path.rename, {"n": 0}

            def boom(self, target, _real=real, _seen=seen, _k=stop_after):
                _seen["n"] += 1
                result = _real(self, target)
                if _seen["n"] == _k:
                    raise KeyboardInterrupt
                return result

            Path.rename = boom
            try:
                run_renumber(feed, work)
            except BaseException:
                pass
            finally:
                Path.rename = real
            self.assertEqual(self._durations(work), reference,
                             "audio lost in a 3-cycle at rename #%d" % stop_after)
            self.assertEqual([p.name for p in work.glob(".renum.*")], [],
                             "staging file stranded in a 3-cycle at #%d" % stop_after)


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
            stage_uuid = str(uuid.uuid4())
            story = {
                "format": "v1", "version": 2, "uuid": str(uuid.uuid4()),
                "stageNodes": [{
                    "uuid": stage_uuid, "id": stage_uuid, "name": "cover",
                    "squareOne": True, "image": None, "audio": None,
                    "okTransition": None, "homeTransition": None,
                    "controlSettings": {"wheel": 0, "ok": 1, "home": 0,
                                        "pause": 0, "autoplay": 0},
                }],
                "actionNodes": [],
            }
            # Sanity: this story is otherwise valid, so the traversal member is
            # the only reason read_archive may reject the archive.
            good = Path(tmp) / "good.zip"
            with zipfile.ZipFile(good, "w") as zf:
                zf.writestr("story.json", json.dumps(story))
            install_pack.read_archive(good)

            with zipfile.ZipFile(bad, "w") as zf:
                zf.writestr("story.json", json.dumps(story))
                zf.writestr("assets/../escape.mp3", b"x")
            with self.assertRaises(install_pack.PackError):
                install_pack.read_archive(bad)

            # A traversal member outside assets/ is never extracted, so this
            # pins the generic name guard rather than the asset-name one.
            for member in ("../escape.mp3", "a/../../escape.mp3", "/abs.mp3"):
                with self.subTest(member=member):
                    hostile = Path(tmp) / ("h%d.zip" % abs(hash(member)))
                    with zipfile.ZipFile(hostile, "w") as zf:
                        zf.writestr("story.json", json.dumps(story))
                        zf.writestr(member, b"x")
                    with self.assertRaises(install_pack.PackError):
                        install_pack.read_archive(hostile)


class ClassifierTests(unittest.TestCase):
    """Pin is_part_series directly.

    An earlier suite exercised it only through end-to-end fixtures, so
    replacing its whole body with `return True` still passed.
    """

    @staticmethod
    def items(pairs):
        import datetime
        out = []
        for title, pub in pairs:
            out.append({
                "norm": renumber.norm(title),
                "pub": datetime.datetime(2025, pub[0], pub[1]) if pub else None,
            })
        return out

    def classify(self, pairs):
        items = self.items(pairs)
        dominant, marks = renumber.series_marks(items)
        if dominant is None:
            return False
        return renumber.is_part_series(items, marks, dominant)

    def test_rejects_dates_in_any_wording(self):
        for shape in ("Bulletin du %s", "Bulletin %s",
                      "Les Matins - %s/2025", "Journal du soir, le %s"):
            with self.subTest(shape=shape):
                self.assertFalse(self.classify([
                    (shape % "02/03", (3, 2)),
                    (shape % "01/03", (3, 1)),
                    (shape % "28/02", (2, 28)),
                ]), "a date must never be read as a part number")

    def test_rejects_a_month_window_whose_days_look_like_parts(self):
        pairs = [("Journal - %02d/12" % d, (12, d)) for d in range(12, 0, -1)]
        pairs += [("Journal - %02d/11" % d, (11, d)) for d in range(30, 24, -1)]
        self.assertFalse(self.classify(pairs))

    def test_accepts_a_real_series(self):
        self.assertTrue(self.classify(
            [("Histoire %d/3 : x" % k, (4, 6)) for k in (1, 2, 3)]))

    def test_accepts_a_partial_window(self):
        self.assertTrue(self.classify(
            [("Serie %d/10" % k, (4, 6)) for k in range(1, 6)]))

    def test_accepts_a_series_with_a_missing_part(self):
        self.assertTrue(self.classify(
            [("S %d/8" % k, (4, 6)) for k in (1, 2, 4, 5)]))

    def test_prefers_the_part_token_over_a_date_in_the_subtitle(self):
        pairs = [("Od 1/4 : un", (4, 6)), ("Od 2/4 : deux", (4, 6)),
                 ("Od 3/4 : le carnet du 04/03", (4, 6)), ("Od 4/4 : fin", (4, 6))]
        items = self.items(pairs)
        dominant, marks = renumber.series_marks(items)
        self.assertEqual(dominant, "4")
        self.assertEqual([m.group(1) for m in marks], ["1", "2", "3", "4"])
        self.assertTrue(renumber.is_part_series(items, marks, dominant))

    def test_rejects_when_the_leftovers_carry_their_own_numbering(self):
        """A real story's extras are a "Bonus"; a date feed's are other months."""
        self.assertFalse(self.classify(
            [("A %d/3" % k, (4, 6)) for k in (1, 2, 3)] +
            [("B %d/5" % k, (4, 6)) for k in (1, 2)]))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required to synthesize test audio")
class FallbackOrderTests(unittest.TestCase):
    def test_unnumbered_feed_plays_oldest_first(self):
        """Pins the direction of the no-series fallback, not just the set."""
        directory = Path(tempfile.mkdtemp(prefix="fallback-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        audio = directory / "audio"
        titles = ["Newest", "Middle", "Oldest"]     # feeds list newest first
        write_audio(audio, titles)
        run_renumber(write_feed(directory, titles), audio)
        self.assertEqual(
            [p.name for p in sorted(audio.iterdir()) if p.suffix == ".mp3"],
            ["01_Oldest.mp3", "02_Middle.mp3", "03_Newest.mp3"])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required to synthesize test audio")
class SquareOneTests(unittest.TestCase):
    def test_square_one_stage_is_serialized_first(self):
        """The device boots the first stage, whatever order the archive used."""
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            mp3 = work / "a.mp3"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                 "-i", "anullsrc=r=44100:cl=mono", "-t", "0.2",
                 "-b:a", "64k", "-codec:a", "libmp3lame",
                 "-id3v2_version", "0", "-write_id3v1", "0", str(mp3)],
                check=True)
            audio = mp3.read_bytes()

            from PIL import Image
            bmp = lunii_image.build_bmp(Image.new("RGB", (320, 240), (9, 9, 9)), rle=True)

            def stage(name, square, image=None):
                key = str(uuid.uuid5(uuid.NAMESPACE_DNS, name))
                return {"uuid": key, "id": key, "name": name, "squareOne": square,
                        "image": image, "audio": "a.mp3", "okTransition": None,
                        "homeTransition": None,
                        "controlSettings": {"wheel": 0, "ok": 1, "home": 0,
                                            "pause": 0, "autoplay": 0}}

            # squareOne deliberately stored second.
            story = {"format": "v1", "version": 2, "uuid": str(uuid.uuid4()),
                     "stageNodes": [stage("menu", False),
                                    stage("cover", True, "c.bmp")],
                     "actionNodes": []}
            pack = install_pack.ArchivePack(
                story=story, assets={"a.mp3": audio, "c.bmp": bmp})
            summary = install_pack.build_fs_pack(pack, work_root=work / "out")
            self.assertEqual(summary.stage_count, 2)
            # ni is a 512-byte header then one 44-byte record per stage, in
            # serialization order. Only the cover carries an image, so the
            # first record's image index says which stage leads.
            ni = (summary.work_dir / "ni").read_bytes()
            first_image_index = struct.unpack_from("<i", ni, 512)[0]
            self.assertEqual(first_image_index, 0,
                             "squareOne (the only stage with an image) must lead")


class RssOnlyOrderTests(unittest.TestCase):
    def test_url_mode_fetches_only_the_rss_feed(self):
        """A channel or item link must not trigger a second HTTP request."""
        feed_url = "https://feeds.example/show.xml"
        data = (
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            "<title>Synthetic show</title>"
            "<link>https://site.example/show</link>"
            "<item><title>Newest</title><link>https://site.example/e3</link></item>"
            "<item><title>Middle</title><link>https://site.example/e2</link></item>"
            "<item><title>Oldest</title><link>https://site.example/e1</link></item>"
            "</channel></rss>"
        ).encode()
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return data[:limit]

        def open_feed(request, timeout):
            calls.append(request.full_url)
            if request.full_url != feed_url:
                raise AssertionError("unexpected secondary fetch: %s" % request.full_url)
            return Response()

        real = renumber.urlopen
        renumber.urlopen = open_feed
        try:
            _, items = renumber.load_feed(feed_url)
        finally:
            renumber.urlopen = real

        self.assertEqual(calls, [feed_url])
        self.assertEqual(
            [item["title"] for item in sorted(items, key=lambda item: item["seq"])],
            ["Oldest", "Middle", "Newest"],
        )


class EmbeddedArcTests(unittest.TestCase):
    """A numbered arc inside an anthology must play in part order.

    A feed can be 100 standalone episodes with a 16-part arc among them. The
    arc is far too small a minority for the whole-feed series test, so it was
    left in whatever order the feed happened to give.
    """

    def order(self, titles, days=None):
        """days: publication day per item; defaults to newest-first."""
        directory = Path(tempfile.mkdtemp(prefix="arc-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        days = days or [28 - i for i in range(len(titles))]
        items = "".join(
            "<item><title>%s</title><guid>g%d</guid>"
            "<pubDate>Mon, %02d Mar 2025 06:00:00 GMT</pubDate></item>"
            % (t, i, d) for i, (t, d) in enumerate(zip(titles, days)))
        path = directory / "feed.xml"
        path.write_text(
            '<?xml version="1.0"?><rss version="2.0"><channel><title>S</title>'
            "%s</channel></rss>" % items)
        _, parsed = renumber.load_feed(str(path))
        return [it["title"] for it in sorted(parsed, key=lambda i: i["seq"])]

    def test_arc_is_ordered_without_moving_the_standalone_episodes(self):
        feed = ["Solo1", "Arc 2/3", "Solo2", "Arc 3/3", "Solo3", "Arc 1/3",
                "Solo4", "Solo5"]
        self.assertEqual(
            self.order(feed),
            ["Solo5", "Solo4", "Arc 1/3", "Solo3", "Arc 2/3", "Solo2",
             "Arc 3/3", "Solo1"],
            "the arc must ascend in the slots it already occupies, and the "
            "standalone episodes must not move")

    def test_dates_are_not_treated_as_an_arc(self):
        """Day/month pairs must not be reordered as if they were parts."""
        feed = ["Bulletin du 03/03", "Solo", "Bulletin du 02/03",
                "Bulletin du 01/03"]
        # pubDates match the dates in the titles, as a real feed's do.
        self.assertEqual(self.order(feed, days=[3, 4, 2, 1]),
                         ["Bulletin du 01/03", "Bulletin du 02/03",
                          "Solo", "Bulletin du 03/03"])

    def test_arc_with_a_repeated_part_number_is_left_alone(self):
        feed = ["A 1/3", "B 1/3", "Solo", "A 2/3"]
        self.assertEqual(self.order(feed), ["A 2/3", "Solo", "B 1/3", "A 1/3"])


class ArcGuardTests(unittest.TestCase):
    """Isolate each guard in the arc pass.

    Written after mutation testing: the earlier fixtures were caught by
    whichever guard was left, so removing either one alone still passed.
    """

    def order(self, entries):
        """entries: [(title, day)] in feed order."""
        directory = Path(tempfile.mkdtemp(prefix="guard-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        items = "".join(
            "<item><title>%s</title><guid>g%d</guid>"
            "<pubDate>Mon, %02d Dec 2025 06:00:00 GMT</pubDate></item>"
            % (title, i, day) for i, (title, day) in enumerate(entries))
        path = directory / "feed.xml"
        path.write_text(
            '<?xml version="1.0"?><rss version="2.0"><channel><title>S</title>'
            "%s</channel></rss>" % items)
        _, parsed = renumber.load_feed(str(path))
        return [it["title"] for it in sorted(parsed, key=lambda i: i["seq"])]

    def test_dated_items_keep_the_reversed_rss_order(self):
        """Only the date guard can catch this: the parts are inside 1..M.

        Days 1..6 of month 12 look exactly like parts 1..6 of 6, so without
        the pubDate check the arc pass would 'sort' them and override the
        reversed-RSS fallback.
        """
        entries = [("Journal - %02d/12" % k, k) for k in range(1, 7)]
        self.assertEqual(self.order(entries),
                         ["Journal - %02d/12" % k for k in range(6, 0, -1)],
                         "day/month pairs must not be reordered as parts")

    def test_out_of_range_numbers_are_left_alone(self):
        """Only the 1..M range guard can catch this: the dates do not match.

        'Ep 7/3' is not part 7 of 3. The publication days deliberately differ
        from the numerators, so the date guard cannot fire here.
        """
        entries = [("Ep 7/3", 20), ("Ep 9/3", 21),
                   ("Ep 5/3", 22), ("Solo", 23)]
        # Falls back to the feed reversed, untouched by the arc pass.
        self.assertEqual(self.order(entries),
                         ["Solo", "Ep 5/3", "Ep 9/3", "Ep 7/3"])


class RobustnessTests(unittest.TestCase):
    """Malformed input must produce a message, not a traceback."""

    def feed_with(self, item_xml):
        directory = Path(tempfile.mkdtemp(prefix="robust-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = directory / "feed.xml"
        path.write_text(
            '<?xml version="1.0"?><rss version="2.0"><channel><title>S</title>'
            "%s</channel></rss>" % item_xml)
        return path

    def test_malformed_pubdate_does_not_abort(self):
        path = self.feed_with(
            "<item><title>A</title><guid>a</guid>"
            "<pubDate>not a date at all</pubDate></item>"
            "<item><title>B</title><guid>b</guid>"
            "<pubDate>Mon, 03 Mar 2025 06:00:00 GMT</pubDate></item>")
        _, items = renumber.load_feed(str(path))
        self.assertEqual(len(items), 2)
        self.assertIsNone([i for i in items if i["title"] == "A"][0]["pub"])

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required to synthesize test audio")
    def test_directory_named_like_audio_is_ignored(self):
        directory = Path(tempfile.mkdtemp(prefix="robust-dir-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        audio = directory / "audio"
        write_audio(audio, ["Real"])
        (audio / "bonus.mp3").mkdir()          # a directory, not a file
        feed = write_feed(directory, ["Real"])
        run_renumber(feed, audio)              # must not raise
        self.assertTrue((audio / "01_Real.mp3").is_file())
        self.assertTrue((audio / "bonus.mp3").is_dir())

    def test_missing_voice_fails_before_downloading(self):
        """A URL run used to download the whole feed, then fail on the voice."""
        import types
        p2l = load(SCRIPTS / "podcast2lunii.py", "p2l_voice_test")
        downloaded = []
        p2l.download_feed = lambda *a, **k: downloaded.append(1) or Path(".")
        p2l.PIPER = Path("/nonexistent/piper")
        p2l.MODEL = Path("/nonexistent/voice.onnx")
        args = types.SimpleNamespace(
            src="https://example.invalid/feed.xml", download_dir=".", dl_extra=[],
            title=None, slug=None, outdir=None, cover_url=None,
            cover_file=None, menu_prompt="x", no_episode_tts=False)
        with self.assertRaises(SystemExit):
            p2l.build(args)
        self.assertEqual(downloaded, [],
                         "must fail on the missing voice before downloading")

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required to synthesize test audio")
    def test_empty_title_yields_silence_not_a_crash(self):
        """clean_title can reduce a title to nothing; piper exits 1 on empty."""
        p2l = load(SCRIPTS / "podcast2lunii.py", "p2l_under_test")
        self.assertEqual(p2l.clean_title("01_"), "")
        with tempfile.TemporaryDirectory() as tmp:
            data = p2l.tts_mp3("", Path(tmp) / "t.mp3")
            self.assertTrue(data)
            install_pack.validate_mp3(data, "empty-title placeholder")


if __name__ == "__main__":
    unittest.main()
