from plugins.astrbot_plugin_hindsight_bridge.core.identity import Incoming


def confirm_sources(queue, *identities):
    queue.sync_permissions([(instance, user) for instance, _, user in identities])
    for instance, bot, user in identities:
        source = Incoming("aiocqhttp", instance, bot, user, "fixture", 1, "fixture")
        if not queue.access.permitted(source.scope(queue.deployment_id)):
            grant = queue.access.authorization(instance, user)
            queue.activate(source, grant["revision"], now=0)
