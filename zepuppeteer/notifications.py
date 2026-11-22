"""Best-effort Telegram alerts for workflow prerequisites requiring attention."""

import asyncio
import json
import os
from pathlib import Path
import shlex
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .models import Profile


ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def _telegram_credentials() -> tuple[str, str]:
    values = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.strip().removeprefix("export ").partition("=")
            key = key.strip().lower()
            if separator and key in {"telegram_bot_token", "telegram_chat_id"}:
                parts = shlex.split(value, comments=True)
                if len(parts) > 1:
                    raise ValueError("Telegram .env values containing spaces must be quoted.")
                values[key] = parts[0] if parts else ""

    def setting(name):
        return (os.environ.get(name.upper()) or os.environ.get(name) or values.get(name, "")).strip()

    return setting("telegram_bot_token"), setting("telegram_chat_id")


def _send_telegram(token: str, chat_id: str, message: str) -> int | None:
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": message,
                         "link_preview_options": {"is_disabled": True}}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        result = json.load(response)
    if result.get("ok") is not True:
        raise RuntimeError("Telegram did not accept the notification.")
    return result.get("result", {}).get("message_id")


async def notify_attention(profile: Profile, platform: str, description: str) -> bool:
    """Send one plain-text alert; never replace the underlying workflow failure."""
    try:
        token, chat_id = _telegram_credentials()
        if not token or not chat_id:
            print("Telegram alert not sent: configure telegram_bot_token and telegram_chat_id in .env "
                  "or TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment.", file=sys.stderr, flush=True)
            return False
        name = profile.name
        if profile.browser_display_name:
            name += f" ({profile.browser_display_name})"
        message = (f"🔴 Attention needed\nProfile: {name}\n"
                   f"Platform: {platform}\nDescription: {description}")
        # Telegram's message limit is 4096 characters. Count UTF-16 units as
        # well so emoji cannot exceed the limit; do not send profile credentials.
        encoded = message.encode("utf-16-le")
        if len(encoded) > 4096 * 2:
            message = encoded[:4095 * 2].decode("utf-16-le", errors="ignore") + "…"
        message_id = await asyncio.to_thread(_send_telegram, token, chat_id, message)
    except Exception as error:
        # HTTP/transport exceptions can contain the URL, including the bot token.
        detail = f"HTTP {error.code}" if isinstance(error, HTTPError) else type(error).__name__
        print(f"Telegram alert not sent ({detail}); the original task failure is preserved.",
              file=sys.stderr, flush=True)
        return False
    print(f"[{profile.name}] Telegram alert accepted (message ID: {message_id}).", flush=True)
    return True
