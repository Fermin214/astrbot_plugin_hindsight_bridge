"""Tokenizer download stalls and subprocess cancellation cannot use live model tests."""

import asyncio
import pickle
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from plugins.astrbot_plugin_hindsight_bridge.core import materials


class TokenizerLoading(unittest.IsolatedAsyncioTestCase):
    async def test_host_builds_encoding_without_another_download(self):
        name = "isolated-loader-test"
        options = {
            "name": name, "pat_str": r"[\s\S]",
            "mergeable_ranks": {bytes([index]): index for index in range(256)},
            "special_tokens": {},
        }
        process = SimpleNamespace(
            returncode=0,
            communicate=AsyncMock(return_value=(pickle.dumps(options), None)),
        )
        try:
            with patch.object(materials.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)), patch.object(
                materials.tiktoken, "get_encoding", side_effect=AssertionError("host download")
            ):
                loaded = await materials.load_encoding(name)
                self.assertEqual(loaded.decode(materials.encoding(name).encode("你好")), "你好")
        finally:
            materials._loaded_encodings.pop(name, None)
            materials.encoding.cache_clear()

    async def test_timeout_and_cancellation_reap_the_loader(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                started = asyncio.Event()

                async def communicate():
                    if process.returncode is not None:
                        return b"", None
                    started.set()
                    await asyncio.Event().wait()

                def kill():
                    process.returncode = -9

                process = SimpleNamespace(
                    returncode=None, communicate=AsyncMock(side_effect=communicate),
                    kill=Mock(side_effect=kill),
                )
                with patch.object(materials.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                    task = asyncio.create_task(materials.load_encoding("stalled-loader-test", timeout=0.05))
                    await started.wait()
                    if cancelled:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        with self.assertRaisesRegex(materials.TokenizerError, "tokenizer_timeout"):
                            await task
                    process.kill.assert_called_once()
                    self.assertEqual(process.communicate.await_count, 2)

    async def test_failed_loader_reports_no_subprocess_output(self):
        process = SimpleNamespace(
            returncode=1, communicate=AsyncMock(return_value=(b"private upstream failure", None)),
        )
        with patch.object(materials.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaisesRegex(materials.TokenizerError, "^tokenizer_load_failed$"):
                await materials.load_encoding("failed-loader-test")
