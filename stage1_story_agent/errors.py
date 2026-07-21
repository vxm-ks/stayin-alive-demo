"""Stable, sanitized domain errors for the Stage 1 planner."""

from __future__ import annotations


class FormValidationError(ValueError):
    """A machine-readable form-planning validation error."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{code} at {path}: {message}")

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


class PlanValidationError(ValueError):
    """One or more cross-object validation failures."""

    def __init__(self, issues: list[dict[str, str]]) -> None:
        self.issues = issues[:20]
        super().__init__("; ".join(item["code"] for item in self.issues))


class Stage1AgentError(RuntimeError):
    """Base class for errors with a stable public code and exit category."""

    category = "internal"
    exit_code = 1

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.public_message = message
        super().__init__(f"{code}: {message}")


class ConfigurationError(Stage1AgentError):
    category = "configuration"
    exit_code = 2


class BackendError(Stage1AgentError):
    category = "backend"
    exit_code = 3


class ModelContentError(Stage1AgentError):
    category = "content"
    exit_code = 4

    def __init__(
        self,
        code: str,
        message: str,
        *,
        issues: list[dict[str, str]] | None = None,
        content_attempts: int = 0,
        network_attempts: int = 0,
    ) -> None:
        self.issues = (issues or [])[:20]
        self.content_attempts = content_attempts
        self.network_attempts = network_attempts
        super().__init__(code, message)


class ArtifactWriteError(Stage1AgentError):
    category = "write"
    exit_code = 5
