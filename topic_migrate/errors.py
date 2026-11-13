"""Domain errors with stable CLI exit codes."""

from __future__ import annotations


class TopicMigrateError(Exception):
    """Base class for expected, user-facing failures."""

    exit_code = 2


class InputError(TopicMigrateError):
    """Malformed or inconsistent input."""


class InputConflictError(InputError):
    """A stable identity appears twice with conflicting content."""


class ModelError(TopicMigrateError):
    """The requested model cannot be trained or loaded."""


class NamesError(TopicMigrateError):
    """A names file cannot be resolved against the model."""


class CompareError(TopicMigrateError):
    """The two periods cannot be compared as requested."""


class DecisionError(TopicMigrateError):
    """A decision file is incomplete, invalid, or not explicit."""

    exit_code = 4


class BindingError(DecisionError):
    """Inputs, names, plan, or model summaries changed after review."""

    exit_code = 3


class SourceChangedError(BindingError):
    """A source ticket file no longer matches the trained run."""
