"""Environment-backed configuration without persisting secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from .errors import ConfigurationError


DOTENV_KEYS = frozenset(
    {
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "STAGE1_MAX_OUTPUT_TOKENS",
        "STAGE1_TIMEOUT_SECONDS",
        "STAGE1_THINKING",
        "STAGE1_REASONING_EFFORT",
        "STAGE1_MAX_CONTENT_ATTEMPTS",
        "STAGE1_MAX_NETWORK_ATTEMPTS",
    }
)


def default_env_file() -> Path:
    """Return the project-root .env path without creating it."""

    configured = os.getenv("STAGE1_ENV_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[1] / ".env"


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read only Stage 1 keys from a small, non-executable dotenv file."""

    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ConfigurationError("DOTENV_READ_FAILED", f"could not read local environment file: {path}") from exc
    values: dict[str, str] = {}
    for line_number, original in enumerate(lines, start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError("DOTENV_LINE_INVALID", f"invalid .env syntax at line {line_number}")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if name in DOTENV_KEYS:
            values[name] = value
    return values


def _get(name: str, dotenv: dict[str, str], default: str | None = None) -> str | None:
    """Process environment always overrides the local .env file."""

    return os.getenv(name, dotenv.get(name, default))


def _positive_int(name: str, default: int, dotenv: dict[str, str]) -> int:
    raw = _get(name, dotenv)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ConfigurationError("CONFIG_INTEGER_INVALID", f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError("CONFIG_INTEGER_INVALID", f"{name} must be positive")
    return value


def _positive_float(name: str, default: float, dotenv: dict[str, str]) -> float:
    raw = _get(name, dotenv)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ConfigurationError("CONFIG_NUMBER_INVALID", f"{name} must be a number") from exc
    if value <= 0:
        raise ConfigurationError("CONFIG_NUMBER_INVALID", f"{name} must be positive")
    return value


@dataclass(frozen=True)
class Stage1Config:
    api_key: SecretStr | None = None
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-pro"
    max_output_tokens: int = 8192
    timeout_seconds: float = 90.0
    thinking: str = "enabled"
    reasoning_effort: str = "high"
    max_content_attempts: int = 3
    max_network_attempts: int = 3

    @classmethod
    def from_env(
        cls,
        *,
        require_api_key: bool = True,
        env_file: str | Path | None = None,
    ) -> "Stage1Config":
        dotenv = _read_dotenv(Path(env_file).expanduser() if env_file is not None else default_env_file())
        raw_key = _get("DEEPSEEK_API_KEY", dotenv)
        if require_api_key and not raw_key:
            raise ConfigurationError("DEEPSEEK_API_KEY_MISSING", "DEEPSEEK_API_KEY is required")
        thinking = _get("STAGE1_THINKING", dotenv, "enabled")
        if thinking not in {"enabled", "disabled"}:
            raise ConfigurationError("CONFIG_THINKING_INVALID", "STAGE1_THINKING must be enabled or disabled")
        effort = _get("STAGE1_REASONING_EFFORT", dotenv, "high")
        if effort not in {"high", "max"}:
            raise ConfigurationError("CONFIG_REASONING_EFFORT_INVALID", "STAGE1_REASONING_EFFORT must be high or max")
        return cls(
            api_key=SecretStr(raw_key) if raw_key else None,
            base_url=(_get("DEEPSEEK_BASE_URL", dotenv, "https://api.deepseek.com") or "").rstrip("/"),
            model=_get("DEEPSEEK_MODEL", dotenv, "deepseek-v4-pro") or "",
            max_output_tokens=_positive_int("STAGE1_MAX_OUTPUT_TOKENS", 8192, dotenv),
            timeout_seconds=_positive_float("STAGE1_TIMEOUT_SECONDS", 90.0, dotenv),
            thinking=thinking,
            reasoning_effort=effort,
            max_content_attempts=_positive_int("STAGE1_MAX_CONTENT_ATTEMPTS", 3, dotenv),
            max_network_attempts=_positive_int("STAGE1_MAX_NETWORK_ATTEMPTS", 3, dotenv),
        )
