import asyncio
import json
from pathlib import Path
import time
from urllib.parse import unquote, urlsplit


ROOT = Path("/delivery")
ALICE, BOB, OUTSIDER, BOT = 123001, 123002, 123003, 990001


def message_segments(value):
    if isinstance(value, str):
        return [{"type": "text", "data": {"text": value}}]
    if isinstance(value, list):
        return [part for item in value for part in message_segments(item)]
    if isinstance(value, dict):
        if value.get("type") == "node":
            return message_segments(value.get("data", {}).get("content", []))
        return [value]
    return []


class OneBot:
    sequence = 1000

    def __init__(self, session, settings):
        self.session, self.settings = session, settings
        self.bot_id = settings.get("bot_id", BOT)
        self.socket = None
        self.reader = None
        self.actions = []
        self.files = []
        self.replies = asyncio.Queue()
        self.message_id = type(self).sequence

    async def connect(self):
        import aiohttp

        async with asyncio.timeout(30):
            while True:
                try:
                    self.socket = await self.session.ws_connect(
                        f'http://127.0.0.1:{self.settings["onebot_port"]}/ws',
                        headers={"X-Self-ID": str(self.bot_id), "X-Client-Role": "Universal",
                                 "Authorization": "Bearer " + self.settings["onebot_token"]})
                    break
                except (aiohttp.ClientError, OSError):
                    await asyncio.sleep(0.25)
        self.reader = asyncio.create_task(self.read())
        await self.socket.send_json({"time": int(time.time()), "self_id": self.bot_id, "post_type": "meta_event",
                                     "meta_event_type": "lifecycle", "sub_type": "connect"})

    async def read(self):
        import aiohttp

        async for message in self.socket:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            body = json.loads(message.data)
            action, params = body.get("action", ""), body.get("params", {})
            self.actions.append(body)
            result = {}
            if action == "get_login_info":
                await asyncio.sleep(getattr(self, "login_info_delay", 0))
                result = {"user_id": self.bot_id, "nickname": "Delivery bot"}
            elif action == "get_status":
                result = {"online": True, "good": True}
            elif action == "get_msg":
                result = {"message_id": params["message_id"], "self_id": self.bot_id,
                          "user_id": ALICE, "message_type": "private", "sub_type": "friend",
                          "time": int(time.time()), "sender": {"user_id": ALICE, "nickname": "Synthetic quote"},
                          "message": [{"type": "text", "data": {"text": "UNTRUSTED_NATIVE_QUOTE"}}],
                          "raw_message": "UNTRUSTED_NATIVE_QUOTE", "font": 0}
            elif action in {"send_msg", "send_private_msg", "send_group_msg", "send_private_forward_msg", "send_group_forward_msg"}:
                result = {"message_id": len(self.actions) + 10000}
                segments = message_segments(params.get("message", params.get("messages", [])))
                for part in segments:
                    if part.get("type") != "file":
                        continue
                    uri = part.get("data", {}).get("file", "")
                    path = Path(unquote(urlsplit(uri).path) if uri.startswith("file:") else uri).resolve()
                    if not path.is_relative_to(ROOT):
                        raise RuntimeError("OneBot file fixture may only read the dedicated test directory")
                    self.files.append(json.loads(path.read_text()))
                await self.replies.put({**params, "message": segments})
            await self.socket.send_json({"status": "ok", "retcode": 0, "data": result, "echo": body.get("echo")})

    async def send(self, user, text, *, group=False, message_id=None, timestamp=None, metadata=None, segments=None):
        type(self).sequence += 1
        self.message_id = type(self).sequence
        while not self.replies.empty():
            self.replies.get_nowait()
        event = {
            "time": int(time.time()) if timestamp is None else timestamp,
            "self_id": self.bot_id, "post_type": "message", "message_type": "group" if group else "private",
            "sub_type": "normal" if group else "friend", "user_id": user,
            "message_id": message_id or self.message_id,
            "message": segments if segments is not None else [{"type": "text", "data": {"text": text}}],
            "raw_message": text, "font": 0, "sender": {"user_id": user, "nickname": "Synthetic user", "role": "member"},
        }
        if group:
            event["group_id"] = 880001
        if metadata:
            assert not event.keys() & metadata.keys()
            event.update(metadata)
        await self.socket.send_json(event)
        return event["message_id"]

    async def chat(self, user, text, *, timestamp=None, metadata=None, segments=None):
        await self.send(user, text, timestamp=timestamp, metadata=metadata, segments=segments)
        async with asyncio.timeout(100):
            while True:
                reply = await self.replies.get()
                content = reply.get("message", [])
                result = content if isinstance(content, str) else "".join(
                    part.get("data", {}).get("text", "") for part in content if part.get("type") == "text")
                if result:
                    return result

    async def close(self):
        try:
            if self.reader:
                self.reader.cancel()
                try:
                    await self.reader
                except asyncio.CancelledError:
                    pass
        finally:
            if self.socket:
                await self.socket.close()
