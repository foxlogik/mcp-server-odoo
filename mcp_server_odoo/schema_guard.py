"""Local validation of model, field and domain arguments before the RPC.

Every check here runs against data the server already has cached — ``fields_get``
(1h TTL, see ``performance.py``) and the enabled-model list — so a call costs no
extra round trip.

The point is not to stop bad calls; Odoo already does that. The point is *what
comes back*. Odoo answers a mistyped field with a forty-frame XML-RPC traceback
whose last line names the field and nothing else, and a two-element domain leaf
with ``ValueError: not enough values to unpack``. Neither says what the caller
should have written, so a caller that guessed once guesses again. Every message
raised here names the offending value and the nearest real alternative.

Repair versus reject
--------------------
Two malformed shapes are repaired silently because the intent is unambiguous:
a bare operator wrapped in a list (``["|"]``) and HTML-escaped comparison
operators (``&lt;=``), which arrive when a domain has been round-tripped through
markup. Everything else is rejected. In particular a two-element leaf is NOT
repaired: ``["res_id", 762]`` could mean ``=`` or ``in``, and guessing on the
caller's behalf is how the wrong records get written.
"""

import difflib
import html
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .logging_config import get_logger

logger = get_logger(__name__)

# Odoo domain operators. Prefix operators join leaves; the rest compare within one.
PREFIX_OPERATORS = {"&", "|", "!"}
LEAF_OPERATORS = {
    "=",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
    "=?",
    "=like",
    "=ilike",
    "like",
    "not like",
    "ilike",
    "not ilike",
    "in",
    "not in",
    "child_of",
    "parent_of",
    "any",
    "not any",
}

# Fields every model answers to, whether or not fields_get lists them.
ALWAYS_VALID_FIELDS = {"id", "__all__"}

_MAX_SUGGESTIONS = 3
_MIN_SIMILARITY = 0.55


class SchemaGuardError(ValueError):
    """A call rejected locally, carrying a message that names the fix."""


def suggest(name: str, candidates: Iterable[str], limit: int = _MAX_SUGGESTIONS) -> List[str]:
    """Closest candidates to *name*, best first.

    ``difflib`` alone misses the case that matters most here — a caller who wrote
    the label instead of the technical name, or dropped a common prefix, so
    ``date`` should reach ``report_date``. Substring containment is checked first
    for exactly that, then similarity fills the remaining slots.
    """
    if not name:
        return []
    candidates = list(candidates)
    lowered = name.lower()

    contained = [c for c in candidates if lowered in c.lower() or c.lower() in lowered]
    contained.sort(key=lambda c: (abs(len(c) - len(name)), c))

    similar = difflib.get_close_matches(name, candidates, n=limit * 2, cutoff=_MIN_SIMILARITY)

    ordered: List[str] = []
    for candidate in contained + similar:
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered[:limit]


def describe_field(field_name: str, field_defs: Dict[str, Any]) -> str:
    """``report_date (Date)`` — the label is what the caller was looking at."""
    definition = field_defs.get(field_name) or {}
    label = definition.get("string")
    return f"{field_name} ({label})" if label else field_name


def suggest_fields(
    name: str, field_defs: Dict[str, Any], limit: int = _MAX_SUGGESTIONS
) -> List[str]:
    """Closest field names to *name*, label matches first.

    The label is ranked above the technical name on purpose. The two most common
    wrong field names in production traffic were ``date`` on a model whose field
    is ``report_date`` labelled "Date", and ``name`` on a model whose only char
    field is ``title``. Both were read off a form, so the label is the strongest
    signal available about what the caller actually meant.
    """
    if not name or not field_defs:
        return []
    lowered = name.lower().replace("_", " ")

    by_label = [
        field
        for field, definition in field_defs.items()
        if isinstance(definition, dict)
        and isinstance(definition.get("string"), str)
        and definition["string"].lower() == lowered
    ]
    by_label.sort()

    ordered = list(by_label)
    for candidate in suggest(name, field_defs.keys(), limit=limit):
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered[:limit]


# --------------------------------------------------------------------------- #
# Domain                                                                        #
# --------------------------------------------------------------------------- #


def _unescape(value: Any) -> Any:
    if isinstance(value, str) and "&" in value:
        return html.unescape(value)
    return value


def normalize_domain(domain: Any, model: str = "") -> List[Any]:
    """Return *domain* as a valid Odoo domain, or raise naming the bad element.

    Repairs ``["|"]`` to ``"|"`` and unescapes HTML entities in operators.
    Rejects any leaf that is not exactly ``[field, operator, value]``.
    """
    if domain is None:
        return []
    if not isinstance(domain, (list, tuple)):
        raise SchemaGuardError(
            f"Domain must be a list of leaves, got {type(domain).__name__}. "
            f'Example: [["name", "=", "Acme"]]'
        )

    where = f" on {model}" if model else ""
    normalized: List[Any] = []

    for position, element in enumerate(domain):
        # A bare prefix operator, with or without a list around it.
        if isinstance(element, str):
            operator = _unescape(element)
            if operator in PREFIX_OPERATORS:
                normalized.append(operator)
                continue
            raise SchemaGuardError(
                f"Domain element {position}{where} is the string {element!r}, which is not a "
                f"domain operator. Only {sorted(PREFIX_OPERATORS)} may appear on their own; "
                f"everything else must be a 3-element leaf."
            )

        if not isinstance(element, (list, tuple)):
            raise SchemaGuardError(
                f"Domain element {position}{where} is {type(element).__name__}; expected a "
                f"3-element leaf [field, operator, value] or one of {sorted(PREFIX_OPERATORS)}."
            )

        element = list(element)

        # ["|"] — unambiguous, repair it.
        if len(element) == 1 and isinstance(element[0], str):
            operator = _unescape(element[0])
            if operator in PREFIX_OPERATORS:
                logger.debug("Repaired wrapped domain operator %r at position %d", operator, position)
                normalized.append(operator)
                continue

        if len(element) != 3:
            raise SchemaGuardError(
                f"Domain leaf at position {position}{where} has {len(element)} elements, not 3: "
                f"{json_compact(element)}. A leaf is [field, operator, value]"
                + (
                    f' — did you mean ["{element[0]}", "=", {json_compact(element[1])}]?'
                    if len(element) == 2 and isinstance(element[0], str)
                    else "."
                )
            )

        field, operator, value = element
        operator = _unescape(operator)

        if not isinstance(field, str) or not field:
            raise SchemaGuardError(
                f"Domain leaf at position {position}{where} has a non-string field name: "
                f"{json_compact(element)}."
            )
        if not isinstance(operator, str) or operator not in LEAF_OPERATORS:
            raise SchemaGuardError(
                f"Domain leaf at position {position}{where} uses operator {operator!r}, which is "
                f"not valid. Valid operators: {', '.join(sorted(LEAF_OPERATORS))}."
            )

        normalized.append([field, operator, value])

    return normalized


def json_compact(value: Any) -> str:
    """Short, quote-stable rendering for error text."""
    import json as _json

    try:
        return _json.dumps(value, default=str)
    except (TypeError, ValueError):
        return repr(value)


def domain_field_names(domain: Sequence[Any]) -> List[str]:
    """Field names referenced by a normalized domain, dotted paths kept whole."""
    names = []
    for element in domain:
        if isinstance(element, (list, tuple)) and len(element) == 3 and isinstance(element[0], str):
            names.append(element[0])
    return names


# --------------------------------------------------------------------------- #
# Fields                                                                        #
# --------------------------------------------------------------------------- #


def validate_fields(
    model: str,
    requested: Optional[Sequence[str]],
    field_defs: Optional[Dict[str, Any]],
    context: str = "fields",
) -> None:
    """Raise if any requested field is unknown to *model*.

    A dotted path (``partner_id.name``) is checked on its first segment only —
    the rest resolves on the related model, which is not this function's business.
    Fails open when *field_defs* is empty: an unreadable schema is not evidence
    that the caller is wrong.
    """
    # isinstance, not truthiness: a connection that hands back something other
    # than a mapping (a stub, a partial failure) must not be read as "the caller
    # got it wrong".
    if not requested or not isinstance(field_defs, dict) or not field_defs:
        return

    unknown = []
    for name in requested:
        if not isinstance(name, str) or name in ALWAYS_VALID_FIELDS:
            continue
        root = name.split(".", 1)[0]
        if root not in field_defs:
            unknown.append(name)

    if not unknown:
        return

    lines = []
    for name in unknown:
        matches = suggest_fields(name.split(".", 1)[0], field_defs)
        if matches:
            rendered = ", ".join(describe_field(m, field_defs) for m in matches)
            lines.append(f"'{name}' does not exist on {model}. Closest: {rendered}.")
        else:
            lines.append(f"'{name}' does not exist on {model}.")

    raise SchemaGuardError(
        f"Unknown {context} for {model}: "
        + " ".join(lines)
        + f" Call get_fields('{model}') for the full list."
    )


def validate_model(model: str, known_models: Optional[Iterable[str]]) -> None:
    """Raise if *model* is not among *known_models*, naming the nearest ones."""
    if not known_models or isinstance(known_models, (str, bytes)):
        return
    try:
        known = [m for m in known_models if isinstance(m, str)]
    except TypeError:  # not iterable — advisory only, fail open
        return
    if not known:
        return
    if model in known:
        return
    matches = suggest(model, known)
    hint = f" Closest enabled: {', '.join(matches)}." if matches else ""
    raise SchemaGuardError(
        f"Model '{model}' is not available on this database.{hint} "
        f"Call list_models() to see what is enabled."
    )
