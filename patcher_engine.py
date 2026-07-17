from __future__ import annotations

import hashlib
import os
import shutil
import struct
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


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.original{path.suffix}")


def _related_swf_paths(path: Path) -> tuple[Path, ...]:
    if path.name in CONTENT_SWF_NAMES:
        installation = path.parent.parent.parent
    elif path.name == ROOT_SWF_NAME:
        installation = path.parent
    else:
        return (path,)

    paths = (
        installation / ROOT_SWF_NAME,
        *(installation / "applicationStorageDirectory" / "swf" / name for name in CONTENT_SWF_NAMES),
    )
    missing = [candidate.name for candidate in paths if not candidate.is_file()]
    if missing:
        raise InvalidSwfError(
            "The ebook installation is incomplete. Missing ebook SWF: " + ", ".join(missing)
        )
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
    analyses = {candidate: _analyze(data) for candidate, data in data_by_path.items()}
    target_count = sum(len(analysis.target_urls) for analysis in analyses.values())
    legacy_count = sum(analysis.legacy_url_count for analysis in analyses.values())
    if legacy_count == target_count:
        state = "original"
    elif legacy_count == 0:
        state = "patched"
    else:
        state = "mixed"
    backup_paths = tuple(_backup_path(candidate) for candidate in paths)
    existing_backups = [backup for backup in backup_paths if backup.is_file()]
    for backup in existing_backups:
        _validate_backup(backup)
    selected_analysis = analyses[swf_path]
    servers = tuple(sorted({server for analysis in analyses.values() for server in analysis.servers}))
    return InspectionResult(
        path=swf_path,
        state=state,
        compression=selected_analysis.compression,
        version=selected_analysis.version,
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


def _validate_backup(path: Path) -> bytes:
    if not path.is_file():
        raise MissingBackupError("No original SWF backup was found.")
    data = _read_file(path)
    analysis = _analyze(data)
    if analysis.legacy_url_count != len(analysis.target_urls):
        raise PatcherError("The existing backup is not an unpatched original SWF.")
    return data


def _create_backup(path: Path, data: bytes) -> Path:
    backup = _backup_path(path)
    if backup.exists():
        _validate_backup(backup)
        return backup
    created = False
    try:
        with backup.open("xb") as handle:
            created = True
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        _validate_backup(backup)
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
            mode="wb", prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent, delete=False
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


def patch_swf(path, server_url) -> PatchResult:
    swf_path = Path(path).expanduser().resolve()
    paths = _related_swf_paths(swf_path)
    server = normalize_server_url(server_url)
    original_data = {candidate: _read_file(candidate) for candidate in paths}
    before = {candidate: _analyze(data) for candidate, data in original_data.items()}
    before_hash = _sha256(original_data[swf_path])

    backups: dict[Path, Path] = {}
    for candidate in paths:
        backup = _backup_path(candidate)
        if backup.exists():
            _validate_backup(backup)
        elif before[candidate].legacy_url_count != len(before[candidate].target_urls):
            raise PatcherError("This SWF set is already patched and its original backup is missing.")
        else:
            backup = _create_backup(candidate, original_data[candidate])
        backups[candidate] = backup

    output_data: dict[Path, bytes] = {}
    output_hashes: dict[Path, str] = {}
    changed = 0
    for candidate in paths:
        analysis = before[candidate]
        rewritten_body, file_changes = _rewrite_body(
            _decode_swf(original_data[candidate])[2], server
        )
        result = _encode_swf(analysis.compression, analysis.version, rewritten_body)
        after = _analyze(result)
        if any(_rewrite_url(value, server) != value for value in after.target_urls):
            raise PatcherError(
                f"Patched SWF verification failed for {candidate.name}: not every dependency was rewritten."
            )
        if len(after.target_urls) != len(analysis.target_urls):
            raise PatcherError(
                f"Patched SWF verification failed for {candidate.name}: dependency count changed."
            )
        output_data[candidate] = result
        output_hashes[candidate] = _sha256(result)
        changed += file_changes

    replaced: list[Path] = []
    try:
        for candidate in paths:
            if output_data[candidate] == original_data[candidate]:
                continue
            expected_hash = output_hashes[candidate]

            def verify(temp_path: Path, expected_hash=expected_hash):
                temp_data = _read_file(temp_path)
                temp_analysis = _analyze(temp_data)
                if _sha256(temp_data) != expected_hash or temp_analysis.legacy_url_count:
                    raise PatcherError("The temporary patched SWF failed verification.")

            _atomic_replace(candidate, output_data[candidate], verify)
            replaced.append(candidate)
    except PatcherError:
        for candidate in reversed(replaced):
            rollback_data = original_data[candidate]
            rollback_hash = _sha256(rollback_data)

            def verify_rollback(temp_path: Path, rollback_hash=rollback_hash):
                if _sha256(_read_file(temp_path)) != rollback_hash:
                    raise PatcherError("Could not verify the patch rollback.")

            _atomic_replace(candidate, rollback_data, verify_rollback)
        raise

    after_hash = output_hashes[swf_path]

    return PatchResult(
        path=swf_path,
        server_url=server,
        changed_url_count=changed,
        before_sha256=before_hash,
        after_sha256=after_hash,
        backup_path=backups[swf_path],
        message=(
            f"Patched {changed} online dependencies to {server}."
            if changed
            else f"All {len(paths)} ebook SWFs already point to {server} "
            f"({sum(len(item.target_urls) for item in before.values())} dependencies verified)."
        ),
    )


def restore_swf(path) -> RestoreResult:
    swf_path = Path(path).expanduser().resolve()
    paths = _related_swf_paths(swf_path)
    backups = {candidate: _backup_path(candidate) for candidate in paths}
    backup_data = {candidate: _validate_backup(backup) for candidate, backup in backups.items()}
    current_data = {candidate: _read_file(candidate) for candidate in paths}
    before_hash = _sha256(current_data[swf_path])
    backup_hashes = {candidate: _sha256(data) for candidate, data in backup_data.items()}

    restored: list[Path] = []
    try:
        for candidate in paths:
            expected_hash = backup_hashes[candidate]

            def verify(temp_path: Path, expected_hash=expected_hash):
                temp_data = _read_file(temp_path)
                temp_analysis = _analyze(temp_data)
                if (
                    _sha256(temp_data) != expected_hash
                    or temp_analysis.legacy_url_count != len(temp_analysis.target_urls)
                ):
                    raise PatcherError("The temporary restored SWF failed verification.")

            _atomic_replace(candidate, backup_data[candidate], verify)
            restored.append(candidate)
    except PatcherError:
        for candidate in reversed(restored):
            prior_data = current_data[candidate]
            prior_hash = _sha256(prior_data)

            def verify_rollback(temp_path: Path, prior_hash=prior_hash):
                if _sha256(_read_file(temp_path)) != prior_hash:
                    raise PatcherError("Could not verify the restore rollback.")

            _atomic_replace(candidate, prior_data, verify_rollback)
        raise

    for backup in backups.values():
        try:
            backup.unlink()
        except PermissionError as exc:
            raise PermissionPatcherError(
                "The SWFs were restored, but Windows would not remove every backup. Rerun as administrator."
            ) from exc
        except OSError as exc:
            raise PatcherError(f"The SWFs were restored, but a backup could not be removed: {exc}") from exc
    return RestoreResult(
        path=swf_path,
        backup_path=backups[swf_path],
        before_sha256=before_hash,
        after_sha256=backup_hashes[swf_path],
        message="Original SWFs restored. Backups removed.",
    )
