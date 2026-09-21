"""Non-secret configuration; credentials are supplied separately."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Config:
    """An explicit channel pair. Identifiers, not names, define the boundary."""

    slack_team: str
    slack_channel: str
    zulip_site: str
    zulip_channel: int
    database: Path = Path("bridge.sqlite")
    feed: str = "Slack feed"
    storage_backend: str = "sqlite"

    @classmethod
    def load(cls, path: Path) -> "Config":
        """Load configuration without embedding or displaying credentials."""
        data = tomllib.loads(path.read_text())
        cfg = cls(
            slack_team=data["slack"]["team_id"],
            slack_channel=data["slack"]["channel_id"],
            zulip_site=data["zulip"]["site"].rstrip("/"),
            zulip_channel=int(data["zulip"]["channel_id"]),
            database=path.parent / data.get("database", "bridge.sqlite"),
            storage_backend=data.get("storage_backend", "sqlite"),
            feed=data.get("feed_topic", "Slack feed"),
        )
        if cfg.storage_backend not in {"sqlite", "turso"}:
            raise ValueError("storage_backend must be sqlite or turso")
        if urlparse(cfg.zulip_site).scheme != "https":
            raise ValueError(
                "Zulip site must use HTTPS, including a trusted local test certificate"
            )
        if not cfg.slack_team.startswith("T") or not cfg.slack_channel.startswith(("C", "G")):
            raise ValueError("Use explicit Slack team and channel IDs")
        if cfg.zulip_channel <= 0 or not cfg.feed or len(cfg.feed) > 60:
            raise ValueError("Use a positive Zulip channel ID and a feed title of 1–60 characters")
        return cfg

    def identity(self) -> dict[str, str | int]:
        """Bind persistent state to its original pair and reserved feed."""
        return {
            "slack_team": self.slack_team,
            "slack_channel": self.slack_channel,
            "zulip_site": self.zulip_site,
            "zulip_channel": self.zulip_channel,
            "feed": self.feed,
        }


def secret(name: str) -> str:
    """Read a required environment credential without including its value in errors."""
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"Missing environment variable: {name}")
    return value
