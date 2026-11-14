"""Prepare a structure file for the viewer: find its real format, compress, chunk.

Neither the Hub's format label nor the provider's Content-Type can be trusted (one
provider labels PDB text as mmCIF; others serve mmCIF as text/plain or PDB as
application/x-pilot), so the format is read from the file's own content.

Tool results are limited to roughly 150,000 characters in Claude, so a file is sent as
gzip + base64 in chunks of ``CHUNK_CHARS``. The chunk size is a multiple of 4, so each
chunk is valid base64 on its own.
"""

import base64
import gzip
import zlib
from dataclasses import dataclass
from typing import Literal

CHUNK_CHARS = 96_000
GZIP_MAGIC = b"\x1f\x8b"

StructureFormat = Literal["mmcif", "pdb"]

# Record names that can start a PDB file (the first six columns, trimmed).
_PDB_RECORDS = frozenset(
    {
        "HEADER", "TITLE", "REMARK", "ATOM", "HETATM", "MODEL", "CRYST1", "COMPND",
        "SEQRES", "EXPDTA", "AUTHOR", "SOURCE", "KEYWDS", "REVDAT", "JRNL", "DBREF",
        "HELIX", "SHEET", "TER", "CONECT", "MTRIX1", "ORIGX1", "SCALE1",
    }
)  # fmt: skip


class UnsupportedFormat(Exception):
    """The file is not mmCIF or PDB text."""


class TooLarge(Exception):
    """The file is larger than the limit once decompressed."""


@dataclass(frozen=True)
class PreparedFile:
    format: StructureFormat
    decoded_bytes: int
    chunks: list[str]


def sniff_format(data: bytes) -> StructureFormat:
    """The format of structure text, from its first meaningful line."""
    head = data[:4096].decode("utf-8", "replace").lstrip("﻿")
    for line in head.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("data_"):
            return "mmcif"
        if line[:6].strip() in _PDB_RECORDS:
            return "pdb"
        shown = line[:40]
        raise UnsupportedFormat(f"it starts with {shown!r}")
    raise UnsupportedFormat("it is empty")


def _gunzip_limited(data: bytes, max_bytes: int) -> bytes:
    decoder = zlib.decompressobj(wbits=31)
    try:
        out = decoder.decompress(data, max_bytes + 1)
    except zlib.error as exc:
        raise UnsupportedFormat("it is not valid gzip data") from exc
    if len(out) > max_bytes:
        raise TooLarge
    return out


def prepare(data: bytes, max_bytes: int) -> PreparedFile:
    """Sniff the format, then gzip and base64 ``data`` into chunks.

    A file the provider already gzipped is unpacked first (within the same limit).
    """
    if data[:2] == GZIP_MAGIC:
        data = _gunzip_limited(data, max_bytes)
    fmt = sniff_format(data)
    encoded = base64.b64encode(gzip.compress(data, compresslevel=6)).decode("ascii")
    chunks = [encoded[i : i + CHUNK_CHARS] for i in range(0, len(encoded), CHUNK_CHARS)]
    return PreparedFile(format=fmt, decoded_bytes=len(data), chunks=chunks)
