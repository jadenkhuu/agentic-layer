"""Per-client config loaded by `--client <name>`.

A client config carries stack info, conventions, and do/do-not lists. The
runner converts it to a system-prompt prefix and prepends it to every
agent's prompt.

Lookup order:
  <target-repo>/.agentic/clients/<name>.yaml
  <target-repo>/clients/<name>.yaml
  <package-root>/clients/<name>.yaml   (bundled examples like 'purpl')
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ClientConfig(BaseModel):
    name: str
    stack: str = ""
    conventions: list[str] = Field(default_factory=list)
    do: list[str] = Field(default_factory=list)
    do_not: list[str] = Field(default_factory=list)
    extra: str = ""

    def as_system_prefix(self) -> str:
        """Multi-line block prepended to every agent prompt. Trailing
        blank line separates it from the agent's own prompt.
        """
        lines = [f"## client context — {self.name}"]
        if self.stack:
            lines.append(f"stack: {self.stack}")
        if self.conventions:
            lines.append("conventions:")
            lines.extend(f"  - {c}" for c in self.conventions)
        if self.do:
            lines.append("do:")
            lines.extend(f"  - {d}" for d in self.do)
        if self.do_not:
            lines.append("do_not:")
            lines.extend(f"  - {d}" for d in self.do_not)
        if self.extra:
            lines.append("")
            lines.append(self.extra.strip())
        return "\n".join(lines) + "\n\n"

    @classmethod
    def load(cls, path: Path) -> "ClientConfig":
        raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"client config {path}: must be a YAML mapping")
        raw.setdefault("name", Path(path).stem)
        return cls.model_validate(raw)


def find_client_config(name: str, search_roots: list[Path]) -> Path | None:
    for root in search_roots:
        for sub in (".agentic/clients", "clients"):
            for ext in (".yaml", ".yml"):
                p = root / sub / f"{name}{ext}"
                if p.exists():
                    return p
    return None


def load_client(name: str, search_roots: list[Path]) -> ClientConfig:
    p = find_client_config(name, search_roots)
    if not p:
        searched = ", ".join(str(r / "clients") for r in search_roots) + " (and .agentic/clients)"
        raise FileNotFoundError(
            f"client config not found: {name!r}. searched: {searched}"
        )
    return ClientConfig.load(p)
