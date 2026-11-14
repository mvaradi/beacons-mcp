import base64
import gzip
import os

import pytest

from beacons_mcp.structure_file import (
    CHUNK_CHARS,
    TooLarge,
    UnsupportedFormat,
    prepare,
    sniff_format,
)

MMCIF = b"data_AF-P69905-F1\n#\nloop_\n_atom_site.id\n1\n"
PDB = (
    b"ATOM      1  N   MET A   1      11.104  13.207   9.000  1.00 90.00           N\n"
)


def unpack(chunks):
    return gzip.decompress(b"".join(base64.b64decode(c, validate=True) for c in chunks))


# ---- what format is it? -----------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (MMCIF, "mmcif"),
        (b"\n\n# a comment\n#\ndata_x\n", "mmcif"),  # leading blanks and comments
        (b"\xef\xbb\xbfdata_x\n", "mmcif"),  # byte-order mark
        (b"data_x\r\n_a 1\r\n", "mmcif"),  # Windows line ends
        (PDB, "pdb"),
        (
            b"MODEL      1\nATOM      1  N   MET A   1\nENDMDL\n",
            "pdb",
        ),  # an ensemble sample
        (b"HEADER    OXYGEN TRANSPORT\nATOM      1\n", "pdb"),
        (b"REMARK 1\n", "pdb"),
        (b"CRYST1   50.000   50.000   50.000  90.00  90.00  90.00 P 1\n", "pdb"),
        (b"HETATM    1  O   HOH A   1\n", "pdb"),
        (b"TITLE     SOMETHING\n", "pdb"),
    ],
)
def test_the_real_format_is_read_from_the_content(data, expected):
    assert sniff_format(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"   \n\n",
        b"# only a comment\n",
        b"<!DOCTYPE html><html>Not found</html>",
        b'{"detail": "not found"}',
        b"<?xml version='1.0'?>",
        b"just some text\n",
        b"\x93BCIF binary cif",  # BinaryCIF is not text
        b"\x00\x01\x02\x03",
    ],
)
def test_other_content_is_rejected(data):
    with pytest.raises(UnsupportedFormat):
        sniff_format(data)


def test_the_label_the_hub_gives_plays_no_part():
    # A file the Hub would label mmCIF but that is PDB text is still sniffed as PDB.
    assert sniff_format(PDB) == "pdb"


# ---- compression and chunks -------------------------------------------------------


def test_a_small_file_is_one_chunk_that_unpacks_to_the_original():
    prepared = prepare(MMCIF, 1_000_000)

    assert prepared.format == "mmcif"
    assert prepared.decoded_bytes == len(MMCIF)
    assert len(prepared.chunks) == 1
    assert unpack(prepared.chunks) == MMCIF


def test_a_big_file_is_split_into_chunks_of_valid_base64():
    # Hard to compress, so the encoded text really needs several chunks.
    body = b"data_big\n" + b"\n".join(
        os.urandom(24).hex().encode() for _ in range(20_000)
    )

    prepared = prepare(body, 10_000_000)

    assert len(prepared.chunks) > 2
    assert all(len(c) <= CHUNK_CHARS for c in prepared.chunks)
    assert all(len(c) == CHUNK_CHARS for c in prepared.chunks[:-1])
    assert CHUNK_CHARS % 4 == 0  # so every chunk is valid base64 on its own
    for chunk in prepared.chunks:
        base64.b64decode(chunk, validate=True)
    assert unpack(prepared.chunks) == body
    assert prepared.decoded_bytes == len(body)


def test_text_compresses_well():
    text = (
        b"ATOM      1  N   MET A   1      11.104  13.207   9.000  1.00 90.00\n"
    ) * 5000

    prepared = prepare(b"HEADER x\n" + text, 10_000_000)

    assert sum(len(c) for c in prepared.chunks) < len(text) / 10


# ---- files the provider already gzipped, and decompression bombs ------------------


def test_a_gzipped_file_from_the_provider_is_unpacked_first():
    prepared = prepare(gzip.compress(MMCIF), 1_000_000)

    assert prepared.format == "mmcif"
    assert unpack(prepared.chunks) == MMCIF


def test_a_gzipped_file_that_unpacks_over_the_limit_is_refused():
    bomb = gzip.compress(
        b"data_x\n" + b"A" * 5_000_000
    )  # tiny on the wire, huge unpacked

    assert len(bomb) < 10_000
    with pytest.raises(TooLarge):
        prepare(bomb, 1_000_000)


def test_broken_gzip_data_is_rejected():
    with pytest.raises(UnsupportedFormat, match="gzip"):
        prepare(b"\x1f\x8b" + b"definitely not gzip", 1_000_000)


def test_the_limit_applies_to_unpacked_size_exactly():
    body = b"data_x\n" + b"B" * 993  # 1000 bytes

    assert prepare(gzip.compress(body), 1000).decoded_bytes == 1000
    with pytest.raises(TooLarge):
        prepare(gzip.compress(body + b"B"), 1000)


def test_unsupported_content_is_rejected_by_prepare_too():
    with pytest.raises(UnsupportedFormat):
        prepare(b"<html>an error page</html>", 1_000_000)
