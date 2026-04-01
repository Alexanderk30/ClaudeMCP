"""Env-var interpolation: supports ${VAR} and ${VAR:-default} in strings."""

from __future__ import annotations

import os
import re

_ENV_RE = re.compile(
    r"\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)


def interpolate_env(value: str) -> str:
    """Replace ${VAR} / ${VAR:-default} placeholders with env values.

    Raises KeyError if a var has no default and isn't set.

    >>> import os; os.environ["TEST_VAR"] = "hello"
    >>> interpolate_env("token=${TEST_VAR}")
    'token=hello'
    >>> interpolate_env("port=${UNSET:-8080}")
    'port=8080'
    """
    def _sub(m: re.Match[str]) -> str:
        var = m.group("var")
        default = m.group("default")
        val = os.environ.get(var)
        if val is not None:
            return val
        if default is not None:
            return default
        raise KeyError(f"Env var '{var}' is not set and has no default")

    return _ENV_RE.sub(_sub, value)


def interpolate_env_dict(mapping: dict[str, str]) -> dict[str, str]:
    return {k: interpolate_env(v) for k, v in mapping.items()}
