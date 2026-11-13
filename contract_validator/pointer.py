"""JSON Pointer (RFC 6901) helpers.

Pointers are used both for data locations in diagnostics and for contract
locations.  Names containing ``/`` or ``~`` are escaped as ``~1`` and ``~0``
respectively.  The root pointer is the empty string (canonical) and the
convenience spelling ``/`` is accepted on input.
"""

from __future__ import annotations


ROOT = ""


def escape_token(token: str) -> str:
    """Escape a single reference token (``~`` first, then ``/``)."""
    return token.replace("~", "~0").replace("/", "~1")


def unescape_token(token: str) -> str:
    """Decode a single reference token; raise ValueError on bad escapes."""
    out = []
    i = 0
    n = len(token)
    while i < n:
        ch = token[i]
        if ch == "~":
            if i + 1 >= n:
                raise ValueError("dangling '~' escape in reference token")
            nxt = token[i + 1]
            if nxt == "0":
                out.append("~")
            elif nxt == "1":
                out.append("/")
            else:
                raise ValueError(f"invalid '~{nxt}' escape in reference token")
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse(pointer: str) -> list[str]:
    """Split a JSON Pointer into decoded tokens.

    ``""`` denotes the root.  A leading ``/`` is required for any non-empty
    pointer; every further ``/`` divides tokens, which may be empty strings.
    """
    if not isinstance(pointer, str):
        raise ValueError("JSON Pointer must be a string")
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("non-root JSON Pointer must start with '/'")
    return [unescape_token(tok) for tok in pointer[1:].split("/")]


def render(tokens) -> str:
    """Render decoded tokens back to a canonical JSON Pointer."""
    return "".join("/" + escape_token(tok) for tok in tokens)


def join(base: str, token: str) -> str:
    """Append one escaped token to an already-canonical pointer ``base``."""
    return base + "/" + escape_token(token)


def normalize(pointer: str) -> str:
    """Validate and canonicalise a pointer (accepts ``/`` for the root)."""
    if pointer == "/":
        return ROOT
    return render(parse(pointer))
