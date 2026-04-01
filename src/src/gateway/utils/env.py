"""Environment variable interpolation for config values.

Supports ``${VAR}`` and ``${VAR:-default}`` syntax in string values.
This is used by the config loader to resolve env-var references in
downstream server definitions (e.g. ``GITHUB_PERSONAL_ACCESS_TOKEN``).
"""

from __future__ import annotations

import os
import re

_ENV_PATTERN = re.compile(
    r"\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)


def interpolate_env(value: str) -> str:
    """Replace ``${VAR}`` and ``${VAR:-default}`` placeholders with env values.

    Raises ``KeyError`` if a variable has no default and is not set.

    >>> import os; os.environ["TEST_VAR"] = "hello"
    >>> interpolate_env("token=${TEST_VAR}")
    'token=hello'
    >>> interpolate_env("port=${UNSET:-8080}")
    'port=8080'
    """

    def _replace(match: re.Match[str]) -> str:
        var = match.group("var")
        default = match.group("default")
        env_val = os.environ.get(var)
        if env_val is not None:
            return env_val
        if default is not None:
            return default
        raise KeyError(
            f"Environment variable '{var}' is not set and no default provided"
        )

    return _ENV_PATTERN.sub(_replace, value)


def interpolate_env_dict(mapping: dict[str, str]) -> dict[str, str]:
    """Interpolate all values in a string→string dict."""
    return {k: interpolate_env(v) for k, v in mapping.items()}
