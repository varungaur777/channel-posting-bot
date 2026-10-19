"""Print the chat id, title and type of every group/channel the bot sees, to fill .env easily.

Usage (bot must NOT be running anywhere else at the same time, or Telegram answers 409 Conflict):

    python tools/get_ids.py            # listens for 180 seconds
    python tools/get_ids.py 300        # listens for 300 seconds

While it runs:
  * approval group  -> send any message there (e.g. /id), or add the bot to the group
  * storage channel -> post anything (bot must be admin), or forward a channel post to the bot in DM
  * target channel  -> same as storage channel
Reads BOT_TOKEN from the environment / .env. Nothing is written anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from dotenv import load_dotenv
from telegram import Bot, Chat, Update
from telegram.error import Conflict, TelegramError


def _describe(chat: Chat) -> str:
    title = chat.title or chat.full_name or chat.username or "-"
    return f"{chat.id:>16}  {chat.type:<10}  {title}"


async def listen(token: str, seconds: int) -> int:
    seen: dict[int, Chat] = {}
    async with Bot(token) as bot:
        me = await bot.get_me()
        await bot.delete_webhook(drop_pending_updates=False)
        print(f"Bot: @{me.username}  (id {me.id})")
        print(f"Listening for {seconds}s - ab group mein message bhejo / channels mein post karo...\n")
        print(f"{'CHAT ID':>16}  {'TYPE':<10}  TITLE")
        offset = None
        deadline = time.monotonic() + seconds
        allowed = ["message", "channel_post", "my_chat_member", "edited_channel_post", "edited_message"]
        while time.monotonic() < deadline:
            try:
                updates: tuple[Update, ...] = await bot.get_updates(
                    offset=offset, timeout=min(20, max(1, int(deadline - time.monotonic()))),
                    allowed_updates=allowed)
            except Conflict:
                print("\n409 Conflict: bot kahin aur chal raha hai (GitHub Actions / dusra terminal). "
                      "Pehle use band karo.")
                return 1
            except TelegramError as e:
                print(f"(network issue: {e}, retrying)")
                await asyncio.sleep(3)
                continue
            for upd in updates:
                offset = upd.update_id + 1
                chats: list[Chat] = []
                if upd.effective_chat:
                    chats.append(upd.effective_chat)
                msg = upd.effective_message
                origin = getattr(msg, "forward_origin", None) if msg else None
                if origin is not None and getattr(origin, "chat", None):
                    chats.append(origin.chat)  # forwarded from a channel / group
                if msg is not None and msg.migrate_to_chat_id:
                    print(f"  ! group {msg.chat_id} upgraded to supergroup -> NEW id {msg.migrate_to_chat_id}")
                for chat in chats:
                    if chat.id not in seen:
                        seen[chat.id] = chat
                        print(_describe(chat), flush=True)
    groups = [c for c in seen.values() if c.type in ("group", "supergroup", "channel")]
    print("\nDone.", "Groups/channels found:" if groups else "Koi group/channel nahi mila.")
    for c in groups:
        hint = "APPROVAL_GROUP_ID" if c.type in ("group", "supergroup") else "STORAGE_CHANNEL_ID / CHANNEL_ID"
        print(f"  {_describe(c)}   -> {hint}")
    if any(c.type == "group" for c in groups):
        print("\nNote: type 'group' = basic group. Pehle ise supergroup bana lo (ID badal jayegi), phir dobara chalao.")
    return 0


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = (os.environ.get("BOT_TOKEN") or "").strip()
    if not token:
        print("BOT_TOKEN missing: .env mein BOT_TOKEN daalo.")
        return 2
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    return asyncio.run(listen(token, seconds))


if __name__ == "__main__":
    raise SystemExit(main())
