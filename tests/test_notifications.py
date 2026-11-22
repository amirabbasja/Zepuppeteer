import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from zepuppeteer.models import Profile
from zepuppeteer.notifications import notify_attention


class TelegramNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="zepuppeteer-telegram-")
        self.addCleanup(temporary.cleanup)
        self.env_file = Path(temporary.name) / ".env"
        self.env_file.write_text('telegram_bot_token="test-secret"\ntelegram_chat_id="12345"\n')
        self.profile = Profile("acc-5", Path(temporary.name), browser_display_name="AI-acc5")
        env_patch = patch.dict(os.environ, {}, clear=True)
        file_patch = patch("zepuppeteer.notifications.ENV_FILE", self.env_file)
        env_patch.start()
        file_patch.start()
        self.addCleanup(env_patch.stop)
        self.addCleanup(file_patch.stop)

    def successful_response(self):
        return contextlib.closing(io.StringIO('{"ok":true,"result":{"message_id":1}}'))

    async def test_alert_contains_requested_fields_and_uses_configured_bot(self):
        output = io.StringIO()
        with patch("zepuppeteer.notifications.urlopen", return_value=self.successful_response()) as request, \
                contextlib.redirect_stdout(output):
            sent = await notify_attention(self.profile, "colab", "Runtime disconnected")
        self.assertTrue(sent)
        self.assertIn("Telegram alert accepted (message ID: 1)", output.getvalue())
        self.assertNotIn("test-secret", output.getvalue())
        args, kwargs = request.call_args
        outgoing = args[0]
        self.assertEqual(outgoing.full_url, "https://api.telegram.org/bottest-secret/sendMessage")
        self.assertEqual(outgoing.get_method(), "POST")
        payload = json.loads(outgoing.data)
        self.assertEqual(payload["chat_id"], "12345")
        for line in ("Profile: acc-5 (AI-acc5)", "Platform: colab", "Description: Runtime disconnected"):
            self.assertIn(line, payload["text"].splitlines())
        self.assertNotIn("parse_mode", payload)
        self.assertEqual(kwargs["timeout"], 10)

    async def test_environment_settings_override_dotenv(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "environment-secret", "TELEGRAM_CHAT_ID": "67890"}), \
                patch("zepuppeteer.notifications.urlopen", return_value=self.successful_response()) as request:
            self.assertTrue(await notify_attention(self.profile, "colab", "Sign in required"))
        outgoing = request.call_args.args[0]
        self.assertIn("botenvironment-secret/", outgoing.full_url)
        self.assertEqual(json.loads(outgoing.data)["chat_id"], "67890")

    async def test_missing_configuration_skips_sending(self):
        self.env_file.unlink()
        with patch("zepuppeteer.notifications.urlopen") as request, contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(await notify_attention(self.profile, "colab", "Sign in required"))
        request.assert_not_called()

    async def test_api_failure_is_best_effort_and_does_not_log_token(self):
        failure = HTTPError("https://api.telegram.org/bottest-secret/sendMessage", 401, "Unauthorized", {}, None)
        output = io.StringIO()
        with patch("zepuppeteer.notifications.urlopen", side_effect=failure), contextlib.redirect_stderr(output):
            self.assertFalse(await notify_attention(self.profile, "colab", "Sign in required"))
        self.assertIn("HTTP 401", output.getvalue())
        self.assertNotIn("test-secret", output.getvalue())

    async def test_rejected_api_response_and_timeout_are_best_effort(self):
        with patch("zepuppeteer.notifications.urlopen", return_value=contextlib.closing(io.StringIO('{"ok":false}'))), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(await notify_attention(self.profile, "colab", "Sign in required"))
        with patch("zepuppeteer.notifications.urlopen", side_effect=TimeoutError("test-secret")), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertFalse(await notify_attention(self.profile, "colab", "Sign in required"))
        self.assertNotIn("test-secret", output.getvalue())

    async def test_long_unicode_description_fits_telegram_limit(self):
        with patch("zepuppeteer.notifications.urlopen", return_value=self.successful_response()) as request:
            self.assertTrue(await notify_attention(self.profile, "colab", "⚠️" * 5000))
        message = json.loads(request.call_args.args[0].data)["text"]
        self.assertLessEqual(len(message.encode("utf-16-le")), 4096 * 2)
        self.assertTrue(message.endswith("…"))
