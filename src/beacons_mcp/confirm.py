"""Let the user pick the protein when a search is ambiguous.

If the client can show a form (MCP elicitation), the user chooses from a short list and
the choice comes back to the model as confirmed. Otherwise, or if the user dismisses
the form, the result is returned as before and the model is told to ask in chat.
"""

import logging
from typing import Any, Literal

from mcp import types
from mcp.server.mcpserver import Context
from pydantic import Field, create_model

from beacons_mcp.models import ProteinCandidate, ProteinSearchResult

logger = logging.getLogger("beacons_mcp")

MAX_CHOICES = 5
MAX_NAME_CHARS = 70

DISMISSED_NOTE = (
    "The user dismissed the picker without choosing. Ask them in chat which of these "
    "they mean before calling find_structures."
)


def choices(candidates: list[ProteinCandidate]) -> list[ProteinCandidate]:
    """The candidates to offer the user, or an empty list when no choice is needed.

    Ambiguous means several reviewed entries, or no reviewed entry and several
    unreviewed ones. One reviewed entry among unreviewed fragments is not ambiguous.
    Only the first few are offered: a long dropdown is no help.
    """
    reviewed = [c for c in candidates if c.reviewed]
    if len(reviewed) > 1:
        return reviewed[:MAX_CHOICES]
    if not reviewed and len(candidates) > 1:
        return candidates[:MAX_CHOICES]
    return []


def label(candidate: ProteinCandidate) -> str:
    """One line for the picker: accession, name, entry name (and a flag if unreviewed)."""
    name = candidate.protein_name or "unnamed protein"
    if len(name) > MAX_NAME_CHARS:
        name = name[: MAX_NAME_CHARS - 3].rstrip() + "..."
    # Plain ASCII: terminals differ in how they show anything fancier.
    text = f"{candidate.accession} - {name} ({candidate.entry_name})"
    return text if candidate.reviewed else f"{text} - unreviewed"


def _client_can_elicit(ctx: Context) -> bool:
    session = ctx.request_context.session
    return session.check_client_capability(
        types.ClientCapabilities(elicitation=types.ElicitationCapability())
    )


async def confirm_candidate(
    ctx: Context, result: ProteinSearchResult
) -> ProteinSearchResult:
    """Ask the user to pick, if that is needed and the client can show a picker."""
    options = choices(result.candidates)
    if not options:
        return result

    supported = _client_can_elicit(ctx)
    logger.info(
        "search is ambiguous (%d options); client supports elicitation: %s",
        len(options),
        supported,
    )
    if not supported:
        return result

    by_label = {label(c): c for c in options}
    schema: Any = create_model(
        "ProteinChoice",
        protein=(
            Literal[tuple(by_label)],  # type: ignore[valid-type]
            Field(description="Which protein do you mean?"),
        ),
    )
    message = (
        f"Several proteins match '{result.query}' in {result.organism}. "
        "Which one do you mean?"
    )
    if result.total_matches and result.total_matches > len(options):
        # The list is UniProt's top few, not everything: say so, and say what to do.
        message += (
            f" Showing {len(options)} of {result.total_matches} matches; "
            "decline if yours is not here."
        )
    try:
        answer = await ctx.elicit(message, schema)
    except Exception as exc:  # noqa: BLE001 - the picker is optional; chat still works
        logger.warning("elicitation failed (%r); the model will ask in chat", exc)
        return result

    if answer.action != "accept":
        logger.info("user %sed the picker", answer.action)
        return result.model_copy(update={"note": DISMISSED_NOTE})

    chosen = by_label[answer.data.protein]
    logger.info("user picked %s", chosen.accession)
    return result.model_copy(
        update={
            "candidates": [chosen],
            "confirmed_by_user": True,
            "note": (
                f"The user chose {chosen.accession} ({chosen.entry_name}) in a dialog. "
                "Use it; do not ask again."
            ),
        }
    )
