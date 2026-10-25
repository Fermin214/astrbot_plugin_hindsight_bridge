"""Explicit platform-instance/user pairs and conservative P1 configuration migration."""

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


def migrate(config, known_accounts=()):
    if int(config.get("permission_config_version", 1)) >= 2:
        return pairs(config.get("allowed_accounts", [])), ""
    explicit = config.get("allowed_accounts", [])
    error = ""
    if explicit:
        selected = pairs(explicit)
    else:
        users = {
            str(v).strip() for v in config.get("allowed_user_ids", []) if str(v).strip()
        }
        platforms = {
            str(v).strip()
            for v in config.get("allowed_platform_ids", [])
            if str(v).strip()
        }
        if len(platforms) == 1 or (platforms and len(users) == 1):
            selected = frozenset((p, u) for p in platforms for u in users)
        elif not platforms:
            selected = frozenset((p, u) for p, u in known_accounts if u in users)
            if users and not selected:
                error = "permission_pairs_required"
        else:
            selected, error = frozenset(), "permission_pairs_required"
    config["allowed_accounts"] = [
        {"platform_instance_id": p, "user_id": u} for p, u in sorted(selected)
    ]
    config["permission_config_version"] = 2
    config.pop("allowed_user_ids", None)
    config.pop("allowed_platform_ids", None)
    if hasattr(config, "save_config"):
        config.save_config()
    return selected, error
