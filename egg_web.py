#!/usr/bin/env python3
"""
In-memory EGG archive extraction for web use.
Accepts raw bytes (no filesystem access), returns JSON-serializable results.
"""
from __future__ import annotations

import base64
import bz2
import lzma
import struct
import sys
import unicodedata
import zlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

# ── Magic constants ──────────────────────────────────────────────────────────
EGG_MAGIC           = 0x41474745
FILE_MAGIC          = 0x0A8590E3
BLOCK_MAGIC         = 0x02B50C13
ENCRYPT_MAGIC       = 0x08D1470F
FILENAME_MAGIC      = 0x0A8591AC
WINDOWS_INFO_MAGIC  = 0x2C86950B
POSIX_INFO_MAGIC    = 0x1EE922E5
COMMENT_MAGIC       = 0x04C63672
SPLIT_MAGIC         = 0x24F5A262
SOLID_MAGIC         = 0x24E5A060
DUMMY_MAGIC         = 0x07463307
GLOBAL_ENCRYPT_MAGIC = 0x08D144A8
SKIP_MAGIC          = 0xFFFF0000
END_MAGIC           = 0x08E28222

DIRECTORY_ATTRIBUTE   = 0x80
POSIX_DIRECTORY_MODE  = 0o040000
DATA_INLINE_LIMIT     = 512 * 1024   # 512 KB — include base64 data field


# ── Low-level reader ─────────────────────────────────────────────────────────
class ByteReader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def tell(self) -> int:
        return self.offset

    def remaining(self) -> int:
        return len(self.data) - self.offset

    def read(self, size: int) -> bytes:
        if size < 0 or self.offset + size > len(self.data):
            raise ValueError("Unexpected end of archive")
        chunk = self.data[self.offset: self.offset + size]
        self.offset += size
        return chunk

    def peek_u32(self) -> int | None:
        if self.remaining() < 4:
            return None
        return struct.unpack_from("<I", self.data, self.offset)[0]

    def read_u8(self) -> int:
        return self.read(1)[0]

    def read_u16(self) -> int:
        return struct.unpack("<H", self.read(2))[0]

    def read_u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def read_u64(self) -> int:
        return struct.unpack("<Q", self.read(8))[0]


# ── Data classes ─────────────────────────────────────────────────────────────
@dataclass
class FieldRec:
    magic: int
    bitflag: int
    payload: bytes
    raw_size: int


@dataclass
class VolumeInfo:
    header_id: int
    prev_id: int
    next_id: int
    payload_offset: int   # byte offset where non-header payload begins
    data: bytes


@dataclass
class FileEntryWeb:
    file_id: int
    path: str             # relative posix path as string
    expected_size: int
    is_directory: bool = False
    posix_mode: int | None = None
    windows_attribute: int | None = None
    method: int = 0       # compression method of last block written
    buffer: bytearray = field(default_factory=bytearray)

    @property
    def complete(self) -> bool:
        return len(self.buffer) == self.expected_size


# ── Helpers ──────────────────────────────────────────────────────────────────
_METHOD_NAMES = {0: "store", 1: "deflate", 2: "bzip2", 3: "azo", 4: "lzma"}


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _decode_name(payload: bytes, bitflag: int) -> str:
    if bitflag & 0x04:
        raise ValueError("Encrypted filenames are not supported")
    if not (bitflag & 0x08):
        return payload.decode("utf-8")
    locale = 0
    name_bytes = payload
    if len(payload) >= 2:
        locale = struct.unpack_from("<H", payload, 0)[0]
        name_bytes = payload[2:]
    encoding = {0: sys.getfilesystemencoding() or "utf-8", 932: "cp932", 949: "cp949"}.get(locale, "utf-8")
    return name_bytes.decode(encoding, errors="replace")


def _sanitize_path(raw: str) -> str:
    """Return a clean posix-style relative path string (no .. or absolute refs)."""
    parts = [p for p in raw.replace("\\", "/").split("/") if p and p not in (".", "..")]
    return "/".join(parts)


def _read_field(reader: ByteReader) -> FieldRec:
    magic = reader.read_u32()
    if magic == END_MAGIC:
        raise ValueError("END magic is not a field")
    if magic == SKIP_MAGIC:
        return FieldRec(magic=magic, bitflag=0, payload=b"", raw_size=0)
    bitflag = reader.read_u8()
    size_is_u32 = bitflag & 0x01
    raw_size = reader.read_u32() if size_is_u32 else reader.read_u16()
    payload = reader.read(raw_size)
    return FieldRec(magic=magic, bitflag=bitflag, payload=payload, raw_size=raw_size)


def _decompress(method: int, payload: bytes) -> bytes:
    if method == 0:
        return payload
    if method == 1:
        return zlib.decompress(payload, -15)
    if method == 2:
        return bz2.decompress(payload)
    if method == 4:
        return lzma.decompress(payload)
    if method == 3:
        raise ValueError("AZO-compressed EGG blocks are not supported")
    raise ValueError(f"Unsupported compression method {method}")


# ── Volume parsing (for split archives) ──────────────────────────────────────
def parse_volume_info(data: bytes) -> VolumeInfo:
    reader = ByteReader(data)
    magic = reader.read_u32()
    version = reader.read_u16()
    if magic != EGG_MAGIC:
        raise ValueError("Data does not start with an EGG header")
    if version != 0x0100:
        raise ValueError(f"Unsupported EGG version {version:#x}")

    header_id = reader.read_u32()
    _reserved = reader.read_u32()

    prev_id = 0
    next_id = 0
    saw_split = False
    while True:
        m = reader.peek_u32()
        if m == END_MAGIC:
            reader.read_u32()
            break
        f = _read_field(reader)
        if f.magic == SPLIT_MAGIC:
            if f.raw_size != 8:
                raise ValueError(f"Unexpected split header size {f.raw_size}")
            prev_id, next_id = struct.unpack("<II", f.payload)
            saw_split = True

    if not saw_split:
        raise ValueError("This volume does not contain a SPLIT_MAGIC header")

    return VolumeInfo(
        header_id=header_id,
        prev_id=prev_id,
        next_id=next_id,
        payload_offset=reader.tell(),
        data=data,
    )


def order_volumes(volumes: list[VolumeInfo]) -> list[VolumeInfo]:
    by_header = {v.header_id: v for v in volumes}
    first = next((v for v in volumes if v.prev_id == 0), None)
    if first is None:
        raise ValueError("Could not find the first split volume (no volume has prev_id == 0)")

    ordered: list[VolumeInfo] = []
    seen: set[int] = set()
    current = first
    while True:
        if current.header_id in seen:
            raise ValueError("Split volume chain contains a cycle")
        ordered.append(current)
        seen.add(current.header_id)
        if current.next_id == 0:
            break
        current = by_header.get(current.next_id)
        if current is None:
            raise ValueError("A required split volume is missing from the uploaded files")

    if len(ordered) != len(volumes):
        raise ValueError("Split volume chain is incomplete or contains unrelated parts")

    return ordered


def build_stream(parts: list[bytes]) -> tuple[bytes, bool, int]:
    """
    Given a list of raw bytes (one per uploaded file), decide if they form a split
    archive, reorder/combine them, and return (combined_stream, is_split, volume_count).

    If only one file is uploaded and it is not a split volume, return it as-is.
    """
    if len(parts) == 1:
        data = parts[0]
        # Try to detect split by parsing the header
        try:
            parse_volume_info(data)
            # It parses as a split volume but only one part was given — still treat as split
            volumes = order_volumes([parse_volume_info(data)])
            stream = volumes[0].data
            return stream, True, 1
        except ValueError:
            return data, False, 1

    # Multiple parts — must be a split archive
    volume_infos = [parse_volume_info(p) for p in parts]
    ordered = order_volumes(volume_infos)
    combined = bytearray(ordered[0].data)
    for vol in ordered[1:]:
        combined.extend(vol.data[vol.payload_offset:])
    return bytes(combined), True, len(ordered)


# ── EGG archive extraction (in-memory) ───────────────────────────────────────
def _parse_egg_header(reader: ByteReader) -> tuple[bool, bool]:
    if reader.read_u32() != EGG_MAGIC:
        raise ValueError("Archive does not begin with an EGG header")
    version = reader.read_u16()
    if version != 0x0100:
        raise ValueError(f"Unsupported EGG version {version:#x}")
    _header_id = reader.read_u32()
    _reserved = reader.read_u32()

    is_split = False
    is_solid = False
    while True:
        m = reader.peek_u32()
        if m == END_MAGIC:
            reader.read_u32()
            break
        f = _read_field(reader)
        if f.magic == SPLIT_MAGIC:
            is_split = True
        elif f.magic == SOLID_MAGIC:
            is_solid = True
        elif f.magic == GLOBAL_ENCRYPT_MAGIC:
            raise ValueError("Encrypted EGG archives are not supported")

    return is_split, is_solid


def _parse_file_header(reader: ByteReader, id_to_path: dict[int, str]) -> FileEntryWeb:
    if reader.read_u32() != FILE_MAGIC:
        raise ValueError("Expected a file header")
    file_id = reader.read_u32()
    file_length = reader.read_u64()

    rel_path: str | None = None
    windows_attribute: int | None = None
    posix_mode: int | None = None

    while True:
        m = reader.peek_u32()
        if m == END_MAGIC:
            reader.read_u32()
            break
        f = _read_field(reader)
        if f.magic == FILENAME_MAGIC:
            if f.bitflag & 0x10:
                if len(f.payload) < 4:
                    raise ValueError("Relative filename field is truncated")
                parent_id = struct.unpack_from("<I", f.payload, 0)[0]
                name = _decode_name(f.payload[4:], f.bitflag)
                parent = id_to_path.get(parent_id)
                if parent is None:
                    raise ValueError(f"Relative filename references missing parent id {parent_id}")
                rel_path = (parent + "/" + _sanitize_path(name)).lstrip("/")
            else:
                rel_path = _sanitize_path(_decode_name(f.payload, f.bitflag))
        elif f.magic == WINDOWS_INFO_MAGIC:
            if f.raw_size >= 9:
                windows_attribute = f.payload[8]
        elif f.magic == POSIX_INFO_MAGIC:
            if f.raw_size >= 4:
                posix_mode = struct.unpack_from("<I", f.payload, 0)[0]
        elif f.magic == ENCRYPT_MAGIC:
            raise ValueError("Encrypted file entries are not supported")

    if rel_path is None:
        raise ValueError(f"File entry {file_id} does not have a filename header")

    is_dir = False
    if windows_attribute is not None:
        is_dir = bool(windows_attribute & DIRECTORY_ATTRIBUTE)
    if posix_mode is not None:
        is_dir = is_dir or (posix_mode & POSIX_DIRECTORY_MODE) == POSIX_DIRECTORY_MODE

    # Store the *unsanitized* path part for relative lookups, but a clean version as key
    id_to_path[file_id] = rel_path

    return FileEntryWeb(
        file_id=file_id,
        path=rel_path,
        expected_size=file_length,
        is_directory=is_dir,
        posix_mode=posix_mode,
        windows_attribute=windows_attribute,
    )


def _parse_block_header(reader: ByteReader) -> tuple[int, int, int, int]:
    if reader.read_u32() != BLOCK_MAGIC:
        raise ValueError("Expected a block header")
    method = reader.read_u8()
    _hint = reader.read_u8()
    uncompressed_size = reader.read_u32()
    compressed_size = reader.read_u32()
    expected_crc = reader.read_u32()

    while True:
        m = reader.peek_u32()
        if m == END_MAGIC:
            reader.read_u32()
            break
        f = _read_field(reader)
        if f.magic == ENCRYPT_MAGIC:
            raise ValueError("Encrypted block entries are not supported")

    return method, uncompressed_size, compressed_size, expected_crc


def _entry_to_dict(entry: FileEntryWeb) -> dict:
    result: dict = {
        "path": entry.path,
        "size": entry.expected_size,
        "isDirectory": entry.is_directory,
        "method": _METHOD_NAMES.get(entry.method, str(entry.method)),
    }
    if not entry.is_directory and entry.expected_size <= DATA_INLINE_LIMIT:
        result["data"] = base64.b64encode(bytes(entry.buffer)).decode("ascii")
    return result


def extract_egg_bytes(
    stream: bytes,
    detected_split: bool = False,
) -> tuple[list[dict], bool, bool]:
    """
    Extract an EGG archive from raw bytes.

    Returns:
        (files, is_split, is_solid)
        where `files` is a list of dicts:
            {path, size, isDirectory, method, data?}
        `data` (base64) is present only when size <= 512 KB.
    """
    reader = ByteReader(stream)
    header_split, is_solid = _parse_egg_header(reader)
    is_split = detected_split or header_split

    id_to_path: dict[int, str] = {}
    pending: list[FileEntryWeb] = []
    results: list[dict] = []
    current: FileEntryWeb | None = None

    while reader.remaining() > 0:
        magic = reader.peek_u32()
        if magic is None:
            break
        if magic == END_MAGIC:
            reader.read_u32()
            continue

        if magic == FILE_MAGIC:
            entry = _parse_file_header(reader, id_to_path)
            pending.append(entry)
            if entry.expected_size == 0 or entry.is_directory:
                results.append(_entry_to_dict(entry))
            if not is_solid and not entry.is_directory and entry.expected_size > 0:
                current = entry
            continue

        if magic == BLOCK_MAGIC:
            method, uncompressed_size, compressed_size, expected_crc = _parse_block_header(reader)
            compressed = reader.read(compressed_size)
            block = _decompress(method, compressed)
            actual_crc = zlib.crc32(block) & 0xFFFFFFFF
            if actual_crc != expected_crc:
                raise ValueError(f"CRC32 mismatch: expected {expected_crc:#x}, got {actual_crc:#x}")
            if len(block) != uncompressed_size:
                raise ValueError(f"Unexpected block size: expected {uncompressed_size}, got {len(block)}")

            if is_solid:
                block_view = memoryview(block)
                consumed = 0
                while consumed < len(block_view):
                    target = next(
                        (e for e in pending if not e.is_directory and not e.complete),
                        None,
                    )
                    if target is None:
                        raise ValueError("Solid block contains data for an unknown file entry")
                    need = target.expected_size - len(target.buffer)
                    chunk = block_view[consumed: consumed + need]
                    target.buffer.extend(chunk)
                    target.method = method
                    consumed += len(chunk)
                    if target.complete:
                        results.append(_entry_to_dict(target))
                continue

            if current is None:
                current = next(
                    (e for e in reversed(pending) if not e.is_directory and not e.complete),
                    None,
                )
            if current is None:
                raise ValueError("Found a block without a file entry")
            current.buffer.extend(block)
            current.method = method
            if len(current.buffer) > current.expected_size:
                raise ValueError(f"{current.path!r} grew larger than its declared file size")
            if current.complete:
                results.append(_entry_to_dict(current))
                current = None
            continue

        if magic in (COMMENT_MAGIC, DUMMY_MAGIC, SKIP_MAGIC):
            _read_field(reader)
            continue

        raise ValueError(f"Unknown record magic {magic:#x} at offset {reader.tell()}")

    unfinished = [e.path for e in pending if not e.is_directory and not e.complete]
    if unfinished:
        raise ValueError(f"Archive ended before finishing: {', '.join(unfinished)}")

    return results, is_split, is_solid
