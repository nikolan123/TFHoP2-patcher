from __future__ import annotations

import bz2
import hashlib
import os
import shutil
import struct
import sys
import tempfile
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit


LEGACY_HOSTS = {
    "www.gameslice.com",
    "ww.gameslice.com",
    "geoffkeighley.com",
    "www.thefinalhoursofportal2.com",
    "www.youtube.com",
    "a1.mzstatic.com",
    "a2.mzstatic.com",
    "a4.mzstatic.com",
    "a5.mzstatic.com",
}

SIMPLE_PATHS = {
    "/p2/index.php5",
    "/p2polls/poll1.php5",
    "/p2polls/poll3.php5",
    "/p2polls/poll4.php5",
    "/p2polls/poll5.php5",
    "/p2polls/poll6.php5",
    "/p2polls/subscribe/index.php5",
    "/TFHoP2_assets/howto/diagram.html",
    "/TFHoP2_assets/interactive/index.html",
    "/TFHoP2_assets/oddcouple/",
    "/TFHoP2_assets/companioncube/",
}

AUDIO_PATHS = {
    "/us/r1000/050/Music/89/1f/6d/mzm.hmsvmlsc.aac.p.m4a",
    "/us/r1000/013/Music/9f/da/c0/mzi.dviddoso.aac.p.m4a",
    "/us/r30/Music/2c/81/ca/mzi.wxxicnqc.aac.p.m4a",
    "/us/r1000/013/Music/90/2a/b7/mzm.ojhmmnlb.aac.p.m4a",
    "/us/r1000/022/Music/24/12/e8/mzi.bukbidcp.aac.p.m4a",
    "/us/r1000/026/Music/e4/6f/19/mzm.hcizchvw.aac.p.m4a",
    "/us/r1000/036/Music/d7/5e/9b/mzi.eyucogyw.aac.p.m4a",
    "/us/r30/Music/3e/c7/48/mzm.cxiybvxa.aac.p.m4a",
}

REQUIRED_PATHS = {
    "/p2polls/poll1.php5",
    "/TFHoP2_assets/howto/diagram.html",
    "/apiplayer",
}

CONTENT_SWF_NAMES = (
    "TheFinalHoursOfPortal2_0-9.swf",
    "TheFinalHoursOfPortal2_10-17.swf",
)
ROOT_SWF_NAME = "TheFinalHoursOfPortal2.swf"
PANORAMA_SWF_NAMES = (
    "valve_lobby_vr.swf",
    "valve_room1_vr.swf",
    "valve_room2_vr.swf",
)
URL_SWF_NAMES = (ROOT_SWF_NAME, *CONTENT_SWF_NAMES)

# The tiny BSDIFF files contain only the ActionScript compatibility changes. They are
# applied to decompressed SWF bodies, so none of the original panorama images are bundled.
PANORAMA_PATCHES = {
    ROOT_SWF_NAME: (
        "panorama-root.bsdiff",
        "7f7afac3bc8b27745d5d0ca99947f1e14320e187ed9d709a37c7dad67719d04d",
        "b2db2b7acb8d4b11ad193c13b5a01d5db0659324e5a1caf559c1061c1e534e5f",
    ),
    "valve_lobby_vr.swf": (
        "panorama-lobby.bsdiff",
        "1cab3016c234d16d1cf5e82da41cae3d124f144d23a17c5059c1a9aac221df74",
        "4037fa10671cf7e6909b786f6ff567ac78b5910715023a3f84945e9b4d5650b0",
    ),
    "valve_room1_vr.swf": (
        "panorama-room1.bsdiff",
        "1467e0a2e8183a220f2bcc140fe938554ca445ef34eff4c6bed014e95ceee794",
        "1a168062178ae59d1ef679af47f91a973eb95fb46703f76556394ef7359421d7",
    ),
    "valve_room2_vr.swf": (
        "panorama-room2.bsdiff",
        "2adf0ebf1fae760f7cd51360cbec856eef6e718ab61dd7fec2d32cfd138d5188",
        "266eab0a6c02d1c15ba0cd8a54ccd21e77db0c24c25cc2debecf52f78170e77d",
    ),
}
ROOT_PANORAMA_CANONICAL_HASHES = {
    "original": "d8b15b294d34c3bf147c20b52c7aa8f73b750bfd55e5d825ddb518e8939b431e",
    "patched": "587f1b55123f698546faa37fa69d272b0d027beee468bd388cf2c38b7d830bf1",
}


class PatcherError(Exception):
    """Base class for errors safe to show directly in the GUI."""


class InvalidSwfError(PatcherError):
    pass


class UnsupportedSwfError(PatcherError):
    pass


class InvalidServerError(PatcherError):
    pass


class MissingBackupError(PatcherError):
    pass


class PermissionPatcherError(PatcherError):
    pass


@dataclass(frozen=True)
class InspectionResult:
    path: Path
    state: str
    compression: str
    version: int
    target_url_count: int
    legacy_url_count: int
    servers: tuple[str, ...]
    sha256: str
    backup_path: Path
    backup_exists: bool


@dataclass(frozen=True)
class ServerStatus:
    url: str
    reachable: bool
    detail: str


@dataclass(frozen=True)
class PatchResult:
    path: Path
    server_url: str
    changed_url_count: int
    before_sha256: str
    after_sha256: str
    backup_path: Path
    message: str


@dataclass(frozen=True)
class RestoreResult:
    path: Path
    backup_path: Path
    before_sha256: str | None
    after_sha256: str
    message: str


@dataclass(frozen=True)
class _Analysis:
    compression: str
    version: int
    urls: tuple[str, ...]
    target_urls: tuple[str, ...]
    legacy_url_count: int
    servers: tuple[str, ...]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _report(progress, message: str) -> None:
    if progress is not None:
        progress(message)


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.original{path.suffix}")


def _related_swf_paths(path: Path) -> tuple[Path, ...]:
    if path.name in (*CONTENT_SWF_NAMES, *PANORAMA_SWF_NAMES):
        installation = path.parent.parent.parent
    elif path.name == ROOT_SWF_NAME:
        installation = path.parent
    else:
        return (path,)

    paths = (
        installation / ROOT_SWF_NAME,
        *(installation / "applicationStorageDirectory" / "swf" / name for name in CONTENT_SWF_NAMES),
        *(installation / "applicationStorageDirectory" / "swf" / name for name in PANORAMA_SWF_NAMES),
    )
    missing = [candidate.name for candidate in paths if not candidate.is_file()]
    if missing:
        raise InvalidSwfError("The ebook installation is incomplete. Missing ebook SWF: " + ", ".join(missing))
    return paths


def normalize_server_url(server_url: str) -> str:
    value = server_url.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise InvalidServerError(f"Invalid revival server address: {exc}") from exc

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise InvalidServerError("The revival server must be a complete http:// or https:// address.")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidServerError("The revival server address cannot contain a username or password.")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise InvalidServerError("Use only the server origin, without a path, query, or fragment.")

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def _is_target_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.path in SIMPLE_PATHS or parsed.path in AUDIO_PATHS:
        return True
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if parsed.path == "/p2polls/poll.php5" and query.get("id") in {"20", "21"}:
        return True
    if parsed.path == "/apiplayer" and query.get("version") == "3":
        return True
    return False


def _rewrite_url(value: str, server_url: str) -> str:
    if not _is_target_url(value):
        return value
    parsed = urlsplit(value.strip())
    server = urlsplit(server_url)
    return urlunsplit((server.scheme, server.netloc, parsed.path, parsed.query, parsed.fragment))


def _read_u30(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(5):
        if offset >= len(data):
            raise InvalidSwfError("The ActionScript constant pool is truncated.")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << (index * 7)
        if not byte & 0x80:
            if value > 0x3FFFFFFF:
                raise InvalidSwfError("Invalid U30 value in the ActionScript constant pool.")
            return value, offset
    raise InvalidSwfError("Invalid variable-length integer in the ActionScript constant pool.")


def _read_encoded_u32(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(5):
        if offset >= len(data):
            raise InvalidSwfError("The ActionScript constant pool is truncated.")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << (index * 7)
        if not byte & 0x80:
            return value & 0xFFFFFFFF, offset
    raise InvalidSwfError("Invalid variable-length integer in the ActionScript constant pool.")


def _write_u30(value: int) -> bytes:
    if not 0 <= value <= 0x3FFFFFFF:
        raise InvalidSwfError("ActionScript string length exceeds the U30 range.")
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            output.append(byte | 0x80)
        else:
            output.append(byte)
            return bytes(output)


def _skip_encoded_values(data: bytes, offset: int, count: int) -> int:
    for _ in range(max(0, count - 1)):
        _, offset = _read_encoded_u32(data, offset)
    return offset


def _string_pool_bounds(abc: bytes) -> tuple[int, list[tuple[int, int, int]], int]:
    if len(abc) < 4:
        raise InvalidSwfError("A DoABC tag has a truncated ActionScript header.")
    offset = 4

    int_count, offset = _read_u30(abc, offset)
    offset = _skip_encoded_values(abc, offset, int_count)
    uint_count, offset = _read_u30(abc, offset)
    offset = _skip_encoded_values(abc, offset, uint_count)
    double_count, offset = _read_u30(abc, offset)
    double_bytes = max(0, double_count - 1) * 8
    if offset + double_bytes > len(abc):
        raise InvalidSwfError("The ActionScript double constant pool is truncated.")
    offset += double_bytes

    string_count_offset = offset
    string_count, offset = _read_u30(abc, offset)
    strings: list[tuple[int, int, int]] = []
    for _ in range(max(0, string_count - 1)):
        length_offset = offset
        length, data_offset = _read_u30(abc, offset)
        end = data_offset + length
        if end > len(abc):
            raise InvalidSwfError("The ActionScript string constant pool is truncated.")
        strings.append((length_offset, data_offset, end))
        offset = end
    return string_count_offset, strings, offset


def _strings_from_abc(abc: bytes) -> list[str]:
    _, strings, _ = _string_pool_bounds(abc)
    output: list[str] = []
    for _, start, end in strings:
        try:
            output.append(abc[start:end].decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return output


def _rewrite_abc(abc: bytes, server_url: str) -> tuple[bytes, int]:
    string_count_offset, strings, pool_end = _string_pool_bounds(abc)
    output = bytearray(abc[:string_count_offset])
    string_count, after_count = _read_u30(abc, string_count_offset)
    output.extend(abc[string_count_offset:after_count])
    changed = 0

    for _, start, end in strings:
        raw = abc[start:end]
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            new_raw = raw
        else:
            replacement = _rewrite_url(value, server_url)
            new_raw = replacement.encode("utf-8")
            changed += replacement != value
        output.extend(_write_u30(len(new_raw)))
        output.extend(new_raw)

    expected_strings = max(0, string_count - 1)
    if len(strings) != expected_strings:
        raise InvalidSwfError("The ActionScript string pool count is inconsistent.")
    output.extend(abc[pool_end:])
    return bytes(output), changed


def _decode_swf(data: bytes) -> tuple[str, int, bytes]:
    if len(data) < 8:
        raise InvalidSwfError("The selected file is too short to be a SWF.")
    signature = data[:3]
    version = data[3]
    declared_length = struct.unpack_from("<I", data, 4)[0]
    try:
        if signature == b"CWS":
            body = zlib.decompress(data[8:])
            compression = "CWS"
        elif signature == b"FWS":
            body = data[8:]
            compression = "FWS"
        elif signature == b"ZWS":
            raise UnsupportedSwfError("LZMA-compressed ZWS files are not supported.")
        else:
            raise InvalidSwfError("The selected file is not a SWF.")
    except zlib.error as exc:
        raise InvalidSwfError(f"The compressed SWF data is damaged: {exc}") from exc
    if declared_length != len(body) + 8:
        raise InvalidSwfError("The SWF declared length does not match its contents.")
    return compression, version, body


def _tag_offset(body: bytes) -> int:
    if not body:
        raise InvalidSwfError("The SWF header is missing.")
    nbits = body[0] >> 3
    rect_length = (5 + (nbits * 4) + 7) // 8
    offset = rect_length + 4
    if offset > len(body):
        raise InvalidSwfError("The SWF frame header is truncated.")
    return offset


def _iter_tags(body: bytes):
    offset = _tag_offset(body)
    while offset < len(body):
        tag_start = offset
        if offset + 2 > len(body):
            raise InvalidSwfError("A SWF tag header is truncated.")
        header = struct.unpack_from("<H", body, offset)[0]
        offset += 2
        tag_code = header >> 6
        length = header & 0x3F
        if length == 0x3F:
            if offset + 4 > len(body):
                raise InvalidSwfError("A long SWF tag header is truncated.")
            length = struct.unpack_from("<I", body, offset)[0]
            offset += 4
        data_start = offset
        data_end = data_start + length
        if data_end > len(body):
            raise InvalidSwfError("A SWF tag extends beyond the end of the file.")
        yield tag_start, tag_code, data_start, data_end
        offset = data_end
        if tag_code == 0:
            if offset != len(body):
                raise InvalidSwfError("Unexpected data follows the SWF end tag.")
            return
    raise InvalidSwfError("The SWF end tag is missing.")


def _abc_from_tag(tag_data: bytes) -> tuple[bytes, bytes]:
    if len(tag_data) < 5:
        raise InvalidSwfError("A DoABC tag is truncated.")
    name_end = tag_data.find(b"\0", 4)
    if name_end == -1:
        raise InvalidSwfError("A DoABC tag name is not terminated.")
    prefix_end = name_end + 1
    return tag_data[:prefix_end], tag_data[prefix_end:]


def _tag_header(tag_code: int, length: int) -> bytes:
    if length < 0x3F:
        return struct.pack("<H", (tag_code << 6) | length)
    return struct.pack("<HI", (tag_code << 6) | 0x3F, length)


def _all_strings(body: bytes) -> list[str]:
    strings: list[str] = []
    for _, tag_code, start, end in _iter_tags(body):
        if tag_code == 82:
            _, abc = _abc_from_tag(body[start:end])
            strings.extend(_strings_from_abc(abc))
    return strings


def _rewrite_body(body: bytes, server_url: str) -> tuple[bytes, int]:
    output = bytearray()
    cursor = 0
    changed = 0
    for tag_start, tag_code, data_start, data_end in _iter_tags(body):
        output.extend(body[cursor:tag_start])
        tag_data = body[data_start:data_end]
        if tag_code == 82:
            prefix, abc = _abc_from_tag(tag_data)
            rewritten_abc, tag_changes = _rewrite_abc(abc, server_url)
            tag_data = prefix + rewritten_abc
            changed += tag_changes
        output.extend(_tag_header(tag_code, len(tag_data)))
        output.extend(tag_data)
        cursor = data_end
    output.extend(body[cursor:])
    return bytes(output), changed


def _encode_swf(compression: str, version: int, body: bytes) -> bytes:
    header = compression.encode("ascii") + bytes((version,)) + struct.pack("<I", len(body) + 8)
    if compression == "CWS":
        return header + zlib.compress(body, level=9)
    return header + body


def _bsdiff_int(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 8 > len(data):
        raise PatcherError("The bundled panorama patch is damaged.")
    value = data[offset + 7] & 0x7F
    for index in range(6, -1, -1):
        value = (value * 256) + data[offset + index]
    return -value if data[offset + 7] & 0x80 else value


def _apply_bsdiff(old: bytes, patch: bytes) -> bytes:
    if len(patch) < 32 or patch[:8] != b"BSDIFF40":
        raise PatcherError("The bundled panorama patch is invalid.")
    control_length = _bsdiff_int(patch, 8)
    diff_length = _bsdiff_int(patch, 16)
    new_size = _bsdiff_int(patch, 24)
    if min(control_length, diff_length, new_size) < 0:
        raise PatcherError("The bundled panorama patch has invalid lengths.")
    control_end = 32 + control_length
    diff_end = control_end + diff_length
    if diff_end > len(patch):
        raise PatcherError("The bundled panorama patch is truncated.")
    try:
        control = bz2.decompress(patch[32:control_end])
        diff = bz2.decompress(patch[control_end:diff_end])
        extra = bz2.decompress(patch[diff_end:])
    except (OSError, EOFError, ValueError) as exc:
        raise PatcherError("The bundled panorama patch is damaged.") from exc

    result = bytearray(new_size)
    old_position = new_position = control_position = diff_position = extra_position = 0
    while new_position < new_size:
        if control_position + 24 > len(control):
            raise PatcherError("The bundled panorama patch control data is truncated.")
        add_length = _bsdiff_int(control, control_position)
        copy_length = _bsdiff_int(control, control_position + 8)
        seek = _bsdiff_int(control, control_position + 16)
        control_position += 24
        if add_length < 0 or copy_length < 0 or new_position + add_length + copy_length > new_size:
            raise PatcherError("The bundled panorama patch contains invalid instructions.")
        if diff_position + add_length > len(diff) or extra_position + copy_length > len(extra):
            raise PatcherError("The bundled panorama patch data is truncated.")
        for index in range(add_length):
            old_index = old_position + index
            old_value = old[old_index] if 0 <= old_index < len(old) else 0
            result[new_position + index] = (diff[diff_position + index] + old_value) & 0xFF
        new_position += add_length
        old_position += add_length
        diff_position += add_length
        result[new_position : new_position + copy_length] = extra[extra_position : extra_position + copy_length]
        new_position += copy_length
        extra_position += copy_length
        old_position += seek
    return bytes(result)


def _asset_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "assets" / name


def _apply_panorama_patch(path: Path, data: bytes) -> bytes:
    patch_info = PANORAMA_PATCHES.get(path.name)
    if patch_info is None:
        return data
    patch_name, original_hash, patched_hash = patch_info
    compression, version, body = _decode_swf(data)
    body_hash = _sha256(body)
    if body_hash == patched_hash:
        return data
    if body_hash != original_hash:
        raise InvalidSwfError(f"{path.name} is not the supported original ebook version required for the panorama fix.")
    try:
        patch_data = _asset_path(patch_name).read_bytes()
    except (FileNotFoundError, OSError) as exc:
        raise PatcherError(f"The bundled panorama patch is missing: {patch_name}") from exc
    patched_body = _apply_bsdiff(body, patch_data)
    if _sha256(patched_body) != patched_hash:
        raise PatcherError(f"Panorama compatibility verification failed for {path.name}.")
    return _encode_swf(compression, version, patched_body)


def _panorama_patch_state(path: Path, data: bytes) -> str:
    _, _, body = _decode_swf(data)
    if path.name == ROOT_SWF_NAME:
        body, _ = _rewrite_body(body, "https://canonical.invalid")
        body_hash = _sha256(body)
        if body_hash == ROOT_PANORAMA_CANONICAL_HASHES["original"]:
            return "original"
        if body_hash == ROOT_PANORAMA_CANONICAL_HASHES["patched"]:
            return "patched"
        return "mixed"
    _, original_hash, patched_hash = PANORAMA_PATCHES[path.name]
    body_hash = _sha256(body)
    if body_hash == original_hash:
        return "original"
    if body_hash == patched_hash:
        return "patched"
    return "mixed"


def _analyze(data: bytes) -> _Analysis:
    compression, version, body = _decode_swf(data)
    strings = _all_strings(body)
    urls = tuple(value for value in strings if value.startswith(("http://", "https://")))
    targets = tuple(value for value in urls if _is_target_url(value))
    paths = {urlsplit(value).path for value in targets}
    if len(targets) < 10 or not REQUIRED_PATHS.issubset(paths):
        raise InvalidSwfError("This does not appear to be The Final Hours of Portal 2 SWF.")

    legacy_count = 0
    servers: set[str] = set()
    for value in targets:
        parsed = urlsplit(value)
        if parsed.hostname and parsed.hostname.lower() in LEGACY_HOSTS:
            legacy_count += 1
        else:
            servers.add(urlunsplit((parsed.scheme, parsed.netloc, "", "", "")))
    return _Analysis(
        compression=compression,
        version=version,
        urls=urls,
        target_urls=targets,
        legacy_url_count=legacy_count,
        servers=tuple(sorted(servers)),
    )


def _read_file(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise InvalidSwfError(f"SWF not found: {path}") from exc
    except PermissionError as exc:
        raise PermissionPatcherError(
            "Windows denied access. Close the ebook and rerun the patcher as administrator."
        ) from exc
    except OSError as exc:
        raise PatcherError(f"Could not read the SWF: {exc}") from exc


def inspect_swf(path) -> InspectionResult:
    swf_path = Path(path).expanduser().resolve()
    paths = _related_swf_paths(swf_path)
    data_by_path = {candidate: _read_file(candidate) for candidate in paths}
    analyses = {
        candidate: _analyze(data) for candidate, data in data_by_path.items() if candidate.name in URL_SWF_NAMES
    }
    target_count = sum(len(analysis.target_urls) for analysis in analyses.values())
    legacy_count = sum(analysis.legacy_url_count for analysis in analyses.values())
    if legacy_count == target_count:
        online_state = "original"
    elif legacy_count == 0:
        online_state = "patched"
    else:
        online_state = "mixed"
    panorama_states = {
        _panorama_patch_state(candidate, data_by_path[candidate])
        for candidate in paths
        if candidate.name in PANORAMA_PATCHES
    }
    panorama_state = panorama_states.pop() if len(panorama_states) == 1 else "mixed"
    if online_state == panorama_state == "original":
        state = "original"
    elif online_state == panorama_state == "patched":
        state = "patched"
    else:
        state = "mixed"
    backup_paths = tuple(_backup_path(candidate) for candidate in paths)
    existing_backups = [backup for backup in backup_paths if backup.is_file()]
    for candidate, backup in zip(paths, backup_paths):
        if backup.is_file():
            _validate_backup(backup, candidate)
    selected_analysis = analyses.get(swf_path)
    if selected_analysis is None:
        compression, version, _ = _decode_swf(data_by_path[swf_path])
    else:
        compression = selected_analysis.compression
        version = selected_analysis.version
    servers = tuple(sorted({server for analysis in analyses.values() for server in analysis.servers}))
    return InspectionResult(
        path=swf_path,
        state=state,
        compression=compression,
        version=version,
        target_url_count=target_count,
        legacy_url_count=legacy_count,
        servers=servers,
        sha256=_sha256(data_by_path[swf_path]),
        backup_path=_backup_path(swf_path),
        backup_exists=len(existing_backups) == len(backup_paths),
    )


def probe_server(server_url, timeout=2.0) -> ServerStatus:
    normalized = normalize_server_url(server_url)
    request = urllib.request.Request(normalized + "/", headers={"User-Agent": "TFHoP2-Revival-Patcher/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return ServerStatus(normalized, True, f"Server responded with HTTP {response.status}.")
    except urllib.error.HTTPError as exc:
        return ServerStatus(normalized, True, f"Server responded with HTTP {exc.code}.")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return ServerStatus(normalized, False, str(getattr(exc, "reason", exc)))


def _validate_original_data(path: Path, data: bytes) -> None:
    patch_info = PANORAMA_PATCHES.get(path.name)
    if patch_info is not None:
        _, original_hash, _ = patch_info
        _, _, body = _decode_swf(data)
        if _sha256(body) != original_hash:
            raise PatcherError(f"The existing backup for {path.name} is not the supported original.")
    if path.name in URL_SWF_NAMES:
        analysis = _analyze(data)
        if analysis.legacy_url_count != len(analysis.target_urls):
            raise PatcherError(f"The existing backup for {path.name} is not unpatched.")


def _validate_backup(path: Path, target: Path) -> bytes:
    if not path.is_file():
        raise MissingBackupError("No original SWF backup was found.")
    data = _read_file(path)
    _validate_original_data(target, data)
    return data


def _create_backup(path: Path, data: bytes) -> Path:
    backup = _backup_path(path)
    if backup.exists():
        _validate_backup(backup, path)
        return backup
    created = False
    try:
        with backup.open("xb") as handle:
            created = True
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        _validate_backup(backup, path)
    except PermissionError as exc:
        if created:
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
        raise PermissionPatcherError(
            "Windows denied permission to create the backup. Rerun the patcher as administrator."
        ) from exc
    except OSError as exc:
        if created:
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
        raise PatcherError(f"Could not create the original SWF backup: {exc}") from exc
    return backup


def _atomic_replace(path: Path, data: bytes, verify) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.stem}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        verify(temporary)
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
        temporary = None
    except PermissionError as exc:
        raise PermissionPatcherError(
            "Windows denied permission to replace the SWF. Close the ebook and rerun the patcher as administrator."
        ) from exc
    except PatcherError:
        raise
    except OSError as exc:
        raise PatcherError(f"Could not replace the SWF. Make sure the ebook is closed: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def patch_swf(
    path,
    server_url,
    fix_online_services=True,
    fix_panoramas=True,
    progress=None,
) -> PatchResult:
    if not fix_online_services and not fix_panoramas:
        raise PatcherError("Select at least one patch.")
    swf_path = Path(path).expanduser().resolve()
    paths = _related_swf_paths(swf_path)
    server = normalize_server_url(server_url) if fix_online_services else server_url.strip()
    _report(progress, f"Reading {len(paths)} ebook SWFs...")
    current_data = {candidate: _read_file(candidate) for candidate in paths}
    before_hash = _sha256(current_data[swf_path])

    preserved_server: str | None = None
    if fix_panoramas and not fix_online_services:
        current_analyses = {
            candidate: _analyze(current_data[candidate])
            for candidate in paths
            if candidate.name in URL_SWF_NAMES
        }
        legacy_count = sum(item.legacy_url_count for item in current_analyses.values())
        target_count = sum(len(item.target_urls) for item in current_analyses.values())
        current_servers = {value for item in current_analyses.values() for value in item.servers}
        if legacy_count == 0 and len(current_servers) == 1:
            preserved_server = current_servers.pop()
        elif legacy_count != target_count:
            raise PatcherError(
                "The online-service URLs are partially patched. Select Fix online services as well to repair them."
            )

    backups: dict[Path, Path] = {}
    backup_data: dict[Path, bytes] = {}
    for index, candidate in enumerate(paths, start=1):
        backup = _backup_path(candidate)
        if backup.exists():
            _report(progress, f"Verifying backup {index}/{len(paths)}: {candidate.name}")
            original = _validate_backup(backup, candidate)
        else:
            _report(progress, f"Creating backup {index}/{len(paths)}: {candidate.name}")
            try:
                _validate_original_data(candidate, current_data[candidate])
            except PatcherError as exc:
                raise PatcherError(f"{candidate.name} is already modified and its original backup is missing.") from exc
            original = current_data[candidate]
            backup = _create_backup(candidate, original)
        backups[candidate] = backup
        backup_data[candidate] = original

    output_data: dict[Path, bytes] = {}
    output_hashes: dict[Path, str] = {}
    changed = 0
    for index, candidate in enumerate(paths, start=1):
        _report(progress, f"Preparing patch {index}/{len(paths)}: {candidate.name}")
        if candidate.name == ROOT_SWF_NAME:
            if fix_panoramas:
                result = _apply_panorama_patch(candidate, backup_data[candidate])
            else:
                result = current_data[candidate]
        elif candidate.name in PANORAMA_SWF_NAMES:
            result = (
                _apply_panorama_patch(candidate, backup_data[candidate])
                if fix_panoramas
                else current_data[candidate]
            )
        else:
            result = backup_data[candidate] if fix_online_services else current_data[candidate]
        file_changes = 0
        if candidate.name in URL_SWF_NAMES and fix_online_services:
            analysis = _analyze(backup_data[candidate])
            compression, version, body = _decode_swf(result)
            rewritten_body, file_changes = _rewrite_body(body, server)
            result = _encode_swf(compression, version, rewritten_body)
            after = _analyze(result)
            if any(_rewrite_url(value, server) != value for value in after.target_urls):
                raise PatcherError(
                    f"Patched SWF verification failed for {candidate.name}: not every dependency was rewritten."
                )
            if len(after.target_urls) != len(analysis.target_urls):
                raise PatcherError(f"Patched SWF verification failed for {candidate.name}: dependency count changed.")
        elif candidate.name == ROOT_SWF_NAME and fix_panoramas and preserved_server is not None:
            compression, version, body = _decode_swf(result)
            rewritten_body, _ = _rewrite_body(body, preserved_server)
            result = _encode_swf(compression, version, rewritten_body)
        output_data[candidate] = result
        output_hashes[candidate] = _sha256(result)
        changed += file_changes

    replaced: list[Path] = []
    changed_paths = [candidate for candidate in paths if output_data[candidate] != current_data[candidate]]
    try:
        for index, candidate in enumerate(changed_paths, start=1):
            _report(progress, f"Installing SWF {index}/{len(changed_paths)}: {candidate.name}")
            expected_hash = output_hashes[candidate]

            def verify(temp_path: Path, expected_hash=expected_hash, candidate=candidate):
                temp_data = _read_file(temp_path)
                if _sha256(temp_data) != expected_hash:
                    raise PatcherError("The temporary patched SWF failed verification.")
                if fix_online_services and candidate.name in URL_SWF_NAMES and _analyze(temp_data).legacy_url_count:
                    raise PatcherError("The temporary patched SWF still contains retired URLs.")

            _atomic_replace(candidate, output_data[candidate], verify)
            replaced.append(candidate)
    except PatcherError:
        _report(progress, "An error occurred. Rolling back changed files...")
        for candidate in reversed(replaced):
            rollback_data = current_data[candidate]
            rollback_hash = _sha256(rollback_data)

            def verify_rollback(temp_path: Path, rollback_hash=rollback_hash):
                if _sha256(_read_file(temp_path)) != rollback_hash:
                    raise PatcherError("Could not verify the patch rollback.")

            _atomic_replace(candidate, rollback_data, verify_rollback)
        raise

    after_hash = output_hashes[swf_path]
    _report(progress, "All selected patches were installed successfully.")

    return PatchResult(
        path=swf_path,
        server_url=server,
        changed_url_count=changed,
        before_sha256=before_hash,
        after_sha256=after_hash,
        backup_path=backups[swf_path],
        message=(
            f"Patched {changed} online dependencies to {server} and applied the Windows compatibility fix to all 3 panoramas."
            if fix_online_services and fix_panoramas
            else f"Patched {changed} online dependencies to {server}."
            if fix_online_services
            else "Applied the Windows compatibility fix to all 3 panoramas."
        ),
    )


def restore_swf(path, progress=None) -> RestoreResult:
    swf_path = Path(path).expanduser().resolve()
    paths = _related_swf_paths(swf_path)
    backups = {candidate: _backup_path(candidate) for candidate in paths}
    backup_data = {}
    for index, (candidate, backup) in enumerate(backups.items(), start=1):
        _report(progress, f"Verifying backup {index}/{len(paths)}: {candidate.name}")
        backup_data[candidate] = _validate_backup(backup, candidate)
    _report(progress, f"Reading {len(paths)} installed SWFs...")
    current_data = {candidate: _read_file(candidate) for candidate in paths}
    before_hash = _sha256(current_data[swf_path])
    backup_hashes = {candidate: _sha256(data) for candidate, data in backup_data.items()}

    restored: list[Path] = []
    try:
        for index, candidate in enumerate(paths, start=1):
            _report(progress, f"Restoring SWF {index}/{len(paths)}: {candidate.name}")
            expected_hash = backup_hashes[candidate]

            def verify(temp_path: Path, expected_hash=expected_hash, candidate=candidate):
                temp_data = _read_file(temp_path)
                if _sha256(temp_data) != expected_hash:
                    raise PatcherError("The temporary restored SWF failed verification.")
                _validate_original_data(candidate, temp_data)

            _atomic_replace(candidate, backup_data[candidate], verify)
            restored.append(candidate)
    except PatcherError:
        _report(progress, "An error occurred. Rolling back restored files...")
        for candidate in reversed(restored):
            prior_data = current_data[candidate]
            prior_hash = _sha256(prior_data)

            def verify_rollback(temp_path: Path, prior_hash=prior_hash):
                if _sha256(_read_file(temp_path)) != prior_hash:
                    raise PatcherError("Could not verify the restore rollback.")

            _atomic_replace(candidate, prior_data, verify_rollback)
        raise

    _report(progress, "Removing original-file backups...")
    for backup in backups.values():
        try:
            backup.unlink()
        except PermissionError as exc:
            raise PermissionPatcherError(
                "The SWFs were restored, but Windows would not remove every backup. Rerun as administrator."
            ) from exc
        except OSError as exc:
            raise PatcherError(f"The SWFs were restored, but a backup could not be removed: {exc}") from exc
    _report(progress, "All original SWFs were restored successfully.")
    return RestoreResult(
        path=swf_path,
        backup_path=backups[swf_path],
        before_sha256=before_hash,
        after_sha256=backup_hashes[swf_path],
        message="Original SWFs restored. Backups removed.",
    )

if __name__ == "__main__":
    print("Wrong one. Run the other file.")
