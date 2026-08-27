#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Thomas Guillem
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
# Portions adapted from STUdio 0.4.0; see NOTICE.md.
"""Install a generated STUdio/Lunii archive directly onto a mounted device.

This converts the archive ``story.json`` graph into the filesystem pack format
used under ``.content/<PACK8>`` and updates the device ``.pi`` index.  It targets
STUdio's firmware-2.x cleartext upload path.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
WORK_ROOT = ROOT / "build" / ".install-work"
COMMON_KEY = bytes(
    [0x91, 0xBD, 0x7A, 0x0A, 0xA7, 0x54, 0x40, 0xA9,
     0xBB, 0xD4, 0x9D, 0x6C, 0xE0, 0xDC, 0xC0, 0xE3]
)
CLEAR_FILES = {"ni", "nm", ".cleartext"}
NO_COPY_FILES = {".cleartext"}

# A 1 GiB asset budget accommodates large multi-episode podcast packs while
# placing a hard bound on this in-memory converter. Individual assets may be
# up to 256 MiB, and graph metadata may be up to 16 MiB.
MAX_ARCHIVE_MEMBERS = 8192
MAX_STORY_BYTES = 16 * 1024 * 1024
MAX_ASSET_BYTES = 256 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 1024 * 1024 * 1024
MAX_ASSET_NAME_BYTES = 255


class PackError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class ArchivePack:
    story: dict[str, Any]
    assets: dict[str, bytes]


@dataclasses.dataclass(frozen=True)
class DeviceInfo:
    metadata_version: int
    firmware_major: int
    firmware_minor: int
    uuid_block: bytes


@dataclasses.dataclass(frozen=True)
class FsBuildSummary:
    work_dir: Path
    pack_uuid: uuid.UUID
    pack8: str
    title: str
    version: int
    stage_count: int
    action_count: int
    image_count: int
    sound_count: int


def pack_folder_name(pack_uuid: uuid.UUID) -> str:
    return pack_uuid.hex[-8:].upper()


def asset_path(index: int) -> str:
    return f"000\\{index:08d}"


def asset_fs_path(root_name: str, index: int) -> Path:
    return Path(root_name) / "000" / f"{index:08d}"


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def fail(message: str) -> None:
    raise PackError(message)


def read_archive(path: Path) -> ArchivePack:
    if not path.is_file():
        fail(f"pack archive not found: {path}")

    try:
        with zipfile.ZipFile(path) as zf:
            members = zf.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                fail(
                    f"archive has {len(members)} members; maximum is "
                    f"{MAX_ARCHIVE_MEMBERS}"
                )

            seen_names: set[str] = set()
            story_info: zipfile.ZipInfo | None = None
            asset_infos: dict[str, zipfile.ZipInfo] = {}
            total_asset_bytes = 0
            for info in members:
                name = info.filename
                if name in seen_names:
                    fail(f"duplicate archive member: {name}")
                seen_names.add(name)
                if not name or "\x00" in name or "\\" in name or name.startswith("/"):
                    fail(f"unsafe archive member name: {name!r}")
                parts = name.rstrip("/").split("/")
                if any(part in ("", ".", "..") for part in parts):
                    fail(f"unsafe archive member name: {name!r}")
                if info.flag_bits & 0x1:
                    fail(f"encrypted archive members are unsupported: {name}")

                if name == "story.json":
                    if info.is_dir():
                        fail("story.json must be a file")
                    if info.file_size > MAX_STORY_BYTES:
                        fail(
                            f"story.json is {info.file_size} bytes; maximum is "
                            f"{MAX_STORY_BYTES}"
                        )
                    story_info = info
                    continue

                if not name.startswith("assets/"):
                    continue
                if name == "assets/" and info.is_dir():
                    continue
                base = name[len("assets/"):]
                if (
                    info.is_dir()
                    or not base
                    or "/" in base
                    or "\\" in base
                    or base in (".", "..")
                    or len(base.encode("utf-8")) > MAX_ASSET_NAME_BYTES
                ):
                    fail(f"asset names must be flat, safe filenames: {name!r}")
                if base in asset_infos:
                    fail(f"duplicate asset name in archive: {base}")
                if info.file_size > MAX_ASSET_BYTES:
                    fail(
                        f"asset {base} is {info.file_size} bytes; maximum is "
                        f"{MAX_ASSET_BYTES}"
                    )
                total_asset_bytes += info.file_size
                if total_asset_bytes > MAX_TOTAL_ASSET_BYTES:
                    fail(
                        f"archive assets total more than {MAX_TOTAL_ASSET_BYTES} bytes"
                    )
                asset_infos[base] = info

            if story_info is None:
                fail("archive is missing story.json")
            try:
                story = json.loads(zf.read(story_info).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                fail(f"story.json is not valid UTF-8 JSON: {exc}")
            assets = {base: zf.read(info) for base, info in asset_infos.items()}
    except PackError:
        raise
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        OSError,
        RuntimeError,
        NotImplementedError,
    ) as exc:
        fail(f"not a valid zip archive: {path}: {exc}")

    validate_archive(story, assets)
    return ArchivePack(story=story, assets=assets)


def node_key(node: dict[str, Any]) -> str:
    value = node.get("id") or node.get("uuid")
    if not isinstance(value, str) or not value:
        fail("node is missing string id/uuid")
    return value


def node_keys(node: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("id", "uuid"):
        value = node.get(field)
        if isinstance(value, str) and value and value not in keys:
            keys.append(value)
    if not keys:
        fail("node is missing string id/uuid")
    return keys


def validate_archive(story: dict[str, Any], assets: dict[str, bytes]) -> None:
    if not isinstance(story, dict):
        fail("story.json root must be an object")
    if story.get("format") not in (None, "v1"):
        fail(f"unsupported story format: {story.get('format')!r}")
    if type(story.get("version", 0)) is not int:
        fail("story version must be an integer")
    try:
        uuid.UUID(str(story["uuid"]))
    except Exception as exc:
        fail(f"invalid pack uuid: {exc}")

    stages = story.get("stageNodes")
    actions = story.get("actionNodes")
    if not isinstance(stages, list) or not stages:
        fail("story must contain at least one stage node")
    if not isinstance(actions, list):
        fail("story actionNodes must be a list")

    stage_by_key: dict[str, dict[str, Any]] = {}
    for stage in stages:
        if not isinstance(stage, dict):
            fail("stage node must be an object")
        for key in node_keys(stage):
            if key in stage_by_key:
                fail(f"duplicate stage node id/uuid: {key}")
            stage_by_key[key] = stage

    action_by_key: dict[str, dict[str, Any]] = {}
    for action in actions:
        if not isinstance(action, dict):
            fail("action node must be an object")
        options = action.get("options")
        if not isinstance(options, list):
            fail(f"action {node_key(action)} options must be a list")
        for option in options:
            if not isinstance(option, str) or not option:
                fail(
                    f"action {node_key(action)} option must be a non-empty string"
                )
            if option not in stage_by_key:
                fail(f"action {node_key(action)} option points to unknown stage {option!r}")
        for key in node_keys(action):
            if key in action_by_key:
                fail(f"duplicate action node id/uuid: {key}")
            action_by_key[key] = action

    squares = [stage for stage in stages if bool(stage.get("squareOne"))]
    if len(squares) != 1:
        fail(f"expected exactly one squareOne stage, found {len(squares)}")

    for stage in stages:
        stage_name = stage.get("name") or node_key(stage)
        controls = stage.get("controlSettings")
        if not isinstance(controls, dict):
            fail(f"{stage_name}: missing controlSettings object")
        for key in ("wheel", "ok", "home", "pause", "autoplay"):
            if key not in controls:
                fail(f"{stage_name}: controlSettings missing {key}")

        image = stage.get("image")
        audio = stage.get("audio")
        if image is not None:
            if not isinstance(image, str) or not image:
                fail(f"{stage_name}: image asset reference must be a non-empty string or null")
            if image not in assets:
                fail(f"{stage_name}: referenced image asset is missing: {image}")
            validate_bmp(assets[image], f"{stage_name}: {image}")
        if audio is not None:
            if not isinstance(audio, str) or not audio:
                fail(f"{stage_name}: audio asset reference must be a non-empty string or null")
            if audio not in assets:
                fail(f"{stage_name}: referenced audio asset is missing: {audio}")
            validate_mp3(assets[audio], f"{stage_name}: {audio}")

        for transition_name in ("okTransition", "homeTransition"):
            transition = stage.get(transition_name)
            if transition is None:
                continue
            if not isinstance(transition, dict):
                fail(f"{stage_name}: {transition_name} must be an object or null")
            action_key = transition.get("actionNode")
            if not isinstance(action_key, str) or not action_key:
                fail(
                    f"{stage_name}: {transition_name}.actionNode must be a non-empty string"
                )
            action = action_by_key.get(action_key)
            if action is None:
                fail(f"{stage_name}: {transition_name} points to unknown action {action_key!r}")
            option_index = transition.get("optionIndex")
            if type(option_index) is not int:
                fail(f"{stage_name}: {transition_name}.optionIndex must be an integer")
            if not 0 <= option_index < len(action["options"]):
                fail(
                    f"{stage_name}: {transition_name}.optionIndex {option_index} "
                    f"out of range for action {action_key}"
                )


def validate_bmp(data: bytes, label: str) -> None:
    if len(data) < 54 or data[:2] != b"BM":
        fail(f"{label}: image is not a BMP file")
    try:
        width = struct.unpack_from("<i", data, 18)[0]
        height = struct.unpack_from("<i", data, 22)[0]
        bit_count = struct.unpack_from("<H", data, 28)[0]
        compression = struct.unpack_from("<I", data, 30)[0]
    except struct.error as exc:
        fail(f"{label}: malformed BMP header: {exc}")
    if (width, height) != (320, 240):
        fail(f"{label}: BMP must be 320x240, got {width}x{height}")
    if bit_count != 4 or compression != 2:
        fail(f"{label}: BMP must be 4-bit BI_RLE4, got bit_count={bit_count}, compression={compression}")


def validate_mp3(data: bytes, label: str) -> None:
    if len(data) < 4:
        fail(f"{label}: MP3 asset is too short")
    if data.startswith(b"ID3"):
        fail(f"{label}: MP3 has an ID3v2 tag")
    if len(data) >= 128 and data[-128:-125] == b"TAG":
        fail(f"{label}: MP3 has an ID3v1 tag")


def build_fs_pack(pack: ArchivePack, work_root: Path = WORK_ROOT) -> FsBuildSummary:
    story = pack.story
    pack_uuid = uuid.UUID(str(story["uuid"]))
    pack8 = pack_folder_name(pack_uuid)
    out_dir = work_root / pack8
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    (out_dir / ".cleartext").touch()
    if bool(story.get("nightModeAvailable")):
        (out_dir / "nm").touch()

    stages: list[dict[str, Any]] = story["stageNodes"]
    actions: list[dict[str, Any]] = story["actionNodes"]
    stage_index: dict[str, int] = {}
    for i, stage in enumerate(stages):
        for key in node_keys(stage):
            stage_index[key] = i
    action_by_key: dict[str, dict[str, Any]] = {}
    action_canonical_key: dict[str, str] = {}
    for action in actions:
        canonical = node_key(action)
        for key in node_keys(action):
            action_by_key[canonical] = action
            action_canonical_key[key] = canonical

    image_hashes: list[str] = []
    sound_hashes: list[str] = []
    image_bytes_by_hash: dict[str, bytes] = {}
    sound_bytes_by_hash: dict[str, bytes] = {}
    action_order: list[str] = []
    action_starts: dict[str, int] = {}
    li_cursor = 0
    records: list[bytes] = []

    def intern_asset(name: str | None, hashes: list[str], data_by_hash: dict[str, bytes]) -> int:
        if name is None:
            return -1
        data = pack.assets[name]
        digest = sha1(data)
        if digest not in data_by_hash:
            data_by_hash[digest] = data
            hashes.append(digest)
        return hashes.index(digest)

    def transition_values(transition: dict[str, Any] | None) -> tuple[int, int, int]:
        if transition is None:
            return -1, -1, -1
        action_key = action_canonical_key[str(transition["actionNode"])]
        action = action_by_key[action_key]
        return action_starts[action_key], len(action["options"]), int(transition["optionIndex"])

    for stage in stages:
        image_index = intern_asset(stage.get("image"), image_hashes, image_bytes_by_hash)
        audio_name = stage.get("audio")
        if audio_name is None:
            fail(
                f"{stage.get('name') or node_key(stage)} has no audio; "
                "STUdio substitutes a built-in blank MP3, which this installer does not yet embed"
            )
        sound_index = intern_asset(audio_name, sound_hashes, sound_bytes_by_hash)

        for transition_name in ("okTransition", "homeTransition"):
            transition = stage.get(transition_name)
            if transition is None:
                continue
            action_key = action_canonical_key[str(transition["actionNode"])]
            if action_key not in action_starts:
                action = action_by_key[action_key]
                action_order.append(action_key)
                action_starts[action_key] = li_cursor
                li_cursor += len(action["options"])

        ok_start, ok_count, ok_selected = transition_values(stage.get("okTransition"))
        home_start, home_count, home_selected = transition_values(stage.get("homeTransition"))
        controls = stage["controlSettings"]
        record = struct.pack(
            "<iiiiiiiihhhhhh",
            image_index,
            sound_index,
            ok_start,
            ok_count,
            ok_selected,
            home_start,
            home_count,
            home_selected,
            1 if bool(controls["wheel"]) else 0,
            1 if bool(controls["ok"]) else 0,
            1 if bool(controls["home"]) else 0,
            1 if bool(controls["pause"]) else 0,
            1 if bool(controls["autoplay"]) else 0,
            0,
        )
        records.append(record)

    version = int(story.get("version", 0))
    if not -32768 <= version <= 32767:
        fail(f"story version does not fit in a signed short: {version}")
    header_prefix = struct.pack(
        "<hhiiiiiB",
        1,
        version,
        512,
        44,
        len(stages),
        len(image_hashes),
        len(sound_hashes),
        1,
    )
    (out_dir / "ni").write_bytes(header_prefix + b"\x00" * (512 - len(header_prefix)) + b"".join(records))

    li = bytearray()
    for action_key in action_order:
        for option in action_by_key[action_key]["options"]:
            li += struct.pack("<i", stage_index[option])
    (out_dir / "li").write_bytes(bytes(li))

    write_assets(out_dir, "ri", "rf", image_hashes, image_bytes_by_hash)
    write_assets(out_dir, "si", "sf", sound_hashes, sound_bytes_by_hash)

    return FsBuildSummary(
        work_dir=out_dir,
        pack_uuid=pack_uuid,
        pack8=pack8,
        title=str(story.get("title") or ""),
        version=version,
        stage_count=len(stages),
        action_count=len(actions),
        image_count=len(image_hashes),
        sound_count=len(sound_hashes),
    )


def write_assets(out_dir: Path, index_name: str, folder_name: str, hashes: list[str], data_by_hash: dict[str, bytes]) -> None:
    index = bytearray()
    for i, digest in enumerate(hashes):
        rel = asset_path(i)
        index += rel.encode("utf-8")
        target = out_dir / asset_fs_path(folder_name, i)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data_by_hash[digest])
    (out_dir / index_name).write_bytes(bytes(index))


def parse_device_metadata(path: Path) -> DeviceInfo:
    data = path.read_bytes()
    if len(data) < 2:
        fail(f"{path} is too short to contain metadata version")
    metadata_version = struct.unpack_from("<h", data, 0)[0]

    if 1 <= metadata_version <= 3:
        if len(data) < 512:
            fail(f"{path} is too short for metadata format {metadata_version}")
        firmware_major = struct.unpack_from("<h", data, 6)[0]
        firmware_minor = struct.unpack_from("<h", data, 8)[0]
        uuid_block = data[256:512]
    elif metadata_version == 6:
        if len(data) < 96:
            fail(f"{path} is too short for metadata format 6")
        try:
            firmware_major = int(data[2:3].decode("utf-8"))
            firmware_minor = int(data[4:5].decode("utf-8"))
        except ValueError as exc:
            fail(f"{path} contains invalid ASCII firmware fields: {exc}")
        serial_and_keys = data[26:50]
        key1 = serial_and_keys[:16]
        key2 = serial_and_keys[16:24] + serial_and_keys[:8]
        key3 = data[64:96]
        uuid_block = key1 + key2 + key3
    else:
        fail(f"unsupported device metadata version in .md: {metadata_version}")

    return DeviceInfo(
        metadata_version=metadata_version,
        firmware_major=firmware_major,
        firmware_minor=firmware_minor,
        uuid_block=uuid_block,
    )


def verify_mount(mount: Path) -> DeviceInfo:
    if not mount.is_dir():
        fail(f"mount path is not a directory: {mount}")
    md = mount / ".md"
    pi = mount / ".pi"
    content = mount / ".content"
    for required in (md, pi):
        if not required.is_file():
            fail(f"device mount is missing {required.name}: {required}")
    if not content.is_dir():
        fail(f"device mount is missing .content directory: {content}")
    if not os.access(mount, os.W_OK) or not os.access(content, os.W_OK):
        fail(f"device mount is not writable: {mount}")

    info = parse_device_metadata(md)
    if info.firmware_major == 3:
        fail(
            f"device firmware {info.firmware_major}.{info.firmware_minor} uses STUdio's V3 AES upload path; "
            "this installer currently supports firmware 2.x only"
        )
    if info.firmware_major != 2:
        fail(f"unsupported device firmware {info.firmware_major}.{info.firmware_minor}; expected firmware 2.x")
    return info


def read_pack_index(pi: Path) -> list[uuid.UUID]:
    data = pi.read_bytes()
    if len(data) % 16 != 0:
        fail(f"{pi} length is not a multiple of 16 bytes")
    return [uuid.UUID(bytes=data[i:i + 16]) for i in range(0, len(data), 16)]


def copy_exclusive(source: Path, destination: Path) -> None:
    """Copy ``source`` without ever replacing an existing destination."""
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output_file:
            descriptor = -1
            shutil.copyfileobj(input_file, output_file)
            output_file.flush()
            os.fsync(output_file.fileno())
        shutil.copystat(source, destination)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        destination.unlink(missing_ok=True)
        raise


def unique_pi_backup(pi: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for counter in range(1000):
        backup = pi.with_name(f".pi.bak.{stamp}.{os.getpid()}.{counter}")
        try:
            copy_exclusive(pi, backup)
            return backup
        except FileExistsError:
            continue
    fail(f"cannot allocate a unique .pi backup name next to {pi}")


def replace_from_copy(source: Path, destination: Path) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.restore.",
            dir=destination.parent,
            delete=False,
        ) as output_file, source.open("rb") as input_file:
            temporary = Path(output_file.name)
            shutil.copyfileobj(input_file, output_file)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_pack_index_atomic(pi: Path, entries: list[uuid.UUID]) -> Path:
    backup = unique_pi_backup(pi)

    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".pi.tmp.",
            dir=pi.parent,
            delete=False,
        ) as fh:
            tmp = Path(fh.name)
            for entry in entries:
                fh.write(entry.bytes)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, pi)
    except BaseException as original:
        try:
            replace_from_copy(backup, pi)
        except BaseException as recovery:
            raise PackError(
                f"failed to update {pi} and could not restore its backup "
                f"{backup}: {recovery}"
            ) from original
        raise
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return backup


def to_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    if value >= 0x80000000:
        value -= 0x100000000
    return value


def uint32(value: int) -> int:
    return value & 0xFFFFFFFF


def bytes_to_ints(data: bytes, byteorder: str) -> list[int]:
    return [
        int.from_bytes(data[i:i + 4], byteorder=byteorder, signed=True)
        for i in range(0, len(data) - len(data) % 4, 4)
    ]


def ints_to_bytes(values: list[int], byteorder: str) -> bytes:
    return b"".join(to_int32(value).to_bytes(4, byteorder=byteorder, signed=True) for value in values)


def mx(key: list[int], e: int, p: int, y: int, z: int, total: int) -> int:
    # Match Java int overflow and unsigned right shifts in XXTEACipher.mx().
    a = (uint32(z) >> 5) ^ uint32(y << 2)
    b = (uint32(y) >> 3) ^ uint32(z << 4)
    c = uint32(total) ^ uint32(y)
    d = uint32(key[(p & 3) ^ e]) ^ uint32(z)
    return to_int32(uint32(uint32(a) + uint32(b)) ^ uint32(uint32(c) + uint32(d)))


def btea(values: list[int], n: int, key: list[int]) -> list[int]:
    values = [to_int32(v) for v in values]
    if n > 1:
        rounds = 1 + 52 // n
        total = 0
        z = values[n - 1]
        while rounds:
            total = to_int32(total - 1640531527)
            e = (uint32(total) >> 2) & 3
            for p in range(n - 1):
                y = values[p + 1]
                values[p] = to_int32(values[p] + mx(key, e, p, y, z, total))
                z = values[p]
            y = values[0]
            values[n - 1] = to_int32(values[n - 1] + mx(key, e, n - 1, y, z, total))
            z = values[n - 1]
            rounds -= 1
    elif n < -1:
        n = -n
        rounds = 1 + 52 // n
        total = to_int32(rounds * -1640531527)
        y = values[0]
        while rounds:
            e = (uint32(total) >> 2) & 3
            for p in range(n - 1, 0, -1):
                z = values[p - 1]
                values[p] = to_int32(values[p] - mx(key, e, p, y, z, total))
                y = values[p]
            z = values[n - 1]
            values[0] = to_int32(values[0] - mx(key, e, 0, y, z, total))
            y = values[0]
            total = to_int32(total + 1640531527)
            rounds -= 1
    return values


def cipher_first_block_common_key(data: bytes, decrypt: bool = False) -> bytes:
    block = data[:min(512, len(data))]
    values = bytes_to_ints(block, "little")
    key = bytes_to_ints(COMMON_KEY, "big")
    n = min(128, len(data) // 4)
    ciphered = ints_to_bytes(btea(values, -n if decrypt else n, key), "little")
    return ciphered + (data[512:] if len(data) > 512 else b"")


def cipher_first_block_specific_key_v2(data: bytes, key_bytes: bytes) -> bytes:
    block = data[:min(64, len(data))]
    values = bytes_to_ints(block, "little")
    key = bytes_to_ints(key_bytes, "big")
    n = min(16, len(data) // 4)
    ciphered = ints_to_bytes(btea(values, n, key), "little")
    return ciphered + (data[64:] if len(data) > 64 else b"")


def specific_key_v2_from_uuid(uuid_block: bytes) -> bytes:
    plain = cipher_first_block_common_key(uuid_block, decrypt=True)
    if len(plain) < 16:
        fail("device UUID block is too short after deciphering")
    order = [11, 10, 9, 8, 15, 14, 13, 12, 3, 2, 1, 0, 7, 6, 5, 4]
    return bytes(plain[i] for i in order)


def add_boot_file_v2(pack_dir: Path, uuid_block: bytes) -> None:
    ri = (pack_dir / "ri").read_bytes()[:64]
    key = specific_key_v2_from_uuid(uuid_block)
    (pack_dir / "bt").write_bytes(cipher_first_block_specific_key_v2(ri, key))


def should_copy(path: Path) -> bool:
    return path.name not in NO_COPY_FILES


def should_cipher(path: Path) -> bool:
    return path.name not in CLEAR_FILES


def count_copy_payload(pack_dir: Path, *, include_boot: bool = False) -> tuple[int, int]:
    count = 0
    total = 0
    for path in pack_dir.rglob("*"):
        if path.is_file() and should_copy(path):
            count += 1
            total += path.stat().st_size
    if include_boot:
        count += 1
        total += 64
    return count, total


def copy_pack_payload(src: Path, dst: Path, *, cipher_v2: bool = False) -> None:
    for root, dirs, files in os.walk(src):
        root_path = Path(root)
        rel_root = root_path.relative_to(src)
        target_root = dst / rel_root
        target_root.mkdir(parents=True, exist_ok=True)
        for dirname in dirs:
            (target_root / dirname).mkdir(exist_ok=True)
        for filename in files:
            source = root_path / filename
            if not should_copy(source):
                continue
            target = target_root / filename
            if cipher_v2 and should_cipher(source):
                target.write_bytes(cipher_first_block_common_key(source.read_bytes()))
            else:
                shutil.copy2(source, target)


def plan_index(entries: list[uuid.UUID], pack_uuid: uuid.UUID, replace: bool) -> tuple[list[uuid.UUID], str]:
    duplicate = pack_uuid in entries
    if duplicate and not replace:
        fail(f"pack UUID already exists in .pi; pass --replace to replace it: {pack_uuid}")
    new_entries = [entry for entry in entries if entry != pack_uuid] if replace else list(entries)
    new_entries.append(pack_uuid)
    action = "replace existing .pi entry and append pack UUID" if duplicate else "append pack UUID"
    return new_entries, action


def install(summary: FsBuildSummary, mount: Path, device: DeviceInfo, entries: list[uuid.UUID], replace: bool) -> Path:
    content = mount / ".content"
    target = content / summary.pack8
    tmp = Path(tempfile.mkdtemp(prefix=f".{summary.pack8}.tmp.", dir=content))
    previous: Path | None = None
    installed_new_target = False
    committed = False
    backup: Path | None = None
    try:
        copy_pack_payload(summary.work_dir, tmp, cipher_v2=True)
        add_boot_file_v2(tmp, device.uuid_block)
        if target.exists():
            if not replace:
                fail(f"target pack folder already exists; pass --replace to replace it: {target}")
            previous = target.with_name(
                f".{summary.pack8}.previous.{uuid.uuid4().hex}"
            )
            if previous.exists():
                fail(f"refusing to overwrite recovery folder: {previous}")
            target.rename(previous)

        try:
            tmp.rename(target)
            installed_new_target = True
            new_entries, _ = plan_index(entries, summary.pack_uuid, replace)
            backup = write_pack_index_atomic(mount / ".pi", new_entries)
            committed = True
        except BaseException as original:
            recovery_errors: list[str] = []
            if installed_new_target and target.exists():
                try:
                    shutil.rmtree(target)
                except OSError as exc:
                    recovery_errors.append(f"could not remove new target {target}: {exc}")
            if previous is not None and previous.exists():
                try:
                    previous.rename(target)
                except OSError as exc:
                    recovery_errors.append(f"could not restore previous target {previous}: {exc}")
            if recovery_errors:
                raise PackError(
                    "install failed and automatic content recovery was incomplete: "
                    + "; ".join(recovery_errors)
                ) from original
            raise
    except OSError as exc:
        fail(f"device install failed: {exc}")
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    if not committed or backup is None:
        fail("device install did not reach its commit point")
    if previous is not None and previous.exists():
        try:
            shutil.rmtree(previous)
        except OSError as exc:
            print(
                f"warning: installed successfully but could not remove previous payload {previous}: {exc}",
                file=sys.stderr,
            )
    try:
        sync_result = subprocess.run(["sync"], check=False)
    except OSError as exc:
        print(
            f"warning: install committed but could not run sync: {exc}",
            file=sys.stderr,
        )
    else:
        if sync_result.returncode != 0:
            print(
                f"warning: install committed but sync exited {sync_result.returncode}",
                file=sys.stderr,
            )
    return backup


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install a generated STUdio/Lunii archive directly onto a mounted Lunii device."
    )
    parser.add_argument("pack_zip", type=Path, help="generated STUdio archive zip")
    parser.add_argument("--mount", type=Path, required=True, help="mounted Lunii device root, e.g. /media/tom/LUNII")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="validate and print actions without writing device content")
    mode.add_argument("--yes", action="store_true", help="perform the install")
    parser.add_argument("--replace", action="store_true", help="replace existing same-UUID pack folder and .pi entry")
    parser.add_argument("--keep-work-dir", action="store_true", help="leave the generated FS pack work directory in place")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    dry_run = not args.yes
    summary: FsBuildSummary | None = None
    try:
        pack = read_archive(args.pack_zip)
        summary = build_fs_pack(pack)
        device = verify_mount(args.mount)

        entries = read_pack_index(args.mount / ".pi")
        new_entries, pi_action = plan_index(entries, summary.pack_uuid, args.replace)
        target = args.mount / ".content" / summary.pack8
        if target.exists() and not args.replace:
            fail(f"target pack folder already exists; pass --replace to replace it: {target}")
        files, bytes_total = count_copy_payload(summary.work_dir, include_boot=True)
        free_bytes = shutil.disk_usage(args.mount).free
        if free_bytes < bytes_total:
            fail(f"not enough free space on device: need {bytes_total} bytes, have {free_bytes}")

        print(f"pack: {summary.title or '(untitled)'}")
        print(f"uuid: {summary.pack_uuid}")
        print(f"version: {summary.version}")
        print(f"device firmware: {device.firmware_major}.{device.firmware_minor} (metadata {device.metadata_version})")
        print(f"target: {target}")
        print(
            "graph: "
            f"{summary.stage_count} stages, {summary.action_count} actions, "
            f"{summary.image_count} images, {summary.sound_count} sounds"
        )
        print(f"payload: {files} files, {bytes_total} bytes")
        print(f".pi: {len(entries)} entries -> {len(new_entries)} entries ({pi_action})")

        if dry_run:
            print("dry-run: no device writes performed")
            return 0

        backup = install(summary, args.mount, device, entries, args.replace)
        print(f"installed: {target}")
        print(f".pi backup: {backup}")
        return 0
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if summary is not None and not args.keep_work_dir:
            shutil.rmtree(summary.work_dir, ignore_errors=True)
            try:
                summary.work_dir.parent.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
