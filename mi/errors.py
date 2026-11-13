"""Error types for pre-run diagnostics."""


class DiagnosticError(Exception):
    """Raised when pre-run diagnostics or run-time checks fail.

    Carries the full list of human-readable findings so that nothing is
    silently dropped or coerced by a downstream library.
    """

    def __init__(self, findings):
        self.findings = [str(f) for f in findings]
        super().__init__("; ".join(self.findings))
