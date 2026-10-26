"""Explicit platform-instance/user pairs."""

from .control import QueueError


def pairs(entries):
    result = set()
    if not isinstance(entries, (list, tuple)):
        raise QueueError("invalid_allowed_accounts")
    for entry in entries:
        if not isinstance(entry, dict):
            raise QueueError("invalid_allowed_account")
        platform, user = entry.get("platform_instance_id"), entry.get("user_id")
        if not isinstance(platform, str) or not platform.strip():
            raise QueueError("invalid_platform_instance_id")
        if not isinstance(user, str) or not user.strip():
            raise QueueError("invalid_allowed_user_id")
        result.add((platform.strip(), user.strip()))
    return frozenset(result)
