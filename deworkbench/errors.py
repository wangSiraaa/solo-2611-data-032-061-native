"""Exception hierarchy. A ValidationError always blocks the analysis."""


class WorkbenchError(Exception):
    """Base class for workbench errors."""


class ValidationError(WorkbenchError):
    """Input or plan validation failure — analysis must not proceed."""


class DesignError(WorkbenchError):
    """Design matrix is not identifiable / estimable."""


class PlanError(ValidationError):
    """The frozen analysis plan itself is malformed."""
