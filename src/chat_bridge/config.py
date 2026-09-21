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
    pair_id: str = "default"
    turso_url_env: str = "TURSO_DATABASE_URL"
    turso_token_env: str = "TURSO_AUTH_TOKEN"

    @classmethod
    def load(cls, path: Path) -> "Config":
        """Load configuration without embedding or displaying credentials."""
        data = tomllib.loads(path.read_text())
        if "pairs" in data:
            raise ValueError("Multi-pair configuration requires the multi-pair loader")
        return cls.from_data(data, path.parent)

    @classmethod
    def from_data(cls, data: dict, directory: Path) -> "Config":
        """Validate one pair using an explicit base directory."""
        cfg = cls(
            slack_team=data["slack"]["team_id"],
            slack_channel=data["slack"]["channel_id"],
            zulip_site=data["zulip"]["site"].rstrip("/"),
            zulip_channel=int(data["zulip"]["channel_id"]),
            database=directory / data.get("database", "bridge.sqlite"),
            pair_id=data.get("id", "default"),
            turso_url_env=data.get("turso_url_env", "TURSO_DATABASE_URL"),
            turso_token_env=data.get("turso_token_env", "TURSO_AUTH_TOKEN"),
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


def load_pairs(path: Path) -> list[Config]:
    """Load legacy or multiple one-to-one pairs sharing one workspace and organization."""
    data = tomllib.loads(path.read_text())
    if "pairs" not in data:
        return [Config.from_data(data, path.parent)]
    if not data["pairs"]:
        raise ValueError("Configure at least one channel pair")
    configs = []
    for pair in data["pairs"]:
        name = pair.get("id", "")
        if not name or not all(c.isalnum() or c in "-_" for c in name):
            raise ValueError("Each pair needs a simple unique id")
        configs.append(
            Config.from_data(
                {
                    "id": name,
                    "database": pair.get("database", f"bridge-{name}.sqlite"),
                    "storage_backend": data.get("storage_backend", "sqlite"),
                    "feed_topic": pair.get("feed_topic", "Slack feed"),
                    "turso_url_env": pair.get(
                        "turso_url_env", f"TURSO_{name.upper().replace('-', '_')}_DATABASE_URL"
                    ),
                    "turso_token_env": pair.get(
                        "turso_token_env", f"TURSO_{name.upper().replace('-', '_')}_AUTH_TOKEN"
                    ),
                    "slack": {
                        "team_id": data["slack"]["team_id"],
                        "channel_id": pair["slack_channel_id"],
                    },
                    "zulip": {
                        "site": data["zulip"]["site"],
                        "channel_id": pair["zulip_channel_id"],
                    },
                },
                path.parent,
            )
        )
    for values in (
        [c.pair_id for c in configs],
        [c.slack_channel for c in configs],
        [c.zulip_channel for c in configs],
        [
            str(c.database.resolve()) if c.storage_backend == "sqlite" else c.turso_url_env
            for c in configs
        ],
    ):
        if len(values) != len(set(values)):
            raise ValueError(
                "Pair ids, channels, and databases must be unique; fan-out is not supported"
            )
    return configs
