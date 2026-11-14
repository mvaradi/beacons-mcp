"""Open one model in the in-chat 3D viewer (an MCP App), and feed it the file.

Two tools use this module. ``show_structure`` is for the model: it re-resolves the
model through the Hub (a model-supplied URL is never trusted) and returns a text
summary plus a short-lived token. ``fetch_structure_file`` is for the viewer only: the
app trades the token for the file, in compressed chunks, and this server does the
download. The viewer's page therefore needs no outside website at all.
"""

import asyncio
import logging
import os
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
from mcp import types
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.server.mcpserver.resources import Resource

from beacons_mcp import hub, safe_fetch, structure_file
from beacons_mcp.models import ModelSummary

logger = logging.getLogger("beacons_mcp")

VIEWER_URI = "ui://beacons/viewer"
DEFAULT_MAX_FILE_MB = 8.0
MAX_MODEL_ID_CHARS = 200
TOKEN_TTL_SECONDS = 15 * 60
MAX_TOKENS = 16
HTML_OVERRIDE_ENV = "BEACONS_MCP_VIEWER_HTML"
MAX_FILE_ENV = "BEACONS_MCP_MAX_FILE_MB"

NOT_BUILT = (
    "The 3D viewer has not been built. Run `npm install` and `npm run build` in the "
    "viewer/ folder (see the README)."
)


# ---- limits and the built page ---------------------------------------------------


def max_file_bytes() -> int:
    """The file size limit (decoded), from BEACONS_MCP_MAX_FILE_MB or the default."""
    raw = os.environ.get(MAX_FILE_ENV)
    if raw:
        try:
            megabytes = float(raw)
            if megabytes > 0:
                return int(megabytes * 1048576)
        except ValueError:
            pass
        logger.warning("ignoring invalid %s=%r", MAX_FILE_ENV, raw)
    return int(DEFAULT_MAX_FILE_MB * 1048576)


def viewer_html_path() -> Path:
    """Where the built viewer page is (an env override exists for tests and custom builds)."""
    override = os.environ.get(HTML_OVERRIDE_ENV)
    if override:
        return Path(override)
    return Path(str(resources.files("beacons_mcp") / "ui" / "viewer.html"))


def viewer_available() -> bool:
    return viewer_html_path().is_file()


class ViewerResource(Resource):
    """The viewer's HTML, read from disk when the host asks for it (not at import)."""

    async def read(self) -> str:
        path = viewer_html_path()
        try:
            return await asyncio.to_thread(path.read_text, encoding="utf-8")
        except FileNotFoundError:
            raise ResourceError(NOT_BUILT) from None


# ---- tokens ----------------------------------------------------------------------


@dataclass
class ViewerEntry:
    """What a token stands for: one Hub-resolved model, and its prepared file."""

    url: str
    model: ModelSummary
    expires: float
    prepared: structure_file.PreparedFile | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ViewerStore:
    """Short-lived, in-memory tokens. Lives for one server run."""

    def __init__(
        self,
        *,
        ttl: float = TOKEN_TTL_SECONDS,
        max_entries: int = MAX_TOKENS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl, self._max, self._clock = ttl, max_entries, clock
        self._entries: OrderedDict[str, ViewerEntry] = OrderedDict()

    def issue(self, url: str, model: ModelSummary) -> str:
        now = self._clock()
        for token in [t for t, e in self._entries.items() if e.expires <= now]:
            del self._entries[token]
        while len(self._entries) >= self._max:
            self._entries.popitem(last=False)  # drop the oldest
        token = secrets.token_urlsafe(18)
        self._entries[token] = ViewerEntry(
            url=url, model=model, expires=now + self._ttl
        )
        return token

    def get(self, token: str) -> ViewerEntry:
        entry = self._entries.get(token)
        if entry is None or entry.expires <= self._clock():
            self._entries.pop(token, None)
            raise ToolError(
                "That viewer link is not valid any more. Call show_structure again to "
                "reopen the model."
            )
        return entry


# ---- show_structure --------------------------------------------------------------


def _pick_model(
    summary: dict[str, Any], accession: str, model_id: str
) -> dict[str, Any]:
    matches = [
        item["summary"]
        for item in summary.get("structures", [])
        if str(item["summary"].get("model_identifier")) == model_id
    ]
    if not matches:
        raise ToolError(
            f"The 3D-Beacons Hub lists no model '{model_id}' for {accession}. Call "
            "find_structures first and pass a model_id exactly as it shows it."
        )
    if len({(m.get("provider"), m.get("model_url")) for m in matches}) > 1:
        logger.warning("model id %r is listed by more than one provider", model_id)
    return matches[0]


def _summary_text(
    entry_name: str | None, accession: str, model: ModelSummary, viewer: bool
) -> str:
    subject = entry_name or accession
    span = (
        f", {model.span_percent}% of the protein."
        if model.span_percent is not None
        else "."
    )
    lines = [
        f"Model {model.model_id} from {model.provider} ({model.category}) for {subject}.",
        f"Residues {model.residues}{span}",
    ]
    if model.resolution_angstrom is not None:
        lines.append(
            f"Resolution {model.resolution_angstrom} Å ({model.experimental_method})."
        )
    if model.confidence_score is not None:
        lines.append(f"Confidence ({model.confidence_type}): {model.confidence_score}.")
    lines.append(
        f"File: {model.model_url} (format as the Hub reports it: {model.file_format})."
    )
    if model.note:
        lines.append(model.note)
    if model.page_url:
        lines.append(f"Page: {model.page_url}")
    lines.append(
        "An interactive 3D viewer opens in chat apps that support it."
        if viewer
        else NOT_BUILT + " Until then, open the file URL in a molecular viewer."
    )
    return "\n".join(lines)


async def show_structure(
    client: httpx.AsyncClient,
    store: ViewerStore,
    uniprot_accession: str,
    model_id: str,
) -> types.CallToolResult:
    accession = hub.normalise_accession(uniprot_accession)
    model_id = model_id.strip()
    if not model_id or len(model_id) > MAX_MODEL_ID_CHARS:
        raise ToolError(
            "model_id is empty or too long. Pass a model_id exactly as find_structures "
            "shows it, for example 'AF-P69905-F1'."
        )

    summary = await hub.fetch_summary(client, accession)
    raw = _pick_model(summary, accession, model_id)
    entry = summary.get("uniprot_entry", {})
    model = hub.model_summary(raw, entry.get("sequence_length"))
    if not model.model_url:
        raise ToolError(
            f"The Hub gives no file URL for model '{model_id}', so it cannot be shown."
        )

    viewer = viewer_available()
    token = store.issue(model.model_url, model)
    logger.info("show_structure %s %s: viewer built: %s", accession, model_id, viewer)
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=_summary_text(entry.get("id"), accession, model, viewer),
            )
        ],
        structured_content={
            "token": token,
            "accession": accession,
            "model_id": model.model_id,
            "provider": model.provider,
            "category": model.category,
            "residues": model.residues,
            "span_percent": model.span_percent,
            "conformers": model.conformers,
            "is_sample": model.conformers is not None,
            "file_url": model.model_url,
            "page_url": model.page_url,
        },
    )


# ---- fetch_structure_file --------------------------------------------------------


async def fetch_structure_file(
    client: httpx.AsyncClient, store: ViewerStore, token: str, chunk: int
) -> types.CallToolResult:
    entry = store.get(token)
    async with entry.lock:  # two chunk-0 requests must not download the file twice
        if entry.prepared is None:
            limit = max_file_bytes()
            data = await safe_fetch.fetch_limited(client, entry.url, limit)
            try:
                entry.prepared = structure_file.prepare(data, limit)
            except structure_file.TooLarge:
                raise ToolError(
                    f"The file is larger than the {limit / 1048576:g} MB limit once "
                    f"unpacked. Download it from {entry.url}."
                ) from None
            except structure_file.UnsupportedFormat as exc:
                raise ToolError(
                    "The viewer shows mmCIF and PDB files, but this file is not one "
                    f"({exc}). Download it from {entry.url} instead."
                ) from None
            logger.info(
                "prepared %s: %d bytes, %d chunk(s)",
                entry.model.model_id,
                entry.prepared.decoded_bytes,
                len(entry.prepared.chunks),
            )
    prepared = entry.prepared
    if not 0 <= chunk < len(prepared.chunks):
        raise ToolError(
            f"Chunk {chunk} does not exist; the file has {len(prepared.chunks)}."
        )
    return types.CallToolResult(
        # Small on purpose: the payload is for the viewer, not for the model.
        content=[
            types.TextContent(
                type="text",
                text=f"Structure file chunk {chunk + 1} of {len(prepared.chunks)}.",
            )
        ],
        structured_content={
            "chunk": chunk,
            "chunks": len(prepared.chunks),
            "format": prepared.format,
            "encoding": "gzip+base64",
            "decoded_bytes": prepared.decoded_bytes,
            "is_sample": entry.model.conformers is not None,
            "conformers": entry.model.conformers,
            "data": prepared.chunks[chunk],
        },
    )
