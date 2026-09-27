"""
cv_agent.config.settings — Configuration dataclasses and loader.

Config discovery order:
  1. Path explicitly passed to load_config()
  2. Packaged config/default.toml
  3. Built-in defaults (no file needed — tests run without a config file)

Environment variable overrides are intentionally deferred to a later step.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from importlib import resources

if sys.version_info >= (3, 11):
    from importlib.resources.abc import Traversable
else:
    from importlib.abc import Traversable
from importlib.resources import files as resource_files
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib  # stdlib from 3.11+
else:
    import tomli as tomllib  # type: ignore[no-redef]

_RESOURCE_PACKAGE = "cv_agent.resources"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _resource_path(relative_path: str) -> Path:
    """Return the installed filesystem path for a packaged runtime resource."""
    resource = resources.files(_RESOURCE_PACKAGE).joinpath(relative_path)
    return Path(resource)  # type: ignore[arg-type]


_DEFAULT_CONFIG_RESOURCE = resource_files("cv_agent").joinpath("resources/default.toml")
_DEFAULT_REGISTRY_RESOURCE = resource_files("cv_agent").joinpath(
    "resources/spec/capability_registry.json"
)



def _default_config_path() -> Path | Traversable:
    return (
        _DEFAULT_CONFIG_RESOURCE
        if _DEFAULT_CONFIG_RESOURCE.is_file()
        else _REPO_ROOT / "config" / "default.toml"
    )


def _default_registry_path() -> Path | Traversable:
    return (
        _DEFAULT_REGISTRY_RESOURCE
        if _DEFAULT_REGISTRY_RESOURCE.is_file()
        else _REPO_ROOT / "spec" / "capability_registry.json"
    )


@dataclass
class LLMOverride:
    """Provider/model override for a specific named agent role."""

    provider: str
    model: str


@dataclass
class LLMConfig:
    """LLM provider configuration."""

    provider: str = "mock"
    model: str = "fake-1"
    # Keyed by agent role name (e.g. "planner", "critic")
    overrides: dict[str, LLMOverride] = field(default_factory=dict)


@dataclass
class AgentConfig:
    """Top-level agent configuration."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    registry_path: Path = field(
        default_factory=lambda: _resource_path("spec/capability_registry.json")
    )


def load_config(path: Path | Traversable | None = None) -> AgentConfig:
    """
    Load agent configuration from a TOML file.

    Args:
        path: Explicit path to a .toml config file.
              Defaults to packaged config/default.toml.
              If the file does not exist, built-in defaults are returned.

    Returns:
        Populated AgentConfig.
    """
    if path is None:
        path = _resource_path("config/default.toml")

    if not path.is_file():
        return AgentConfig()

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    llm_raw: dict = raw.get("llm", {})
    overrides_raw: dict = llm_raw.get("overrides", {})

    overrides: dict[str, LLMOverride] = {
        role: LLMOverride(
            provider=role_cfg.get("provider", "mock"),
            model=role_cfg.get("model", "fake-1"),
        )
        for role, role_cfg in overrides_raw.items()
    }

    llm = LLMConfig(
        provider=llm_raw.get("provider", "mock"),
        model=llm_raw.get("model", "fake-1"),
        overrides=overrides,
    )

    registry_path_raw: str | None = raw.get("registry_path")
    registry_path = (
        Path(registry_path_raw)
        if registry_path_raw
        else _resource_path("spec/capability_registry.json")
    )

    return AgentConfig(llm=llm, registry_path=registry_path)
