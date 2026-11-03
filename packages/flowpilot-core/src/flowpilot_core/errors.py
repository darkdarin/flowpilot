"""Graph errors carry every issue at once so an editor can show them all."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GraphIssue:
    """One problem in a graph. ``code`` is stable for UIs; ``path`` points into graph.json."""

    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message} [{self.code}]"


class GraphError(Exception):
    """A graph failed to parse or validate."""

    def __init__(self, issues: list[GraphIssue]) -> None:
        if not issues:
            raise ValueError("GraphError requires at least one issue")
        super().__init__("\n".join(str(i) for i in issues))
        self.issues = tuple(issues)

    @property
    def codes(self) -> set[str]:
        return {i.code for i in self.issues}
