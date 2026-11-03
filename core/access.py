"""Authorization epochs and replay receipts in the latest control journal."""

import math
import json
import time
from dataclasses import dataclass

from .control import QueueError
from .identity import canonical, sha256


@dataclass(frozen=True)
class Command:
    source_id: str
    event_key: str
    digest: str
    occurred_at: float
    received_at: float
    authorization_revision: int
    bank: str
    generation: int


class Access:
    def __init__(self, control, connection):
        self.control, self.connection = control, connection

    def authorization(self, platform, user):
        return self.control.db.execute(
            "SELECT * FROM authorizations WHERE connection_id=? "
            "AND platform_instance_id=? AND user_id=?",
            (self.connection, platform, user),
        ).fetchone()

    def sync(self, selected, *, legacy_sources=()):
        """Selection can revoke immediately; activation always needs confirmation."""
        control = self.control
        with control.transaction():
            for row in control.db.execute(
                "SELECT * FROM authorizations WHERE connection_id=?", (self.connection,),
            ).fetchall():
                if row["enabled"] and (row["platform_instance_id"], row["user_id"]) not in selected:
                    control.db.execute(
                        "UPDATE authorizations SET enabled=0,revision=revision+1,"
                        "accept_from=MAX(accept_from,?) WHERE connection_id=? "
                        "AND platform_instance_id=? AND user_id=?",
                        (time.time(), self.connection, row["platform_instance_id"], row["user_id"]),
                    )
                    control.bump()
            for platform, user in selected:
                if self.authorization(platform, user):
                    continue
                old = control.db.execute(
                    "SELECT a.source_id,o.accept_from FROM accounts a JOIN owners o "
                    "USING(connection_id,owner_id) WHERE a.connection_id=? "
                    "AND a.platform_instance_id=? AND a.user_id=?",
                    (self.connection, platform, user),
                ).fetchall() if legacy_sources else []
                # An ambiguous historical pair needs explicit identity confirmation.
                source, lower = (
                    (old[0]["source_id"], old[0]["accept_from"])
                    if len(old) == 1 and old[0]["source_id"] in legacy_sources
                    else ("", 0)
                )
                control.db.execute(
                    "INSERT INTO authorizations(connection_id,platform_instance_id,user_id,"
                    "source_id,enabled,accept_from) VALUES (?,?,?,?,?,?)",
                    (self.connection, platform, user, source, int(bool(source)), lower),
                )
                control.bump()

    def activate(self, source, expected_revision, *, now=None):
        control = self.control
        row = self.authorization(source.platform_instance_id, source.user_id)
        if not row or row["revision"] != expected_revision:
            raise QueueError("authorization_changed")
        account = control.account(self.connection, source.id)
        lower = max(
            time.time() if now is None else now,
            row["accept_from"],
            account["accept_from"] if account else 0,
        )
        with control.transaction():
            control.db.execute(
                "UPDATE authorizations SET source_id=?,enabled=1,revision=revision+1,"
                "accept_from=? WHERE connection_id=? AND platform_instance_id=? AND user_id=?",
                (source.id, lower, self.connection, source.platform_instance_id, source.user_id),
            )
            if account:
                control.db.execute(
                    "UPDATE owners SET revision=revision+1,command_from=MAX(command_from,?),"
                    "command_received_from=MAX(command_received_from,?) "
                    "WHERE connection_id=? AND owner_id=?",
                    (lower, lower, self.connection, account["owner_id"]),
                )
            control.bump()
        return self.authorization(source.platform_instance_id, source.user_id)

    def permitted(self, source):
        row = self.authorization(source.platform_instance_id, source.user_id)
        return bool(row and row["enabled"] and row["source_id"] == source.id)

    def observe_bot(self, platform, logical_type, bot_id):
        """Confirmed account changes suspend old bindings; never bind a new account."""
        control, changed = self.control, False
        with control.transaction():
            for row in control.db.execute(
                "SELECT g.*,a.identity_json FROM authorizations g JOIN accounts a "
                "ON a.connection_id=g.connection_id AND a.source_id=g.source_id "
                "WHERE g.connection_id=? AND g.platform_instance_id=? AND g.enabled=1",
                (self.connection, platform),
            ).fetchall():
                identity = json.loads(row["identity_json"])
                if identity[1] == logical_type and identity[3] == bot_id:
                    continue
                control.db.execute(
                    "UPDATE authorizations SET enabled=0,revision=revision+1,"
                    "accept_from=MAX(accept_from,?) WHERE connection_id=? "
                    "AND platform_instance_id=? AND user_id=?",
                    (time.time(), self.connection, platform, row["user_id"]),
                )
                control.bump()
                changed = True
        return changed

    def accept(self, incoming, source):
        row = self.authorization(source.platform_instance_id, source.user_id)
        if not row or not row["enabled"] or row["source_id"] != source.id:
            return False
        account = self.control.account(self.connection, source.id)
        return (
            incoming.timestamp >= math.ceil(row["accept_from"])
            and incoming.received_at > max(
                row["accept_from"], account["receive_from"] if account else 0,
            )
        )

    def command(self, incoming, source, scope, name, arguments=()):
        row = self.authorization(source.platform_instance_id, source.user_id)
        if not self.permitted(source):
            raise QueueError("authorization_changed")
        owner = self.control.owner(self.connection, scope["owner_id"])
        command = Command(
            source.id, source.event_key(incoming.message_id),
            sha256(canonical([name, list(arguments), sha256(incoming.text)])),
            incoming.timestamp, incoming.received_at, row["revision"], scope["bank"], scope["generation"],
        )
        previous = self.receipt(command)
        if previous:
            code = "command_identity_conflict" if previous["digest"] != command.digest else "command_already_applied"
            raise QueueError(code)
        if (
            incoming.received_at <= max(row["accept_from"], owner["command_received_from"])
            or incoming.timestamp < math.ceil(max(
                row["accept_from"], owner["command_from"], owner["accept_from"],
            ))
        ):
            raise QueueError("command_expired")
        return command

    def receipt(self, command):
        return self.control.db.execute(
            "SELECT * FROM command_receipts WHERE connection_id=? AND source_id=? AND event_key=?",
            (self.connection, command.source_id, command.event_key),
        ).fetchone()

    def run(self, command, scope, operation):
        """No await inside this transaction: consumed events and changes are atomic."""
        control = self.control
        with control.transaction():
            if self.receipt(command):
                raise QueueError("command_already_applied")
            authorization = control.db.execute(
                "SELECT * FROM authorizations WHERE connection_id=? AND source_id=? "
                "AND enabled=1 AND revision=?",
                (self.connection, command.source_id, command.authorization_revision),
            ).fetchone()
            owner = control.owner(self.connection, scope["owner_id"])
            if (
                not authorization or not owner or owner["bank"] != command.bank
                or owner["generation"] != command.generation
                or owner["revision"] != scope["revision"]
                or command.received_at <= max(
                    authorization["accept_from"], owner["command_received_from"],
                )
                or command.occurred_at < math.ceil(max(
                    authorization["accept_from"], owner["command_from"], owner["accept_from"],
                ))
            ):
                raise QueueError("command_expired")
            control.db.execute(
                "INSERT INTO command_receipts VALUES (?,?,?,?,?,?,?,?,?)",
                (self.connection, command.source_id, command.event_key, command.digest,
                 command.occurred_at, command.authorization_revision, command.bank,
                 command.generation, "registered"),
            )
            control.db.execute(
                "UPDATE owners SET command_from=MAX(command_from,?),"
                "command_received_from=MAX(command_received_from,?) "
                "WHERE connection_id=? AND owner_id=?",
                # OneBot timestamps have second precision. The next command
                # must prove it belongs to a later second, even with clock skew.
                (max(time.time(), math.floor(command.occurred_at) + 1),
                 max(time.time(), command.received_at), self.connection, scope["owner_id"]),
            )
            result = operation()
            control.db.execute(
                "UPDATE command_receipts SET result='applied' WHERE connection_id=? "
                "AND source_id=? AND event_key=?",
                (self.connection, command.source_id, command.event_key),
            )
            control.bump()
        return result
