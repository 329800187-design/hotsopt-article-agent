"""Search discovery API for the research subsystem."""

from __future__ import annotations

from typing import Any

from research.service import ResearchService


def discover_with_fallback(topic: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Run the primary discoverer and its fallback, returning audit evidence."""
    return ResearchService().discover_with_fallback(topic)


__all__ = ["ResearchService", "discover_with_fallback"]
