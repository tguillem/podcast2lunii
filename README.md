# podcast2lunii

Turn a podcast — a local folder of audio files, or an RSS feed URL — into a
Lunii story pack, and optionally install it straight onto a mounted device.

This is an unofficial project, not affiliated with Lunii or with any podcast
publisher. It is vibe-coded and largely LLM-generated.

## Safety

- Use only media you are allowed to download and copy.
- Back up the device before installing anything.
- Direct installation supports firmware 2.x only.
- Installation is a dry run unless `--yes` is supplied.
- Physical-device validation is still pending.

## Setup

Requirements: Python 3.10+, `ffmpeg`, and `ffprobe`. Linux and macOS are
supported.

```sh
python3 tools/bootstrap.py --with-voice
. lunii-pack/venv/bin/activate
python3 tools/check_deps.py
```

## Download and install a pack

Download and convert podcasts to Lunii story packs.

```sh
./lunii-pack/podcast2lunii URL
```

Cover art comes from the feed's own RSS artwork. When a feed declares none — and
always for a local folder — supply it yourself with `--cover-file PATH` or
`--cover-url URL`.

Back up and mount the device, then inspect a dry run:

```sh
./lunii-pack/install_pack /path/to/pack.zip --mount /path/to/device --dry-run
```

Write only after checking the plan:

```sh
./lunii-pack/install_pack /path/to/pack.zip --mount /path/to/device --yes
```

`--replace` replaces a pack with the same UUID. The installer has rollback
and `.pi` backup handling, but neither replaces a device backup.

## Development checks

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q lunii-pack/scripts tools tests
python3 -m py_compile podcast-renumber
for f in yt-dlp-podcast lunii-pack/podcast2lunii lunii-pack/install_pack; do sh -n "$f" || exit 1; done
python3 tools/check_deps.py
```

The tests need `ffmpeg` but no network, no voice model, and no device. They
cover episode ordering, the guarantee that no audio is ever deleted, recovery
from an interrupted rename, BMP output, and archive validation.

## License

MPL-2.0. See [LICENSE](LICENSE), [NOTICE.md](NOTICE.md), and
[THIRD_PARTY.md](THIRD_PARTY.md).
