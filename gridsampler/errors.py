"""Error types that always identify the offending file and variable."""

from __future__ import annotations


class SamplerError(Exception):
    """A user-facing input or merge error.

    ``path`` and ``variable`` are stored separately so the CLI can also put
    them in diagnostics instead of relying on message parsing.
    """

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        variable: str | None = None,
        kind: str = "input_error",
    ) -> None:
        self.message = message
        self.path = path
        self.variable = variable
        self.kind = kind
        super().__init__(self.format())

    def format(self) -> str:
        location: list[str] = []
        if self.path is not None:
            location.append(str(self.path))
        if self.variable is not None:
            location.append(str(self.variable))
        prefix = ":".join(location)
        if prefix:
            return f"{prefix}: {self.message}"
        return self.message


class MergeError(SamplerError):
    """Raised for duplicate timestamps or incompatible spatial metadata."""

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        variable: str | None = None,
    ) -> None:
        super().__init__(message, path=path, variable=variable, kind="merge_error")
