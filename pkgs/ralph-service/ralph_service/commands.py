"""Owner-only commands posted in the configured Telegram channel."""

from contextlib import closing

from . import telegram


def _command(post, configured_chat_id, bot):
    chat = post.get("chat", {})
    if chat.get("type") != "channel":
        return None
    destination = str(configured_chat_id)
    if destination.startswith("@"):
        if destination[1:].casefold() != chat.get("username", "").casefold():
            return None
    elif destination != str(chat.get("id")):
        return None
    text = post.get("text", "").strip()
    command, separator, username = text.partition("@")
    if separator and username.casefold() != bot.get("username", "").casefold():
        return None
    return command if command in {"/status", "/help"} else None


def _owner_only(token, post, bot):
    chat_id = post["chat"]["id"]
    sender_chat = post.get("sender_chat")
    if sender_chat is not None and sender_chat.get("id") != chat_id:
        return False
    sender = post.get("from")
    if sender and sender.get("is_bot"):
        return False
    admins = telegram.api_request(
        token,
        "getChatAdministrators",
        {"chat_id": chat_id, "return_bots": True},
    )
    owners = [admin["user"]["id"] for admin in admins if admin["status"] == "creator"]
    if len(owners) != 1 or (sender and sender.get("id") != owners[0]):
        return False
    bot_can_post = False
    for admin in admins:
        if admin["status"] == "creator":
            continue
        if admin.get("can_post_messages"):
            if admin["user"]["id"] != bot["id"]:
                return False
            bot_can_post = True
    return bot_can_post


def poll_commands(
    store,
    token,
    configured_chat_id,
    bot,
    state_dir,
    report_dir,
    report_base_url,
    timeout=20,
):
    """Queue replies and acknowledge a fetched batch in one transaction."""
    with closing(store._connect()) as db:
        saved = db.execute(
            "SELECT value FROM metadata WHERE key = 'update_offset'"
        ).fetchone()
    offset = int(saved[0]) if saved else 0
    updates = telegram.api_request(
        token,
        "getUpdates",
        {"offset": offset, "timeout": timeout, "allowed_updates": ["channel_post"]},
        timeout=timeout + 10,
    )
    if not updates:
        return
    with closing(store._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        for update in updates:
            if update["update_id"] < offset:
                continue
            post = update.get("channel_post", {})
            command = _command(post, configured_chat_id, bot)
            if command and _owner_only(token, post, bot):
                if command == "/status":
                    from . import status

                    text = status.status_message(state_dir, report_dir, report_base_url)
                else:
                    text = (
                        "Ralph monitoring commands\n"
                        "/status - Service health, review results and costs\n"
                        "/help - Show these commands\n"
                        "Post commands in this channel as its owner."
                    )
                db.execute(
                    "INSERT OR IGNORE INTO outbox (key, text) VALUES (?, ?)",
                    (f"command:{update['update_id']}", telegram.truncate_message(text)),
                )
            offset = max(offset, update["update_id"] + 1)
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('update_offset', ?)",
            (str(offset),),
        )
