"""User-facing texts (simple Hinglish) and message rendering. All texts use parse_mode=HTML."""

from __future__ import annotations

from datetime import datetime, tzinfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.utils import fmt_dt, h, preview_text, truncate

STATUS_EMOJI = {
    "pending": "⏳", "approved": "🗓", "publishing": "🚀", "published": "✅",
    "rejected": "❌", "cancelled": "🚫", "failed": "⚠️", "needs_review": "🔍",
}
STATUS_LABEL = {
    "pending": "Pending (approval ka wait)", "approved": "Scheduled", "publishing": "Publishing...",
    "published": "Published", "rejected": "Rejected", "cancelled": "Cancelled", "failed": "Failed",
    "needs_review": "Needs review",
}
# /list argument -> stored status
LIST_ALIASES = {"scheduled": "approved", "approved": "approved", "review": "needs_review"}


def status_text(status: str) -> str:
    return f"{STATUS_EMOJI.get(status, '•')} {STATUS_LABEL.get(status, status)}"


def approval_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"ap:{post_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"rj:{post_id}"),
    ]])


def fmt_eta(eta: tuple[datetime, datetime] | None, tz: tzinfo, paused: bool = False) -> str:
    if eta is None:
        return "-"
    early, late = eta
    same_day = early.astimezone(tz).date() == late.astimezone(tz).date()
    text = f"~{fmt_dt(early, tz)} – {fmt_dt(late, tz, with_date=not same_day)}"
    return text + (" (abhi PAUSED hai)" if paused else "")


def render_control(post: dict, tz: tzinfo, *, eta: tuple[datetime, datetime] | None = None,
                   paused: bool = False) -> str:
    """The control message in the approval group; kept updated through the whole lifecycle."""
    st = post["status"]
    lines = [
        f"📝 <b>Post #{post['_id']}</b>",
        f"👤 Submitter: {h(post.get('submitter_name'))} (<code>{post.get('submitter_id')}</code>)",
        f"📦 Type: {h(post.get('content_type'))}",
        f"🕒 Submitted: {fmt_dt(post.get('created_at'), tz)}",
        f"📌 Status: <b>{status_text(st)}</b>",
    ]
    if post.get("decided_by") and st != "pending":
        verb = "Rejected" if st == "rejected" else "Approved"
        lines.append(f"👮 {verb} by {h(post.get('decided_by_name'))} (<code>{post.get('decided_by')}</code>)"
                     f" · {fmt_dt(post.get('decided_at'), tz)}")
    if st == "rejected" and post.get("reject_reason"):
        lines.append(f"💬 Reason: {h(post['reject_reason'])}")
    if st == "approved":
        lines.append(f"⏰ ETA: {fmt_eta(eta, tz, paused)}")
        if post.get("retries"):
            lines.append(f"🔁 Retry {post['retries']} · next try {fmt_dt(post.get('next_attempt_at'), tz)}")
    if st == "published":
        lines.append(f"📢 Published: {fmt_dt(post.get('published_at'), tz)}")
    if st == "cancelled":
        lines.append(f"🚫 Cancelled by <code>{post.get('cancelled_by')}</code>"
                     f" · {fmt_dt(post.get('cancelled_at'), tz)}")
    if st in ("failed", "needs_review") and post.get("last_error"):
        lines.append(f"⚠️ {h(truncate(post['last_error'], 300))}")
    if st == "needs_review":
        lines.append("👉 Owner: channel check karo, phir zarurat ho to /retry " + str(post["_id"]))
    return "\n".join(lines)


def render_details(post: dict, tz: tzinfo, eta: tuple[datetime, datetime] | None = None,
                   paused: bool = False) -> str:
    lines = [render_control(post, tz, eta=eta, paused=paused), ""]
    lines.append(f"🔁 Retries: {post.get('retries', 0)}")
    if post.get("next_attempt_at"):
        lines.append(f"⏭ Next attempt: {fmt_dt(post['next_attempt_at'], tz)}")
    if post.get("last_error") and post["status"] not in ("failed", "needs_review"):
        lines.append(f"🧾 Last error: {h(truncate(post['last_error'], 300))}")
    lines.append(f"🗄 Storage msgs: <code>{post.get('storage_message_ids') or '-'}</code>")
    if post.get("channel_message_ids"):
        lines.append(f"📣 Channel msgs: <code>{post['channel_message_ids']}</code>")
    preview = truncate(preview_text(post.get("items") or []), 200)
    if preview:
        lines.append(f"🔤 Preview: {h(preview)}")
    return "\n".join(lines)


def render_list_line(post: dict, tz: tzinfo) -> str:
    when = post.get("published_at") or post.get("decided_at") or post.get("created_at")
    preview = truncate(preview_text(post.get("items") or []), 40)
    return (f"{STATUS_EMOJI.get(post['status'], '•')} <b>#{post['_id']}</b> · {h(post.get('content_type'))} · "
            f"{fmt_dt(when, tz)}" + (f" · {h(preview)}" if preview else ""))


def help_text(min_iv: float, max_iv: float) -> str:
    return (
        "🤖 <b>Channel Posting Bot</b>\n\n"
        "📤 <b>Post kaise bhejein:</b>\n"
        "Bas mujhe yahan DM mein content bhejo – text, photo, video, GIF, album, document, audio, voice, "
        "poll, location, kuch bhi. Main use approval group mein bhej dunga.\n\n"
        f"✅ Approve hone ke baad post queue mein jata hai aur har {min_iv:g}–{max_iv:g} minute ke gap pe "
        "channel mein publish hota hai. Har step pe aapko message milega.\n\n"
        "✏️ Bhejne ke baad edit karoge to wo track nahi hoga – /cancel karke dobara bhejo.\n\n"
        "<b>Commands:</b>\n"
        "/id – apna user ID aur chat ID\n"
        "/stats – kitne post kis status mein, interval, next slot\n"
        "/queue – scheduled posts ETA ke saath\n"
        "/list &lt;pending|scheduled|published|rejected|cancelled|failed|needs_review|all&gt; [n]\n"
        "/post &lt;id&gt; – post ki details (DM mein preview bhi)\n"
        "/approve &lt;id&gt; · /reject &lt;id&gt; [reason] – approvers ke liye\n"
        "/cancel &lt;id&gt; – apna pending/scheduled post cancel karo\n\n"
        "<b>Owner only:</b>\n"
        "/retry &lt;id&gt; · /pause · /resume · /setinterval &lt;min&gt; &lt;max&gt;"
    )


def unauthorized_text(user_id: int) -> str:
    return (f"🙏 Aap is bot ko use karne ke liye authorized nahi ho.\n"
            f"Aapka user ID: <code>{user_id}</code>\n"
            "Ye ID owner ko bhejo agar access chahiye.")


BOT_COMMANDS = [
    ("start", "Bot kaise use karein"),
    ("help", "Help aur commands"),
    ("id", "Chat ID aur user ID"),
    ("stats", "Status counts, interval, next slot"),
    ("queue", "Scheduled posts ETA ke saath"),
    ("list", "Recent posts: /list <status> [n]"),
    ("post", "Post details: /post <id>"),
    ("approve", "Approve: /approve <id>"),
    ("reject", "Reject: /reject <id> [reason]"),
    ("cancel", "Cancel: /cancel <id>"),
    ("retry", "Owner: /retry <id>"),
    ("pause", "Owner: publishing roko"),
    ("resume", "Owner: publishing chalu karo"),
    ("setinterval", "Owner: /setinterval <min> <max>"),
]
