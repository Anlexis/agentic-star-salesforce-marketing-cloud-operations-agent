"""Input- and output-safety helpers for CMN-C2-232.

Pure, stateless domain helpers (NOT framework gate methods). They implement the
part of the template's security guarantees that the template itself owns:

* ``sanitize_query``      - strip HTML markup and control characters, collapse
                            whitespace, and cap length before the caller's text
                            is serialized for the workflow;
* ``find_injection``      - deterministic refusal of prompt-injection request
                            forms, enforced by the node that owns the caller
                            contract rather than relying on any surrounding
                            framework gate being active;
* ``sanitize_display_name`` - reduce a caller-supplied campaign display name to
                            an inert display alphabet, because that value is
                            rendered back into the external response;
* ``contains_credential_like`` / ``iter_nested_strings`` / ``safe_error_lines``
                          - the output-side rules: one definition of what a
                            credential-shaped string looks like, a recursive
                            walk over any nested response value, and the
                            reduction of internal error entries to a form that
                            is safe to hand back to a caller.

Keeping these here (rather than inline in the node) makes each rule testable on
its own and keeps one definition per rule.
"""

from __future__ import annotations

import re
from typing import Any, Iterator

_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Control characters except tab and newline; they have no place in a request and
# would otherwise ride into logs and the rendered confirmation.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"[ \t\r\f\v]+")

DEFAULT_MAX_LENGTH = 4000

# Deterministic prompt-injection request forms. Deliberately narrow: each
# alternative needs an *instruction-overriding* phrase, not merely a word that
# also occurs in ordinary marketing copy ("update the campaign", "send the
# instructions to the team", a campaign literally named "Ignore Previous") must
# stay unaffected - that is asserted in both directions by the tests.
_INJECTION_RE = re.compile(
    r"(?:ignore|disregard|forget|override|bypass)\s+(?:all\s+|any\s+|the\s+)*"
    r"(?:previous|prior|preceding|earlier|above|prior-?stated|your)\s+"
    r"(?:\w+\s+){0,2}?(?:instruction|instructions|rule|rules|prompt|prompts|direction|directions)"
    r"|(?:system|developer)\s+prompt"
    r"|(?:reveal|show|print|repeat|output|disclose|dump)\s+(?:me\s+)?(?:your|the)\s+"
    r"(?:\w+\s+){0,2}?(?:prompt|instructions|system\s+message|configuration)"
    r"|you\s+are\s+now\s+(?:a|an|the)\s"
    r"|developer\s+mode"
    r"|jailbreak"
    r"|<\|\s*(?:im_start|im_end|system|endoftext)\s*\|>"
    r"|\[\s*(?:system|instructions?)\s*\]",
    re.IGNORECASE,
)

# Campaign display names are rendered back to the caller, so they are reduced to
# an inert alphabet: word characters (including Japanese), spaces, and a small
# punctuation set. Everything else - markup, quotes, colons, at-signs, newlines,
# control characters - is dropped rather than escaped. Square brackets are kept
# so a framework mask marker stays legible as a marker instead of collapsing
# into something that reads like a real name.
_DISPLAY_ALLOWED_RE = re.compile(r"[^\w \-.,&'()/#\[\]]+", re.UNICODE)
DISPLAY_NAME_MAX_LENGTH = 100


def sanitize_query(query: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Strip HTML tags and control characters, normalise spacing, cap length."""
    cleaned = _HTML_TAG_RE.sub("", query)
    cleaned = _CONTROL_CHARS_RE.sub("", cleaned)
    cleaned = _WS_RE.sub(" ", cleaned)
    return cleaned[:max_length]


def find_injection(text: str) -> bool:
    """True when the text carries a recognised instruction-override form.

    The caller refuses the request outright; nothing derived from the text is
    carried forward. The matched span is deliberately NOT returned - rejected
    caller content must not round-trip into an error message or a log line.
    """
    return bool(text) and _INJECTION_RE.search(text) is not None


def sanitize_display_name(name: str, max_length: int = DISPLAY_NAME_MAX_LENGTH) -> str:
    """Reduce a caller-supplied campaign display name to an inert display form.

    Applied to every name that can reach the external response. Characters
    outside the allowed alphabet are dropped, runs of whitespace collapse to a
    single space, and the result is length-capped.
    """
    cleaned = _DISPLAY_ALLOWED_RE.sub(" ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_length]


# ── Output-side rules ────────────────────────────────────────────────────────

# Credential-shaped strings that must never reach the caller. Thresholds are
# deliberately at least as sensitive as the inbound redaction in
# ValidateInputNode, so nothing the input scan would have caught can be missed
# on the way out.
_CREDENTIAL_LIKE_RE = re.compile(
    r"eyJ[A-Za-z0-9._-]{6,}|sk-[A-Za-z0-9]{6,}|secret_[A-Za-z0-9]{6,}|Bearer\s+[A-Za-z0-9._-]{8,}"
)

# An internal error entry can carry a stack trace (absolute source paths, frame
# lines). Only its first line is caller-safe, and never one that is itself a
# trace frame.
_TRACE_FRAGMENT_RE = re.compile(r'File "|Traceback \(most recent call last\)')
MAX_ERROR_LINE_CHARS = 200
MAX_ERROR_LINES = 5


def iter_nested_strings(value: Any) -> Iterator[str]:
    """Yield every string reachable inside value (dict/list/tuple recursion)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from iter_nested_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from iter_nested_strings(child)


def contains_credential_like(value: Any) -> bool:
    """True when any string reachable inside value looks like a credential."""
    return any(_CREDENTIAL_LIKE_RE.search(text) for text in iter_nested_strings(value))


def safe_error_lines(error_log: Any) -> "list[str]":
    """Reduce internal error entries to the subset that is safe to return.

    Keeps the first line of each entry (the message; the remainder of an entry
    can be a stack trace carrying absolute source paths), drops any line that
    is a trace fragment or carries a credential shape, truncates, de-duplicates
    while preserving order, and caps the count. The messages themselves name a
    field, never a rejected caller value.
    """
    if not isinstance(error_log, (list, tuple)):
        return []
    lines: "list[str]" = []
    for entry in error_log:
        if not isinstance(entry, str) or not entry.strip():
            continue
        first = entry.splitlines()[0].strip()
        if not first or _TRACE_FRAGMENT_RE.search(first) or _CREDENTIAL_LIKE_RE.search(first):
            continue
        first = first[:MAX_ERROR_LINE_CHARS]
        if first not in lines:
            lines.append(first)
        if len(lines) >= MAX_ERROR_LINES:
            break
    return lines
