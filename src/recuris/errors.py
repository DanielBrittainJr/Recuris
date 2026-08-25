"""Framework exceptions."""

from __future__ import annotations


class RecurisError(Exception):
    """Base class."""


class WritePermissionError(RecurisError):
    """A ledger field was written by a channel that has no write permission.

    Invariant 1 (hybrid grounding discipline): execution/feasibility state is
    written ONLY by the harness/oracle from objective evidence; the model can
    never mark its own work done. Raising here (instead of logging) makes the
    violation impossible to ship silently.
    """


class ProfileError(RecurisError):
    """Malformed profile.yaml / EM entry / plugin."""
