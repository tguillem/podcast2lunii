#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Thomas Guillem
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Create an isolated project environment and install declared dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import venv
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENV = ROOT / "lunii-pack" / "venv"
REQUIREMENTS = ROOT / "requirements.txt"
DEPENDENCIES = ROOT / "tools" / "dependencies.json"
CHECK_DEPS = ROOT / "tools" / "check_deps.py"
MIN_PYTHON = (3, 10)
PACKAGING_TOOLS = (
    "pip==26.2.1",
    "setuptools==84.0.0",
    "wheel==0.48.0",
)
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60


class BootstrapError(RuntimeError):
    """A setup failure with a user-facing explanation."""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create an isolated venv and install Lunii podcast tools."
    )
    parser.add_argument(
        "--venv",
        type=Path,
        default=DEFAULT_VENV,
        help=f"virtual environment path (default: {DEFAULT_VENV})",
    )
    parser.add_argument(
        "--with-voice",
        action="store_true",
        help="download and verify the approximately 63 MB French Piper voice",
    )
    parser.add_argument(
        "--force-voice",
        action="store_true",
        help="re-download and verify both voice files (implies --with-voice)",
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="ask pip to refresh packages to the exact reviewed manifest versions",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print intended operations without creating files or using the network",
    )
    return parser.parse_args(argv)


def fail(message: str) -> None:
    raise BootstrapError(message)


def normalized_venv_path(path: Path) -> Path:
    result = path.expanduser().resolve(strict=False)
    if result in {Path(result.anchor), ROOT, ROOT / "lunii-pack"}:
        fail(f"refusing unsafe virtual environment path: {result}")
    return result


def venv_python(venv_path: Path) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    filename = "python.exe" if os.name == "nt" else "python"
    return venv_path / directory / filename


def read_pyvenv_config(path: Path) -> dict[str, str]:
    config: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"cannot read {path}: {exc}")
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            config[key.strip().lower()] = value.strip()
    return config


def validate_existing_venv(path: Path) -> Path:
    config_path = path / "pyvenv.cfg"
    python = venv_python(path)
    if not path.is_dir() or not config_path.is_file() or not python.is_file():
        fail(
            f"{path} exists but is not a usable virtual environment; "
            "move it aside or choose another path with --venv"
        )
    config = read_pyvenv_config(config_path)
    if config.get("include-system-site-packages", "false").lower() != "false":
        fail(
            f"{path} exposes system site packages and cannot prove a clean setup; "
            "move it aside or choose another path with --venv"
        )
    probe = subprocess.run(
        [str(python), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0:
        fail(f"cannot run virtual environment interpreter {python}: {probe.stderr.strip()}")
    try:
        version = tuple(int(part) for part in probe.stdout.strip().split(".")[:2])
    except ValueError:
        fail(f"cannot parse Python version reported by {python}: {probe.stdout.strip()!r}")
    if version < MIN_PYTHON:
        fail(
            f"{path} uses Python {probe.stdout.strip()}; Python 3.10 or newer is "
            "required; move it aside or choose another path with --venv"
        )
    return python


def load_voice_metadata() -> dict[str, Any]:
    try:
        document = json.loads(DEPENDENCIES.read_text(encoding="utf-8"))
        voice = document["voice"]
        files = voice["files"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        fail(f"cannot load voice metadata from {DEPENDENCIES}: {exc}")
    if not isinstance(files, list) or not files:
        fail(f"voice metadata in {DEPENDENCIES} has no files")
    for item in files:
        try:
            name = item["name"]
            url = item["url"]
            size = item["size"]
            checksum = item["sha256"]
        except (KeyError, TypeError) as exc:
            fail(f"invalid voice file metadata in {DEPENDENCIES}: {exc}")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(url, str)
            or not url.startswith("https://")
            or not isinstance(size, int)
            or size <= 0
            or not isinstance(checksum, str)
            or len(checksum) != 64
        ):
            fail(f"invalid voice file metadata for {name!r} in {DEPENDENCIES}")
    return voice


def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(DOWNLOAD_CHUNK_SIZE):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def voice_file_is_valid(path: Path, metadata: dict[str, Any]) -> bool:
    if not path.is_file():
        return False
    try:
        size, checksum = file_digest(path)
    except OSError:
        return False
    return size == metadata["size"] and checksum == metadata["sha256"]


def download_to_temporary(
    metadata: dict[str, Any], destination: Path
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".download",
            dir=destination.parent,
            delete=False,
        ) as output:
            temporary = Path(output.name)
            request = urllib.request.Request(
                metadata["url"],
                headers={"User-Agent": "lunii-bootstrap/1"},
            )
            digest = hashlib.sha256()
            size = 0
            with urllib.request.urlopen(
                request, timeout=DOWNLOAD_TIMEOUT_SECONDS
            ) as response:
                while chunk := response.read(DOWNLOAD_CHUNK_SIZE):
                    size += len(chunk)
                    if size > metadata["size"]:
                        fail(
                            f"downloaded {metadata['name']} exceeds its declared "
                            f"size of {metadata['size']} bytes"
                        )
                    digest.update(chunk)
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size != metadata["size"]:
            fail(
                f"downloaded {metadata['name']} has size {size}; "
                f"expected {metadata['size']}"
            )
        actual = digest.hexdigest()
        if actual != metadata["sha256"]:
            fail(
                f"downloaded {metadata['name']} has SHA-256 {actual}; "
                f"expected {metadata['sha256']}"
            )
        return temporary
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def install_voice(voice: dict[str, Any], force: bool) -> None:
    voice_dir = ROOT / "lunii-pack" / "voices"
    pending: list[tuple[dict[str, Any], Path]] = []
    for metadata in voice["files"]:
        target = voice_dir / metadata["name"]
        if voice_file_is_valid(target, metadata) and not force:
            print(f"Voice asset already verified: {target}")
            continue
        if target.exists() and not force:
            fail(
                f"existing voice asset failed size or checksum validation: {target}; "
                "use --force-voice to replace it only after reviewing the pinned source"
            )
        pending.append((metadata, target))

    if not pending:
        return
    print(
        "Network access: downloading the selected Piper voice from the pinned "
        f"rhasspy/piper-voices revision {voice['revision']}.",
        flush=True,
    )
    staged: list[tuple[Path, Path]] = []
    try:
        for metadata, target in pending:
            print(f"Downloading {metadata['name']} ({metadata['size']} bytes) ...")
            staged.append((download_to_temporary(metadata, target), target))
        for temporary, target in staged:
            os.replace(temporary, target)
            print(f"Verified voice asset: {target}")
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def command_text(command: list[str]) -> str:
    return shlex.join(command)


def pip_commands(python: Path, upgrade: bool) -> list[list[str]]:
    prefix = [str(python), "-m", "pip", "install"]
    if upgrade:
        prefix.append("--upgrade")
    return [
        [*prefix, *PACKAGING_TOOLS],
        [*prefix, "--requirement", str(REQUIREMENTS)],
    ]


def run(argv: list[str]) -> int:
    args = parse_args(argv)
    if sys.version_info < MIN_PYTHON:
        fail(
            "Python 3.10 or newer is required; running "
            f"{sys.version.split()[0]} at {sys.executable}"
        )

    path = normalized_venv_path(args.venv)
    wants_voice = args.with_voice or args.force_voice
    voice = load_voice_metadata()
    print(f"Bootstrap interpreter: {sys.version.split()[0]} at {sys.executable}")
    print(f"Virtual environment: {path}")

    if args.dry_run:
        if path.exists():
            python = validate_existing_venv(path)
            print("Would reuse the existing isolated virtual environment.")
        else:
            python = venv_python(path)
            print("Would create the virtual environment without system site packages.")
        for command in pip_commands(python, args.upgrade):
            print(f"Would run: {command_text(command)}")
        if wants_voice:
            print(
                "Would download and checksum-verify the pinned voice files from "
                f"revision {voice['revision']}."
            )
        else:
            print("Would not download voice files (use --with-voice to opt in).")
        check = [str(python), str(CHECK_DEPS), "--venv", str(path)]
        if not wants_voice:
            check.append("--skip-voice")
        print(f"Would run: {command_text(check)}")
        print("Dry run: no files changed and no network requests made.")
        return 0

    if path.exists():
        python = validate_existing_venv(path)
        print("Reusing the existing isolated virtual environment.")
    else:
        print("Creating a virtual environment without system site packages ...")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            venv.EnvBuilder(with_pip=True, system_site_packages=False).create(path)
        except (OSError, subprocess.SubprocessError) as exc:
            fail(
                f"could not create {path}: {exc}. On Debian/Ubuntu, install the "
                "python3-venv package. The partial path, if any, was left in place "
                "for review and was not deleted."
            )
        python = validate_existing_venv(path)

    print(
        "Network access: pip will contact the configured package index to install "
        "the reviewed versions in requirements.txt.",
        flush=True,
    )
    for command in pip_commands(python, args.upgrade):
        print(f"Running: {command_text(command)}", flush=True)
        subprocess.run(command, check=True)

    if wants_voice:
        install_voice(voice, args.force_voice)
    else:
        print("Voice download skipped; re-run with --with-voice to enable title speech.")

    check = [str(python), str(CHECK_DEPS), "--venv", str(path)]
    if not wants_voice:
        check.append("--skip-voice")
    print(f"Running dependency checks: {command_text(check)}", flush=True)
    result = subprocess.run(check, check=False)
    if result.returncode != 0:
        return result.returncode

    activate = path / ("Scripts" if os.name == "nt" else "bin") / "activate"
    print("Setup complete. Next commands:")
    print(f"  . {shlex.quote(str(activate))}")
    print("  ./lunii-pack/podcast2lunii --help")
    print("  ./lunii-pack/install_pack --help")
    return 0


def main() -> int:
    try:
        return run(sys.argv[1:])
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError, urllib.error.URLError) as exc:
        print(f"error: bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
