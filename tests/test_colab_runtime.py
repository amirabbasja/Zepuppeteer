"""Runtime detection reads only a toolbar screenshot region."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from zepuppeteer.tasks.colab.open_notebook import check_runtime_connection

class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_states_and_transition_precedence(self):
        for text, expected in (("RAM Disk", "connected"), ("Connected", "connected"), ("Connect", "disconnected"),
                               ("Connecting RAM Disk", "connecting"), ("Restarting RAM Disk", "restarting"),
                               ("No controls", "unknown")):
            page = SimpleNamespace(browser=SimpleNamespace(targets={}), read_text=AsyncMock(return_value=text))
            self.assertEqual(await check_runtime_connection(page, timeout=0), expected)
            self.assertNotEqual(page.read_text.call_args.args, ())
