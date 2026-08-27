#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Thomas Guillem
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Read-only dependency and portability diagnostics for the project."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENV = ROOT / "lunii-pack" / "venv"
REQUIREMENTS = ROOT / "requirements.txt"
DEPENDENCIES = ROOT / "tools" / "dependencies.json"
MIN_PYTHON = (3, 10)
STATUSES = {"OK", "MISSING", "UNSUPPORTED", "WARNING"}
FAILURE_STATUSES = {"MISSING", "UNSUPPORTED"}
HASH_CHUNK_SIZE = 1024 * 1024
PROCESS_TIMEOUT_SECONDS = 30
PACKAGE_IMPORTS = {
    "defusedxml": "defusedxml",
    "mutagen": "mutagen",
    "Pillow": "PIL",
    "piper-tts": "piper",
    "requests": "requests",
    "yt-dlp": "yt_dlp",
}


@dataclasses.dataclass(frozen=True)
class Result:
    id: str
    status: str
    summary: str
    required: bool = True
    details: dict[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"invalid dependency status: {self.status}")

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Python, system, voice, and launcher dependencies without changing them."
    )
    parser.add_argument(
        "--quick", action="store_true", help="skip Piper and FFmpeg smoke tests"
    )
    parser.add_argument(
        "--skip-voice",
        action="store_true",
        help="treat the optional Piper voice model and synthesis test as skipped",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit stable machine-readable JSON"
    )
    parser.add_argument(
        "--venv",
        type=Path,
        default=DEFAULT_VENV,
        help=f"virtual environment path (default: {DEFAULT_VENV})",
    )
    return parser.parse_args(argv)


def venv_python(venv_path: Path) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    filename = "python.exe" if os.name == "nt" else "python"
    return venv_path / directory / filename


def executable_in_venv(venv_path: Path, name: str) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return venv_path / directory / f"{name}{suffix}"


def run_process(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT_SECONDS,
        **kwargs,
    )


def platform_result() -> Result:
    system = platform.system()
    machine = platform.machine()
    details = {"system": system, "architecture": machine, "release": platform.release()}
    if system == "Linux":
        return Result("platform", "OK", f"Linux {machine} is supported", details=details)
    if system == "Darwin" and machine.lower() in {"arm64", "aarch64"}:
        return Result("platform", "OK", "macOS Apple Silicon is supported", details=details)
    if system == "Darwin" and machine.lower() in {"x86_64", "amd64"}:
        return Result(
            "platform",
            "WARNING",
            "Intel macOS is untested in the first release",
            details=details,
        )
    return Result(
        "platform",
        "UNSUPPORTED",
        f"{system or 'unknown OS'} {machine or 'unknown architecture'} is not supported",
        details=details,
    )


def checker_python_result() -> Result:
    version = platform.python_version()
    details = {"executable": sys.executable, "version": version}
    if sys.version_info < MIN_PYTHON:
        return Result(
            "python",
            "UNSUPPORTED",
            f"Python {version} is too old; Python 3.10 or newer is required",
            details=details,
        )
    return Result(
        "python",
        "OK",
        f"Python {version} at {sys.executable}",
        details=details,
    )


def read_pyvenv_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip().lower()] = value.strip()
    return values


def venv_result(path: Path) -> tuple[Result, Path | None]:
    python = venv_python(path)
    config_path = path / "pyvenv.cfg"
    details = {"path": str(path), "python": str(python)}
    if not path.is_dir() or not config_path.is_file() or not python.is_file():
        return (
            Result(
                "venv",
                "MISSING",
                f"isolated virtual environment not found at {path}; run python3 "
                "tools/bootstrap.py (Debian/Ubuntu may first require python3-venv)",
                details=details,
            ),
            None,
        )
    try:
        config = read_pyvenv_config(config_path)
    except OSError as exc:
        return Result("venv", "MISSING", f"cannot read {config_path}: {exc}", details=details), None
    includes_system = config.get("include-system-site-packages", "false").lower()
    details["include_system_site_packages"] = includes_system
    if includes_system != "false":
        return (
            Result(
                "venv",
                "UNSUPPORTED",
                "virtual environment exposes system site packages; recreate it with tools/bootstrap.py",
                details=details,
            ),
            python,
        )
    try:
        probe = run_process(
            [
                str(python),
                "-I",
                "-c",
                "import json,sys; print(json.dumps({'version': list(sys.version_info[:3]), "
                "'prefix': sys.prefix, 'base_prefix': sys.base_prefix, 'executable': sys.executable}))",
            ]
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result("venv", "MISSING", f"cannot run {python}: {exc}", details=details), None
    if probe.returncode != 0:
        return (
            Result(
                "venv",
                "MISSING",
                f"cannot run {python}: {(probe.stderr or probe.stdout).strip()}",
                details=details,
            ),
            None,
        )
    try:
        info = json.loads(probe.stdout)
    except json.JSONDecodeError:
        return Result("venv", "MISSING", f"invalid interpreter response from {python}", details=details), None
    details.update(info)
    if tuple(info["version"][:2]) < MIN_PYTHON:
        return (
            Result(
                "venv",
                "UNSUPPORTED",
                f"virtual environment uses Python {'.'.join(map(str, info['version']))}; 3.10+ is required",
                details=details,
            ),
            python,
        )
    if info["prefix"] == info["base_prefix"]:
        return Result("venv", "UNSUPPORTED", f"{python} is not isolated from its base interpreter", details=details), python
    return (
        Result(
            "venv",
            "OK",
            f"isolated Python {'.'.join(map(str, info['version']))} at {python}",
            details=details,
        ),
        python,
    )


def requirements() -> tuple[dict[str, str], str | None]:
    pins: dict[str, str] = {}
    try:
        lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {}, str(exc)
    for line in lines:
        item = line.split("#", 1)[0].strip()
        if not item:
            continue
        name, separator, version = item.partition("==")
        if not separator or not name or not version:
            return {}, f"expected an exact name==version pin, found {item!r}"
        pins[name] = version
    return pins, None


def package_results(python: Path | None) -> tuple[list[Result], dict[str, str]]:
    pins, error = requirements()
    if error:
        return [Result("python_packages", "MISSING", f"cannot read exact pins: {error}")], {}
    if python is None:
        return (
            [
                Result(
                    f"python_package.{name.lower()}",
                    "MISSING",
                    f"cannot check {name}=={version} without the virtual environment",
                )
                for name, version in pins.items()
            ],
            {},
        )

    probe_code = """
import importlib
import importlib.metadata
import json
import sys

mapping = json.loads(sys.argv[1])
result = {}
for distribution, module in mapping.items():
    item = {}
    try:
        item["version"] = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        item["version"] = None
    try:
        importlib.import_module(module)
        item["import"] = "OK"
    except Exception as exc:
        item["import"] = f"{type(exc).__name__}: {exc}"
    result[distribution] = item
print(json.dumps(result, sort_keys=True))
"""
    try:
        probe = run_process(
            [str(python), "-I", "-B", "-c", probe_code, json.dumps(PACKAGE_IMPORTS)]
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [Result("python_packages", "MISSING", f"package probe failed: {exc}")], {}
    if probe.returncode != 0:
        summary = (probe.stderr or probe.stdout).strip()
        return [Result("python_packages", "MISSING", f"package probe failed: {summary}")], {}
    try:
        installed = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        return [Result("python_packages", "MISSING", f"invalid package probe output: {exc}")], {}

    found_versions: dict[str, str] = {}
    results: list[Result] = []
    for name, expected in pins.items():
        item = installed.get(name, {})
        found = item.get("version")
        imported = item.get("import")
        details = {"expected_version": expected, "installed_version": found, "import": imported}
        if found is None:
            results.append(Result(f"python_package.{name.lower()}", "MISSING", f"{name}=={expected} is not installed", details=details))
        elif found != expected:
            results.append(Result(f"python_package.{name.lower()}", "MISSING", f"{name}=={expected} is required; found {found}", details=details))
        elif imported != "OK":
            results.append(Result(f"python_package.{name.lower()}", "MISSING", f"cannot import {PACKAGE_IMPORTS[name]}: {imported}", details=details))
        else:
            found_versions[name] = found
            results.append(Result(f"python_package.{name.lower()}", "OK", f"{name} {found} imports successfully", details=details))
    return results, found_versions


def venv_executable_result(
    path: Path, name: str, arguments: list[str], package_version: str | None
) -> Result:
    executable = executable_in_venv(path, name)
    details = {"path": str(executable), "package_version": package_version}
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return Result(f"executable.{name}", "MISSING", f"{name} executable not found in the virtual environment", details=details)
    try:
        probe = run_process([str(executable), *arguments])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result(f"executable.{name}", "MISSING", f"cannot run {executable}: {exc}", details=details)
    output = (probe.stdout or probe.stderr).strip().splitlines()
    if probe.returncode != 0:
        return Result(f"executable.{name}", "MISSING", f"{name} probe exited {probe.returncode}: {' '.join(output[:1])}", details=details)
    details["probe_output"] = output[0] if output else ""
    version_text = package_version or details["probe_output"] or "unknown version"
    return Result(f"executable.{name}", "OK", f"{name} {version_text} at {executable}", details=details)


def ffmpeg_guidance(name: str) -> str:
    if platform.system() == "Darwin":
        return f"install {name} with `brew install ffmpeg`, then re-run this check"
    if platform.system() == "Linux":
        return (
            f"install {name} from the OS packages (Debian/Ubuntu: `sudo apt install ffmpeg`; "
            "Fedora: `sudo dnf install ffmpeg`), then re-run this check"
        )
    return f"install {name} from https://ffmpeg.org/download.html"


def system_executable_result(name: str) -> tuple[Result, Path | None]:
    found = shutil.which(name)
    if found is None:
        return Result(f"executable.{name}", "MISSING", f"{name} not found; {ffmpeg_guidance(name)}"), None
    executable = Path(found)
    try:
        probe = run_process([str(executable), "-version"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result(f"executable.{name}", "MISSING", f"cannot run {executable}: {exc}"), executable
    line = (probe.stdout or probe.stderr).strip().splitlines()
    if probe.returncode != 0 or not line:
        return Result(f"executable.{name}", "MISSING", f"{name} version probe failed at {executable}"), executable
    return Result(f"executable.{name}", "OK", f"{line[0]} at {executable}", details={"path": str(executable), "version_line": line[0]}), executable


def load_voice_files() -> tuple[list[dict[str, Any]], str | None]:
    try:
        document = json.loads(DEPENDENCIES.read_text(encoding="utf-8"))
        files = document["voice"]["files"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return [], str(exc)
    if not isinstance(files, list) or not files:
        return [], "voice file list is empty"
    return files, None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def voice_result(skip: bool) -> tuple[Result, dict[str, Path]]:
    if skip:
        return Result("voice_assets", "OK", "voice asset check skipped by request", required=False), {}
    files, error = load_voice_files()
    if error:
        return Result("voice_assets", "MISSING", f"cannot load {DEPENDENCIES}: {error}"), {}
    voice_dir = ROOT / "lunii-pack" / "voices"
    paths: dict[str, Path] = {}
    details: dict[str, Any] = {"files": []}
    problems: list[str] = []
    for metadata in files:
        name = metadata.get("name")
        if not isinstance(name, str) or Path(name).name != name:
            problems.append(f"invalid metadata name {name!r}")
            continue
        path = voice_dir / name
        file_details: dict[str, Any] = {
            "name": name,
            "path": str(path),
            "expected_size": metadata.get("size"),
            "expected_sha256": metadata.get("sha256"),
        }
        details["files"].append(file_details)
        if not path.is_file():
            problems.append(f"{name} is missing")
            continue
        try:
            size = path.stat().st_size
            checksum = sha256(path)
        except OSError as exc:
            problems.append(f"cannot read {name}: {exc}")
            continue
        file_details.update({"size": size, "sha256": checksum})
        if size != metadata.get("size") or checksum != metadata.get("sha256"):
            problems.append(f"{name} failed size or SHA-256 verification")
            continue
        paths[name] = path
    if problems:
        return Result("voice_assets", "MISSING", "; ".join(problems) + "; run tools/bootstrap.py --with-voice", details=details), paths
    return Result("voice_assets", "OK", f"{len(paths)} pinned Piper voice files verified", details=details), paths


def source_import_result(python: Path | None) -> Result:
    if python is None:
        return Result("source_import", "MISSING", "cannot import project source without the virtual environment")
    sources = [
        ROOT / "podcast-renumber",
        ROOT / "lunii-pack" / "scripts" / "build_pack.py",
        ROOT / "lunii-pack" / "scripts" / "install_pack.py",
        ROOT / "lunii-pack" / "scripts" / "lunii_image.py",
        ROOT / "lunii-pack" / "scripts" / "podcast2lunii.py",
        ROOT / "lunii-pack" / "scripts" / "prep_audio.py",
    ]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        return Result("source_import", "MISSING", f"project source missing: {', '.join(missing)}")
    code = """
import importlib.machinery
import importlib.util
import pathlib
import sys

sys.dont_write_bytecode = True
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "lunii-pack" / "scripts"))
for index, filename in enumerate(sys.argv[2:]):
    name = f"_lunii_dependency_check_{index}"
    loader = importlib.machinery.SourceFileLoader(name, filename)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
"""
    try:
        probe = run_process([str(python), "-I", "-B", "-c", code, str(ROOT), *map(str, sources)])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result("source_import", "MISSING", f"source import probe failed: {exc}")
    if probe.returncode != 0:
        lines = (probe.stderr or probe.stdout).strip().splitlines()
        return Result("source_import", "MISSING", f"source import failed: {' | '.join(lines[-3:])}")
    return Result("source_import", "OK", f"imported {len(sources)} project source files without bytecode writes")


def launcher_result() -> Result:
    launchers = [
        ROOT / "yt-dlp-podcast",
        ROOT / "lunii-pack" / "podcast2lunii",
        ROOT / "lunii-pack" / "install_pack",
    ]
    problems: list[str] = []
    for path in launchers:
        if not path.is_file() or not os.access(path, os.X_OK):
            problems.append(f"{path.relative_to(ROOT)} is missing or not executable")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"cannot read {path.relative_to(ROOT)}: {exc}")
            continue
        if "readlink -f" in text:
            problems.append(f"{path.relative_to(ROOT)} uses GNU-only readlink -f")
    if problems:
        return Result("launchers", "UNSUPPORTED", "; ".join(problems))
    return Result("launchers", "OK", "three launchers use portable script-directory resolution")


def ffmpeg_smoke_result(ffmpeg: Path | None, quick: bool) -> Result:
    if quick:
        return Result("smoke.ffmpeg", "OK", "FFmpeg smoke test skipped by --quick", required=False)
    if ffmpeg is None:
        return Result("smoke.ffmpeg", "MISSING", "FFmpeg smoke test cannot run without ffmpeg")
    try:
        with tempfile.TemporaryDirectory(prefix="lunii-ffmpeg-check-") as directory:
            root = Path(directory)
            source = root / "silence.wav"
            output = root / "silence.mp3"
            with wave.open(str(source), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(22050)
                wav.writeframes(b"\0\0" * 2205)
            probe = run_process(
                [str(ffmpeg), "-y", "-v", "error", "-i", str(source), "-codec:a", "libmp3lame", str(output)]
            )
            if probe.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
                return Result("smoke.ffmpeg", "MISSING", f"FFmpeg MP3 smoke test failed: {(probe.stderr or probe.stdout).strip()}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result("smoke.ffmpeg", "MISSING", f"FFmpeg MP3 smoke test failed: {exc}")
    return Result("smoke.ffmpeg", "OK", "FFmpeg encoded a temporary synthetic WAV as MP3")


def piper_smoke_result(
    venv_path: Path, voice_paths: dict[str, Path], quick: bool, skip_voice: bool
) -> Result:
    if quick or skip_voice:
        reason = "--quick" if quick else "--skip-voice"
        return Result("smoke.piper", "OK", f"Piper synthesis smoke test skipped by {reason}", required=False)
    model = voice_paths.get("fr_FR-siwis-medium.onnx")
    config = voice_paths.get("fr_FR-siwis-medium.onnx.json")
    executable = executable_in_venv(venv_path, "piper")
    if model is None or config is None or not executable.is_file():
        return Result("smoke.piper", "MISSING", "Piper synthesis smoke test prerequisites are missing")
    try:
        with tempfile.TemporaryDirectory(prefix="lunii-piper-check-") as directory:
            output = Path(directory) / "speech.wav"
            probe = run_process(
                [str(executable), "-m", str(model), "-c", str(config), "-f", str(output)],
                input="Test.\n",
            )
            if probe.returncode != 0 or not output.is_file() or output.stat().st_size <= 44:
                return Result("smoke.piper", "MISSING", f"Piper synthesis smoke test failed: {(probe.stderr or probe.stdout).strip()}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result("smoke.piper", "MISSING", f"Piper synthesis smoke test failed: {exc}")
    return Result("smoke.piper", "OK", "Piper synthesized a temporary WAV with the verified voice")


def collect(args: argparse.Namespace) -> tuple[list[Result], Path]:
    venv_path = args.venv.expanduser().resolve(strict=False)
    results = [platform_result(), checker_python_result()]
    checked_venv, python = venv_result(venv_path)
    results.append(checked_venv)
    packages, versions = package_results(python)
    results.extend(packages)
    results.append(venv_executable_result(venv_path, "piper", ["--help"], versions.get("piper-tts")))
    results.append(venv_executable_result(venv_path, "yt-dlp", ["--version"], versions.get("yt-dlp")))
    ffmpeg, ffmpeg_path = system_executable_result("ffmpeg")
    ffprobe, _ = system_executable_result("ffprobe")
    results.extend([ffmpeg, ffprobe])
    voice, voice_paths = voice_result(args.skip_voice)
    results.append(voice)
    results.append(source_import_result(python))
    results.append(launcher_result())
    results.append(ffmpeg_smoke_result(ffmpeg_path, args.quick))
    results.append(piper_smoke_result(venv_path, voice_paths, args.quick, args.skip_voice))
    return results, venv_path


def is_success(results: list[Result]) -> bool:
    return not any(result.required and result.status in FAILURE_STATUSES for result in results)


def emit_human(results: list[Result]) -> None:
    for result in results:
        print(f"{result.status:<11} {result.id}: {result.summary}")
    if is_success(results):
        print("Dependency check: OK")
    else:
        failures = sum(
            result.required and result.status in FAILURE_STATUSES for result in results
        )
        print(f"Dependency check: FAILED ({failures} required checks)")


def emit_json(results: list[Result], venv_path: Path) -> None:
    document = {
        "schema_version": 1,
        "ok": is_success(results),
        "venv": str(venv_path),
        "results": [result.as_dict() for result in results],
    }
    print(json.dumps(document, sort_keys=True, indent=2))


def main() -> int:
    args = parse_args(sys.argv[1:])
    results, venv_path = collect(args)
    if args.json:
        emit_json(results, venv_path)
    else:
        emit_human(results)
    return 0 if is_success(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
