"""Stable memory identities, independent of conversations and display names."""

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Scope:
    deployment_id: str
    platform_type: str
    platform_instance_id: str
    bot_id: str
    user_id: str
    memory_space_id: str = "default"

    @property
    def identity(self) -> str:
        return canonical(
            [
                self.deployment_id,
                self.platform_type,
                self.platform_instance_id,
                self.bot_id,
                self.user_id,
                self.memory_space_id,
            ]
        )

    @property
    def id(self) -> str:
        return sha256(self.identity)

    def bank(self, generation: int = 0) -> str:
        return f"abm-v1-{self.id}-g{generation}"

    def event_key(self, message_id: str) -> str:
        return sha256(
            canonical(
                [
                    self.deployment_id,
                    self.platform_type,
                    self.platform_instance_id,
                    self.bot_id,
                    self.user_id,
                    message_id,
                ]
            )
        )


@dataclass(frozen=True)
class Incoming:
    platform_type: str
    platform_instance_id: str
    bot_id: str
    user_id: str
    message_id: str
    timestamp: int
    text: str
    received_at: float = field(default_factory=time.time, compare=False)

    @property
    def iso_timestamp(self) -> str:
        return datetime.fromtimestamp(self.timestamp, UTC).isoformat()

    def scope(self, deployment_id: str) -> Scope:
        return Scope(
            deployment_id,
            self.platform_type,
            self.platform_instance_id,
            self.bot_id,
            self.user_id,
        )
