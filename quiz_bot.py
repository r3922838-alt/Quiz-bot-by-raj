"""
Quiz Bot - Main Application
============================
Dual-system: Advanced User Bot + Dynamic Admin Panel.
Built on python-telegram-bot v20+ with asyncio.

Author: HackerAI
"""
import io
import os
import re
import random
import asyncio
from typing import List, Dict, Optional
from datetime import datetime

from PIL import Image
import PyPDF2
import aiofiles

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatMemberAdministrator, ChatMemberOwner
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode

from config import (
    BOT_TOKEN, ADMIN_PASSWORD, CATEGORIES, FREE_TRIAL_CREDITS,
    CREDIT_COST_PER_QUIZ, QUIZ_CODE_PREFIX, QUIZ_TIME_LIMIT_SECONDS,
    IMAGE_MAX_WIDTH, IMAGE_QUALITY, UPLOAD_DIR, OWNER_ID
)
from database import db

# ─── Conversation States ────────────────────────────────────────────
(
    AWAIT_AI_PROMPT, AWAIT_PDF_FILE, AWAIT_IMAGE_QUIZ_QUESTION,
    AWAIT_IMAGE_QUIZ_OPTIONS, AWAIT_IMAGE_QUIZ_ANSWER,
    AWAIT_QUIZ_CATEGORY, AWAIT_ADMIN_PASSWORD, AWAIT_ADMIN_ACTION,
    AWAIT_ADMIN_CREDIT_USER, AWAIT_ADMIN_CREDIT_AMOUNT,
    AWAIT_ADMIN_CONFIG_FIELD, AWAIT_ADMIN_CONFIG_VALUE,
    AWAIT_IMAGE_CORRECT_ANSWER
) = range(13)

# ─── Temporary storage for in-progress creations ───────────────────
user_sessions: Dict[int, dict] = {}


# ═══════════════════════════════════════════════════════════════════
#  UTILITY HELPERS
# ═══════════════════════════════════════════════════════════════════

async def compress_image(image_bytes: bytes) -> bytes:
    """Downscale and compress image using Pillow. Returns compressed JPEG bytes."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    if img.width > IMAGE_MAX_WIDTH:
        ratio = IMAGE_MAX_WIDTH / img.width
        new_h = int(img.height * ratio)
        img = img.resize((IMAGE_MAX_WIDTH, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=IMAGE_QUALITY, optimize=True)
    return buf.getvalue()


async def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract text from a PDF buffer asynchronously."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _extract_pdf_text, file_bytes)


def _extract_pdf_text(file_bytes: bytes) -> str:
    """Synchronous PDF extraction."""
    reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
    text = ""
    for page in reader.pages:
        text += page.extract_text() + "\n"
    return text


def parse_quiz_from_text(text: str) -> Optional[List[Dict]]:
    """
    Parse loosely formatted quiz text into structured questions.
    Expected format per question block:

        Q: Question text?
        A) Option A
        B) Option B
        C) Option C
        D) Option D
        Answer: A
    """
    questions = []
    blocks = re.split(r'\n\s*\n', text.strip())
    for block in blocks:
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        q_text = None
        options = {}
        answer = None
        for line in lines:
            if re.match(r'^[Qq][:.]?\s*', line):
                q_text = re.sub(r'^[Qq][:.]?\s*', '', line)
            elif re.match(r'^[Aa][:.)]\s*', line):
                options['A'] = re.sub(r'^[Aa][:.)]\s*', '', line)
            elif re.match(r'^[Bb][:.)]\s*', line):
                options['B'] = re.sub(r'^[Bb][:.)]\s*', '', line)
            elif re.match(r'^[Cc][:.)]\s*', line):
                options['C'] = re.sub(r'^[Cc][:.)]\s*', '', line)
            elif re.match(r'^[Dd][:.)]\s*', line):
                options['D'] = re.sub(r'^[Dd][:.)]\s*', '', line)
            elif re.match(r'^[Aa]nswer\s*[:.]?\s*[A-Da-d]', line, re.I):
                m = re.search(r'[A-Da-d]', line)
                if m:
                    answer = m.group(0).upper()
        if q_text and len(options) == 4 and answer:
            questions.append({
                "question": q_text,
                "a": options['A'], "b": options['B'],
                "c": options['C'], "d": options['D'],
                "correct": answer
            })
    return questions if questions else None


def shuffle_quiz(questions: List[Dict]) -> List[Dict]:
    """
    Deep shuffle: randomize question order AND re-label options
    so A/B/C/D positions are shuffled per question.
    """
    shuffled = list(questions)
    random.shuffle(shuffled)
    result = []
    for q in shuffled:
        opts = [('A', q['a']), ('B', q['b']), ('C', q['c']), ('D', q['d'])]
        correct_text = q[q['correct'].lower()]
        random.shuffle(opts)
        new_labels = ['A', 'B', 'C', 'D']
        new_q = {
            "question": q["question"],
            "image_path": q.get("image_path")
        }
        new_correct = None
        for i, (old_label, text) in enumerate(opts):
            label = new_labels[i]
            new_q[label.lower()] = text
            if text == correct_text:
                new_correct = label
        new_q["correct"] = new_correct
        result.append(new_q)
    return result


def is_admin_or_owner(chat_member) -> bool:
    """Check if a ChatMember has admin or owner privileges."""
    return isinstance(chat_member, (ChatMemberAdministrator, ChatMemberOwner))


# ═══════════════════════════════════════════════════════════════════
#  USER AUTH HELPERS
# ═══════════════════════════════════════════════════════════════════

async def ensure_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Register user if new, return False if banned."""
    user = update.effective_user
    if not user:
        return False
    await db.get_or_create_user(user.id, user.username or "", user.first_name or "")
    if await db.is_banned(user.id):
        await update.effective_message.reply_text("⛔ You are banned from using this bot.")
        return False
    return True


async def require_credits(update: Update, user_id: int, cost: int = CREDIT_COST_PER_QUIZ) -> bool:
    """Check and deduct a credit. Warns user if insufficient."""
    if await db.deduct_credit(user_id, cost):
        return True
    config = await db.get_admin_config()
    msg = (
        "❌ *Insufficient Credits!*\n\n"
        f"You need {cost} credit(s) for this action.\n"
        f"Contact the owner: {config['owner_contact']}\n"
        f"Join: {config['channel_plans']}"
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    return False


async def build_help_text() -> str:
    """Build comprehensive help message from dynamic config."""
    config = await db.get_admin_config()
    return f"""
🧠 *Quiz Bot - Complete Guide*

*📝 Creating Quizzes*
1. `/create` - Start quiz creation wizard
2. Choose method: AI prompt, PDF upload, or Image quiz
3. Select category: {', '.join(CATEGORIES)}
4. Each quiz costs {CREDIT_COST_PER_QUIZ} credit(s)

*🎮 Playing Quizzes*
• In groups: Only admins can run `/play <QuizCode>`
• Private: Use `/play <QuizCode>` directly
• Questions & options are shuffled every time!

*💰 Credit System*
• New users get {FREE_TRIAL_CREDITS} free credits
• Each quiz creation = {CREDIT_COST_PER_QUIZ} credit
• Run out? Contact {config['owner_contact']}
• Plans: {config['channel_plans']}

*🏆 Leaderboard*
• `/leaderboard <code>` - Quiz-specific rankings
• `/mystats` - Your performance stats

*📋 Other Commands*
`/help` - This guide
`/myquizzes` - Your created quizzes
`/credits` - Check your balance
`/admin` - Owner panel (password protected)

*👥 Group Play*
• Admins only: `/play <QuizCode>` in groups
• Each question has {QUIZ_TIME_LIMIT_SECONDS}s timer
• Real-time scoreboard after quiz
"""


# ═══════════════════════════════════════════════════════════════════
#  BASIC COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message."""
    if not await ensure_user(update, context):
        return
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Welcome *{user.first_name}*!\n\n"
        f"🎯 I'm an advanced Quiz Bot.\n"
        f"✅ You received {FREE_TRIAL_CREDITS} free credits to start!\n\n"
        f"Use /help to see all commands.",
        parse_mode=ParseMode.MARKDOWN
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show comprehensive help guide."""
    if not await ensure_user(update, context):
        return
    text = await build_help_text()
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check user's credit balance."""
    if not await ensure_user(update, context):
        return
    user_id = update.effective_user.id
    bal = await db.get_user_credits(user_id)
    await update.message.reply_text(
        f"💰 *Your Credit Balance:* `{bal}`\n\n"
        f"Each quiz creation costs {CREDIT_COST_PER_QUIZ} credit(s).",
        parse_mode=ParseMode.MARKDOWN
    )


async def my_quizzes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List quizzes created by the user."""
    if not await ensure_user(update, context):
        return
    user_id = update.effective_user.id
    quizzes = await db.get_user_quizzes(user_id)
    if not quizzes:
        await update.message.reply_text("You haven't created any quizzes yet. Use /create to start!")
        return
    lines = ["📚 *Your Quizzes:*\n"]
    for q in quizzes[:15]:
        lines.append(f"`{q['quiz_code']}` — {q['title']} ({q['category']}) — {q['play_count']} plays")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's overall statistics."""
    if not await ensure_user(update, context):
        return
    user = await db.get_user(update.effective_user.id)
    total_q = user['total_quizzes_played']
    total_c = user['total_correct']
    accuracy = (total_c / max(total_q * 5, 1)) * 100
    await update.message.reply_text(
        f"📊 *Your Stats*\n\n"
        f"Quizzes Played: {total_q}\n"
        f"Correct Answers: {total_c}\n"
        f"Accuracy: {accuracy:.1f}%\n"
        f"Credits: {user['credits']}",
        parse_mode=ParseMode.MARKDOWN
    )


# ═══════════════════════════════════════════════════════════════════
#  GLOBAL LEADERBOARD
# ═══════════════════════════════════════════════════════════════════

async def global_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show global top performers."""
    if not await ensure_user(update, context):
        return
    top = await db.get_global_leaderboard(10)
    if not top:
        await update.message.reply_text("No one has played any quizzes yet!")
        return
    lines = ["🌍 *Global Leaderboard*\n"]
    for i, row in enumerate(top, 1):
        name = row['first_name'] or row['username'] or f"User{row['user_id']}"
        lines.append(f"{i}. {name} — {row['total_correct']} correct ({row['total_quizzes_played']} quizzes)")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ═══════════════════════════════════════════════════════════════════
#  /CREATE - QUIZ CREATION WIZARD
# ═══════════════════════════════════════════════════════════════════

async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the quiz creation wizard."""
    if not await ensure_user(update, context):
        return ConversationHandler.END
    user_id = update.effective_user.id
    credits = await db.get_user_credits(user_id)
    if credits < CREDIT_COST_PER_QUIZ:
        config = await db.get_admin_config()
        await update.message.reply_text(
            f"❌ You need at least {CREDIT_COST_PER_QUIZ} credit(s).\n"
            f"Contact: {config['owner_contact']}",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("🤖 AI Text Prompt", callback_data="create_ai")],
        [InlineKeyboardButton("📄 PDF/Document", callback_data="create_pdf")],
        [InlineKeyboardButton("🖼 Image Quiz", callback_data="create_image")],
        [InlineKeyboardButton("❌ Cancel", callback_data="create_cancel")]
    ]
    await update.message.reply_text(
        "📝 *How would you like to create a quiz?*\n\n"
        f"Cost: {CREDIT_COST_PER_QUIZ} credit(s)\n"
        f"Your balance: {credits} credits",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return AWAIT_AI_PROMPT


async def create_method_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle creation method selection."""
    query = update.callback_query
    await query.answer()
    if query.data == "create_cancel":
        await query.edit_message_text("❌ Creation cancelled.")
        return ConversationHandler.END

    user_id = query.from_user.id
    method = query.data
    user_sessions[user_id] = {"method": method}

    if method == "create_ai":
        await query.edit_message_text(
            "✍️ *AI Prompt Method*\n\n"
            "Send me a prompt describing the quiz you want.\n\n"
            "Example: *'Create 5 science questions about the solar system'*\n\n"
            "I'll generate a formatted quiz from your description.\n"
            "Send /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN
        )
        return AWAIT_AI_PROMPT

    elif method == "create_pdf":
        await query.edit_message_text(
            "📄 *PDF Upload Method*\n\n"
            "Upload a PDF file containing quiz questions.\n\n"
            "Format each question like:\n"
            "
http://googleusercontent.com/immersive_entry_chip/0

अब आप इस पूरे कोड को कॉपी करके अपनी `quiz_bot.py` फ़ाइल में सेव (replace) कर दीजिए, फिर गिटहब पर कमिट करके रेंडर (Render) पर चेक कीजिये। डिप्लॉयमेंट एकदम सक्सेसफुल हो जाएगा!
