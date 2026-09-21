"""Normalized events and explicit delivery failures."""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

Platform = Literal["slack", "zulip"]


@dataclass
class Event:
    """A platform event stripped of transport envelopes and credentials."""

    key: str
    platform: Platform
    kind: str
    message_id: str
    actor: str = ""
    author: str = ""
    text: str = ""
    parent: str = ""
    topic: str = ""
    revision: str = "0"
    emoji: str = ""
    added: bool = True
    reaction_variant: str = ""
    ids: list[str] = field(default_factory=list)
    out_of_scope: bool = False
    attachments: list[dict[str, Any]] | None = None
    text_format: str = ""

    def json(self) -> dict[str, Any]:
        """Return a durable JSON-compatible representation."""
        return asdict(self)


class DeliveryError(Exception):
    """A sanitized error with an explicit retry/uncertainty classification."""

    def __init__(self, code: str, category: str = "failed", retry_after: float = 5) -> None:
        super().__init__(code)
        self.code = code
        self.category = category
        self.retry_after = retry_after


class Transport(Protocol):
    """The engine's only interface to remote platform mutations."""

    def execute(self, platform: Platform, method: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute one operation or raise a classified, sanitized failure."""
        ...
