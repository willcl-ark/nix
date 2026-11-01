"""Administrator commands posted in the configured Telegram channel."""

import logging
import re
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
    parts = text.split(maxsplit=1)
    if not parts:
        return None
    command, separator, username = parts[0].partition("@")
    if separator and username.casefold() != bot.get("username", "").casefold():
        return None
    if command in {"/high", "/review"}:
        return (command, parts[1] if len(parts) > 1 else "")
    return command if command in {"/status", "/help"} and len(parts) == 1 else None


def _review_target(argument, repositories):
    """Resolve explicit or compact selectors without guessing an ambiguous prefix."""
    matches = set()
    for repo_id, entry in repositories.items():
        selectors = {repo_id}
        if "bitcoin/bitcoin" in {
            entry.get("repository"),
            entry.get("github_repository"),
        }:
            selectors.add("bitcoin")
        for selector in selectors:
            if argument.startswith(selector):
                number = argument[len(selector) :].strip()
                if re.fullmatch(r"[1-9][0-9]{0,9}", number):
                    matches.add((repo_id, int(number)))
    return next(iter(matches)) if len(matches) == 1 else None


def _admin_only(token, post, bot):
    chat_id = post["chat"]["id"]
    sender_chat = post.get("sender_chat")
    if sender_chat is not None and sender_chat.get("id") != chat_id:
        return False
    sender = post.get("from")
    admins = telegram.api_request(
        token,
        "getChatAdministrators",
        {"chat_id": chat_id, "return_bots": True},
    )
    if sender and sender.get("id") not in {admin["user"]["id"] for admin in admins}:
        return False
    # Anonymous channel posts are restricted to posting administrators by Telegram.
    return any(
        admin["user"]["id"] == bot["id"] and admin.get("can_post_messages")
        for admin in admins
    )


def poll_commands(
    store,
    token,
    configured_chat_id,
    bot,
    state_dir,
    report_dir,
    report_base_url,
    timeout=20,
    *,
    repositories,
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
        return 0
    replies = 0
    with closing(store._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        for update in updates:
            if update["update_id"] < offset:
                continue
            post = update.get("channel_post", {})
            command = _command(post, configured_chat_id, bot)
            if command and not _admin_only(token, post, bot):
                logging.info(
                    "Ignored Telegram command from a non-administrator or unverified channel"
                )
                command = None
            if command:
                if isinstance(command, tuple):
                    target = _review_target(command[1], repositories)
                    if target is not None:
                        repo_id, number = target
                        profile = "high" if command[0] == "/high" else "default"
                        db.execute(
                            "INSERT OR IGNORE INTO review_requests (id, repo_id, number, profile) VALUES (?, ?, ?, ?)",
                            (update["update_id"], repo_id, number, profile),
                        )
                        label = "high" if profile == "high" else "normal"
                        text = (
                            f"Requested a fresh {label} review of {repo_id}#{number}."
                        )
                    else:
                        text = f"Usage: {command[0]} <repository> <PR number> (for example gui 1049 or bitcoin36688)"
                elif command == "/status":
                    from . import status

                    text = status.status_message(state_dir, report_dir, report_base_url)
                else:
                    text = (
                        "Ralph commands\n"
                        "/status - Service health, review results and usage\n"
                        "/review <repository> <PR number> - Request a fresh normal review\n"
                        "/high <repository> <PR number> - Request a fresh high review\n"
                        "Compact forms also work: /review gui1049 or bitcoin36688\n"
                        "/help - Show these commands\n"
                        "Post commands in this channel as an administrator.\n"
                        + "Repositories: "
                        + ", ".join(sorted(repositories))
                    )
                replies += db.execute(
                    "INSERT OR IGNORE INTO outbox (key, text) VALUES (?, ?)",
                    (f"command:{update['update_id']}", telegram.truncate_message(text)),
                ).rowcount
            offset = max(offset, update["update_id"] + 1)
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('update_offset', ?)",
            (str(offset),),
        )
    return replies
