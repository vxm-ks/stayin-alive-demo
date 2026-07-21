"""Domain errors emitted by the scaffold compiler."""

from __future__ import annotations


class ScaffoldValidationError(ValueError):
    """A stable, path-addressed validation failure."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{code} at {path}: {message}")

