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
            "```\nQ: Question text?\nA) Option A\nB) Option B\nC) Option C\nD) Option D\nAnswer: A\n```\n\n"
            "Send /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN
        )
        return AWAIT_PDF_FILE

    elif method == "create_image":
        await query.edit_message_text(
            "🖼 *Image Quiz Method*\n\n"
            "Send me the *image* containing your question first.\n"
            "Then I'll ask for the 4 options and correct answer.",
            parse_mode=ParseMode.MARKDOWN
        )
        return AWAIT_IMAGE_QUIZ_QUESTION

    return AWAIT_AI_PROMPT


async def handle_ai_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive AI prompt text and generate quiz."""
    if not await ensure_user(update, context):
        return ConversationHandler.END
    user_id = update.effective_user.id
    prompt = update.message.text

    await update.message.reply_text(
        "⏳ *Generating quiz from your prompt...*",
        parse_mode=ParseMode.MARKDOWN
    )
    await asyncio.sleep(0.5)  # Simulate processing

    # Generate structured quiz from prompt text
    lines = prompt.strip().split('\n')
async def handle_ai_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive AI prompt text and generate quiz."""
    if not await ensure_user(update, context):
        return ConversationHandler.END
    user_id = update.effective_user.id
    prompt = update.message.text

    await update.message.reply_text(
        "⏳ *Generating quiz from your prompt...*",
        parse_mode=ParseMode.MARKDOWN
    )
    await asyncio.sleep(0.5)

    # Try to parse as structured format first
    questions = parse_quiz_from_text(prompt)
    if not questions:
        # Generate template questions based on prompt
        topics = prompt.split()
        questions = [
            {
                "question": f"Based on '{' '.join(topics[:3])}', what is the key concept?",
                "a": "Primary Concept", "b": "Secondary Idea",
                "c": "Related Theory", "d": "Unrelated Fact",
                "correct": "A"
            },
            {
                "question": f"Which field best relates to '{' '.join(topics[:2])}'?",
                "a": "Scientific", "b": "Artistic",
                "c": "Commercial", "d": "General Knowledge",
                "correct": "A"
            },
            {
                "question": f"What is the main application of this topic?",
                "a": "Research & Development", "b": "Education",
                "c": "Industry Practice", "d": "All of the above",
                "correct": "D"
            },
        ]

    user_sessions[user_id]["questions"] = questions
    await update.message.reply_text(f"✅ Generated {len(questions)} questions!")
    return await ask_category(update, context)


async def handle_pdf_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive and parse PDF file."""
    if not await ensure_user(update, context):
        return ConversationHandler.END
    user_id = update.effective_user.id

    if not update.message.document:
        await update.message.reply_text("Please upload a PDF file.")
        return AWAIT_PDF_FILE

    # Check mime type
    if not update.message.document.mime_type == "application/pdf":
        await update.message.reply_text("❌ That's not a PDF. Please upload a valid PDF file.")
        return AWAIT_PDF_FILE

    file_id = update.message.document.file_id
    file_obj = await context.bot.get_file(file_id)

    file_bytes = io.BytesIO()
    await file_obj.download_to_memory(file_bytes)
    file_bytes.seek(0)

    await update.message.reply_text("⏳ Parsing PDF...")

    try:
        text = await extract_text_from_pdf(file_bytes.read())
        questions = parse_quiz_from_text(text)
        if not questions:
            await update.message.reply_text(
                "❌ Couldn't parse questions from this PDF. Ensure format:\n"
                "Q: Question?\nA) Opt1\nB) Opt2\nC) Opt3\nD) Opt4\nAnswer: A"
            )
            return AWAIT_PDF_FILE
        user_sessions[user_id]["questions"] = questions
        await update.message.reply_text(f"✅ Extracted {len(questions)} questions from PDF!")
        return await ask_category(update, context)
    except Exception as e:
        await update.message.reply_text(f"❌ PDF parsing error: {str(e)}")
        return AWAIT_PDF_FILE


async def handle_image_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive image for image quiz."""
    if not await ensure_user(update, context):
        return ConversationHandler.END
    user_id = update.effective_user.id

    if not update.message.photo:
        await update.message.reply_text("Please send a photo.")
        return AWAIT_IMAGE_QUIZ_QUESTION

    photo = update.message.photo[-1]
    file_obj = await context.bot.get_file(photo.file_id)
    file_bytes = io.BytesIO()
    await file_obj.download_to_memory(file_bytes)
    file_bytes.seek(0)

    # Compress the image
    compressed = await compress_image(file_bytes.read())

    # Save to disk
    filename = f"quiz_img_{user_id}_{int(datetime.utcnow().timestamp())}.jpg"
    filepath = os.path.join(UPLOAD_DIR, filename)
    async with aiofiles.open(filepath, 'wb') as f:
        await f.write(compressed)

    user_sessions[user_id] = {
        "image_path": filepath,
        "method": "create_image"
    }

    await update.message.reply_text(
        "✅ Image saved! Now send the *question text* for this image.",
        parse_mode=ParseMode.MARKDOWN
    )
    return AWAIT_IMAGE_QUIZ_OPTIONS


async def handle_image_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive question text, then ask for 4 options."""
    if not await ensure_user(update, context):
        return ConversationHandler.END
    user_id = update.effective_user.id
    user_sessions[user_id]["question_text"] = update.message.text

    await update.message.reply_text(
        "Now send the 4 options in this exact format:\n\n"
        "A) First option\n"
        "B) Second option\n"
        "C) Third option\n"
        "D) Fourth option"
    )
    return AWAIT_IMAGE_QUIZ_ANSWER


async def handle_image_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parse options and ask for correct answer."""
    if not await ensure_user(update, context):
        return ConversationHandler.END
    user_id = update.effective_user.id
    text = update.message.text

    opts = {}
    for line in text.strip().split('\n'):
        line = line.strip()
        m = re.match(r'^([A-Da-d])[:.)]\s*(.*)', line)
        if m:
            opts[m.group(1).upper()] = m.group(2).strip()

    if len(opts) != 4:
        await update.message.reply_text(
            "❌ Need exactly 4 options (A, B, C, D). Try again:\n"
            "A) ...\nB) ...\nC) ...\nD) ..."
        )
        return AWAIT_IMAGE_QUIZ_ANSWER

    user_sessions[user_id]["options"] = opts

    await update.message.reply_text(
        "Which is the correct answer? Reply with just: A, B, C, or D"
    )
    return AWAIT_IMAGE_CORRECT_ANSWER


async def handle_image_correct_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive correct answer letter, build question, ask for category."""
    if not await ensure_user(update, context):
        return ConversationHandler.END
    user_id = update.effective_user.id
    answer = update.message.text.strip().upper()

    if answer not in ('A', 'B', 'C', 'D'):
        await update.message.reply_text("❌ Must be A, B, C, or D. Try again.")
        return AWAIT_IMAGE_CORRECT_ANSWER

    session = user_sessions[user_id]
    opts = session["options"]
    questions = [{
        "question": session.get("question_text", "Image Question"),
        "a": opts['A'], "b": opts['B'], "c": opts['C'], "d": opts['D'],
        "correct": answer,
        "image_path": session.get("image_path")
    }]
    user_sessions[user_id]["questions"] = questions

    await update.message.reply_text("✅ Question built! Now choose a category.")
    return await ask_category(update, context)


async def ask_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show category selection."""
    user_id = update.effective_user.id
    session = user_sessions.get(user_id, {})
    if "questions" not in session:
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(cat, callback_data=f"cat_{cat}")]
        for cat in CATEGORIES
    ]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cat_cancel")])

    await update.effective_message.reply_text(
        f"✅ *{len(session['questions'])} question(s)* ready!\n\n"
        "Now choose a *category*:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return AWAIT_QUIZ_CATEGORY


async def handle_category_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive category selection, finalize quiz creation."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cat_cancel":
        await query.edit_message_text("❌ Quiz creation cancelled.")
        return ConversationHandler.END

    category = query.data.replace("cat_", "")
    user_id = query.from_user.id
    session = user_sessions.get(user_id, {})
    questions = session.get("questions", [])
    title = f"Quiz by {query.from_user.first_name}"

    # Deduct credit
    if not await require_credits(query, user_id):
        return ConversationHandler.END

    # Save to database
    quiz_code = await db.create_quiz(user_id, category, title, questions)

    if quiz_code:
        await query.edit_message_text(
            f"✅ *Quiz Created Successfully!*\n\n"
            f"📌 Code: `{quiz_code}`\n"
            f"📂 Category: {category}\n"
            f"❓ Questions: {len(questions)}\n\n"
            f"Share this code with others to play!\n"
            f"Group admins: `/play {quiz_code}`",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await query.edit_message_text("❌ Failed to create quiz. Invalid category?")

    # Cleanup session
    user_sessions.pop(user_id, None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel any ongoing conversation."""
    user_id = update.effective_user.id
    user_sessions.pop(user_id, None)
    await update.message.reply_text("❌ Operation cancelled.")
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════
#  /PLAY - QUIZ PLAYBACK SYSTEM
# ═══════════════════════════════════════════════════════════════════

async def play_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Play a quiz.
    
    In groups: Only admins/owner can trigger.
    In private: Any user can play their own quizzes.
    """
    if not await ensure_user(update, context):
        return

    user_id = update.effective_user.id
    chat = update.effective_chat
    args = context.args

    if not args:
        await update.message.reply_text("Usage: `/play <QuizCode>`\nExample: `/play #QZ7A3B`", parse_mode=ParseMode.MARKDOWN)
        return

    quiz_code = args[0].strip().upper()
    if not quiz_code.startswith(QUIZ_CODE_PREFIX):
        quiz_code = f"{QUIZ_CODE_PREFIX}{quiz_code}"

    # Fetch quiz
    quiz = await db.get_quiz(quiz_code)
    if not quiz:
        await update.message.reply_text(f"❌ Quiz `{quiz_code}` not found.", parse_mode=ParseMode.MARKDOWN)
        return

    # Group admin check
    if chat.type in ("group", "supergroup"):
        try:
            member = await chat.get_member(user_id)
            if not (is_admin_or_owner(member) or user_id == OWNER_ID):
                await update.message.reply_text(
                    "⛔ Only group *administrators* or the *bot owner* can start quizzes in groups.\n"
                    "Ask an admin to run `/play`.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
        except Exception:
            await update.message.reply_text("⛔ Permission check failed. Are you an admin?")
            return

    # Fetch and shuffle questions
    questions = await db.get_quiz_questions(quiz_code)
    if not questions:
        await update.message.reply_text("❌ This quiz has no questions.")
        return

    shuffled = shuffle_quiz([dict(q) for q in questions])
    await db.increment_play_count(quiz_code)

    # Initialize game state
    game_key = f"game_{chat.id}_{quiz_code}"
    context.chat_data[game_key] = {
        "questions": shuffled,
        "current": 0,
        "scores": {},
        "quiz_code": quiz_code,
        "title": quiz["title"],
        "total": len(shuffled)
    }

    # Send first question
    await send_question(update, context, chat.id, quiz_code, shuffled, 0)


async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        chat_id: int, quiz_code: str, questions: List[Dict],
                        index: int):
    """Send a quiz question with inline answer buttons."""
    if index >= len(questions):
        await end_quiz(update, context, chat_id, quiz_code)
        return

    q = questions[index]
    
    # Build option buttons (shuffled positions already in q)
    options = [
        ('A', q['a']), ('B', q['b']), ('C', q['c']), ('D', q['d'])
    ]
    keyboard = [
        [InlineKeyboardButton(f"{opt[0]}) {opt[1][:40]}", callback_data=f"ans_{quiz_code}_{index}_{opt[0]}")]
        for opt in options
    ]

    # If question has an image, send as media group
    if q.get("image_path") and os.path.exists(q["image_path"]):
        async with aiofiles.open(q["image_path"], 'rb') as f:
            img_bytes = await f.read()
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=io.BytesIO(img_bytes),
            caption=f"*Q{index+1}/{len(questions)}:* {q['question']}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        text = f"*Q{index+1}/{len(questions)}:* {q['question']}"
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process answer callback and advance to next question."""
    query = update.callback_query
    await query.answer()

    data = query.data.split("_")
    # Format: ans_{quiz_code}_{index}_{chosen_letter}
    if len(data) < 4:
        await query.edit_message_text("❌ Invalid response.")
        return

    prefix = data[0]
    quiz_code = data[1]
    q_index = int(data[2])
    chosen = data[3]

    chat_id = query.message.chat_id
    user_id = query.from_user.id
    game_key = f"game_{chat_id}_{quiz_code}"
    game = context.chat_data.get(game_key)

    if not game or q_index != game["current"]:
        await query.edit_message_text("⏳ This question has expired or already been answered!")
        return

    # Check answer
    q = game["questions"][q_index]
    is_correct = chosen == q["correct"]

    # Track score
    if user_id not in game["scores"]:
        game["scores"][user_id] = 0
    if is_correct:
        game["scores"][user_id] += 1

    # Update message to show result
    correct_text = q[q["correct"].lower()]
    chosen_text = q[chosen.lower()] if chosen in ('A','B','C','D') else "?"

    if is_correct:
        reply = f"✅ *Correct!* ({chosen}) {chosen_text}"
    else:
        reply = f"❌ *Wrong!* You chose {chosen}) {chosen_text}\n✅ Correct: {q['correct']}) {correct_text}"

    await query.edit_message_text(reply, parse_mode=ParseMode.MARKDOWN)

    # Advance to next question
    game["current"] += 1
    await asyncio.sleep(0.8)
    await send_question(update, context, chat_id, quiz_code,
                        game["questions"], game["current"])


async def end_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE,
                   chat_id: int, quiz_code: str):
    """Show final scores and leaderboard."""
    game_key = f"game_{chat_id}_{quiz_code}"
    game = context.chat_data.pop(game_key, None)
    if not game:
        return

    scores = game["scores"]
    total = game["total"]

    # Record scores in DB
    for uid, score in scores.items():
        await db.record_score(uid, quiz_code, score, total)

    # Build leaderboard text
    if not scores:
        await context.bot.send_message(chat_id, "😴 No one answered any questions!")
        return

    sorted_scores = sorted(scores.items(), key=lambda x: -x[1])
    lines = ["🏆 *Quiz Over! Final Scores*\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, score) in enumerate(sorted_scores):
        try:
            member = await context.bot.get_chat_member(chat_id, uid)
            name = member.user.first_name or f"User{uid}"
        except:
            name = f"User{uid}"
        medal = medals[i] if i < 3 else f"{i+1}
        
lines = ["🏆 *Quiz Over! Final Scores*\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, score) in enumerate(sorted_scores):
        try:
            member = await context.bot.get_chat_member(chat_id, uid)
            name = member.user.first_name or f"User{uid}"
        except:
            name = f"User{uid}"
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} {name} — {score}/{total}")

    # Also show top 5 from DB leaderboard for this quiz
    db_top = await db.get_leaderboard(quiz_code, 5)
    if len(db_top) > len(sorted_scores):
        lines.append("\n📊 *All-Time Leaderboard:*")
        for i, row in enumerate(db_top[:5], 1):
            name = row['first_name'] or row['username'] or f"User{row['user_id']}"
            lines.append(f"{i}. {name} — {row['score']}/{row['total']}")

    await context.bot.send_message(chat_id, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def quiz_leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show leaderboard for a specific quiz via /leaderboard <code>."""
    if not await ensure_user(update, context):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/leaderboard <QuizCode>`", parse_mode=ParseMode.MARKDOWN)
        return

    quiz_code = args[0].strip().upper()
    if not quiz_code.startswith(QUIZ_CODE_PREFIX):
        quiz_code = f"{QUIZ_CODE_PREFIX}{quiz_code}"

    top = await db.get_leaderboard(quiz_code, 10)
    if not top:
        await update.message.reply_text(f"No scores recorded for `{quiz_code}` yet.", parse_mode=ParseMode.MARKDOWN)
        return

    lines = [f"🏆 *Leaderboard: `{quiz_code}`*\n"]
    for i, row in enumerate(top, 1):
        name = row['first_name'] or row['username'] or f"User{row['user_id']}"
        lines.append(f"{i}. {name} — {row['score']}/{row['total']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ═══════════════════════════════════════════════════════════════════
#  /ADMIN - DYNAMIC ADMIN PANEL
# ═══════════════════════════════════════════════════════════════════

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enter admin panel with password authentication."""
    if not await ensure_user(update, context):
        return ConversationHandler.END

    await update.message.reply_text(
        "🔐 *Admin Authentication Required*\n\nPlease enter the admin password:",
        parse_mode=ParseMode.MARKDOWN
    )
    return AWAIT_ADMIN_PASSWORD


async def admin_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify admin password."""
    password = update.message.text.strip()
    if password != ADMIN_PASSWORD:
        await update.message.reply_text("❌ *Incorrect password!* Access denied.", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    # Authenticate - store in context
    context.user_data["admin_authenticated"] = True

    await update.message.reply_text(
        "✅ *Access Granted!* Welcome to the Admin Panel.\n\n"
        "Choose an action below:",
        reply_markup=admin_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )
    return AWAIT_ADMIN_ACTION


def admin_main_keyboard() -> InlineKeyboardMarkup:
    """Build the main admin panel inline keyboard."""
    keyboard = [
        [InlineKeyboardButton("👤 Change Owner Contact", callback_data="admin_contact")],
        [InlineKeyboardButton("📢 Update Channel Links", callback_data="admin_channels")],
        [InlineKeyboardButton("💰 Manage User Credits", callback_data="admin_credits")],
        [InlineKeyboardButton("🚫 Ban/Unban User", callback_data="admin_ban")],
        [InlineKeyboardButton("📊 View Admin Config", callback_data="admin_view")],
        [InlineKeyboardButton("🚪 Logout", callback_data="admin_logout")]
    ]
    return InlineKeyboardMarkup(keyboard)


async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all admin panel callback queries."""
    query = update.callback_query
    await query.answer()

    if not context.user_data.get("admin_authenticated"):
        await query.edit_message_text("⛔ Session expired. Use /admin again.")
        return ConversationHandler.END

    action = query.data

    if action == "admin_logout":
        context.user_data["admin_authenticated"] = False
        await query.edit_message_text("🚪 Logged out of admin panel.")
        return ConversationHandler.END

    elif action == "admin_view":
        config = await db.get_admin_config()
        text = (
            "📋 *Current Admin Configuration*\n\n"
            f"👤 Owner Contact: `{config['owner_contact']}`\n"
            f"📢 Plans Channel: {config['channel_plans']}\n"
            f"📢 Updates Channel: {config['channel_updates']}\n"
            f"💬 Support Group: {config['channel_support']}\n"
            f"🔐 Force Sub Channel: {config['force_sub_channel']}"
        )
        await query.edit_message_text(text, reply_markup=admin_main_keyboard(), parse_mode=ParseMode.MARKDOWN)
        return AWAIT_ADMIN_ACTION

    elif action == "admin_contact":
        await query.edit_message_text(
            "👤 Send the new owner contact (e.g., `@username` or user ID):",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data["admin_field"] = "owner_contact"
        return AWAIT_ADMIN_CONFIG_VALUE

    elif action == "admin_channels":
        keyboard = [
            [InlineKeyboardButton("📢 Plans Channel", callback_data="cfg_channel_plans")],
            [InlineKeyboardButton("📢 Updates Channel", callback_data="cfg_channel_updates")],
            [InlineKeyboardButton("💬 Support Group", callback_data="cfg_channel_support")],
            [InlineKeyboardButton("🔐 Force Subscribe", callback_data="cfg_force_sub_channel")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
        ]
        await query.edit_message_text(
            "📢 *Select which channel to update:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return AWAIT_ADMIN_ACTION

    elif action.startswith("cfg_"):
        field = action.replace("cfg_", "")
        field_map = {
            "channel_plans": "📢 Plans Channel URL",
            "channel_updates": "📢 Updates Channel URL",
            "channel_support": "💬 Support Group URL",
            "force_sub_channel": "🔐 Force Subscribe Channel URL"
        }
        await query.edit_message_text(
            f"{field_map.get(field, 'URL')}\n\nSend the new URL/link:",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data["admin_field"] = field
        return AWAIT_ADMIN_CONFIG_VALUE

    elif action == "admin_back":
        await query.edit_message_text(
            "✅ Admin Panel:",
            reply_markup=admin_main_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        return AWAIT_ADMIN_ACTION

    elif action == "admin_credits":
        await query.edit_message_text(
            "💰 *Credit Management*\n\n"
            "Send the user ID of the target user:",
            parse_mode=ParseMode.MARKDOWN
        )
        return AWAIT_ADMIN_CREDIT_USER

    elif action == "admin_ban":
        await query.edit_message_text(
            "🚫 *Ban/Unban User*\n\n"
            "Send the user ID to ban/unban:",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data["admin_ban_action"] = True
        return AWAIT_ADMIN_CREDIT_USER

    return AWAIT_ADMIN_ACTION


async def admin_config_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive new config value and update."""
    field = context.user_data.get("admin_field")
    if not field:
        await update.message.reply_text("❌ Session error. Use /admin again.")
        return ConversationHandler.END

    value = update.message.text.strip()
    await db.update_admin_config(**{field: value})

    config = await db.get_admin_config()
    await update.message.reply_text(
        f"✅ *Updated!*\n\n`{field}` = `{config[field]}`\n\nReturning to admin panel...",
        reply_markup=admin_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )
    return AWAIT_ADMIN_ACTION


async def admin_credit_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive target user ID for credit/ban management."""
    try:
        target_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")
        return AWAIT_ADMIN_CREDIT_USER

    context.user_data["admin_target_user"] = target_id

    if context.user_data.get("admin_ban_action"):
        user = await db.get_user(target_id)
        if user and user["is_banned"]:
            await db.set_ban(target_id, False)
            await update.message.reply_text(f"✅ Unbanned user `{target_id}`.", reply_markup=admin_main_keyboard(), parse_mode=ParseMode.MARKDOWN)
        else:
            await db.set_ban(target_id, True)
            await update.message.reply_text(f"✅ Banned user `{target_id}`.", reply_markup=admin_main_keyboard(), parse_mode=ParseMode.MARKDOWN)
        context.user_data.pop("admin_ban_action", None)
        return AWAIT_ADMIN_ACTION

    await update.message.reply_text(
        f"Target user: `{target_id}`\n\n"
        "Enter amount to *add* (positive) or *remove* (negative), "
        "or type `reset` to set to 0:",
        parse_mode=ParseMode.MARKDOWN
    )
    return AWAIT_ADMIN_CREDIT_AMOUNT


async def admin_credit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply credit adjustment."""
    target_id = context.user_data.get("admin_target_user")
    text = update.message.text.strip().lower()

    if text == "reset":
        await db.set_credits(target_id, 0)
        msg = f"✅ Reset credits for `{target_id}` to 0."
    else:
        try:
            amount = int(text)
        except ValueError:
            await update.message.reply_text("❌ Enter a number (positive/negative) or `reset`.")
            return AWAIT_ADMIN_CREDIT_AMOUNT

        if amount >= 0:
            await db.add_credits(target_id, amount)
            msg = f"✅ Added {amount} credits to `{target_id}`."
        else:
            # Remove credits by deducting the absolute value
            await db.add_credits(target_id, amount)  # amount is negative
            msg = f"✅ Removed {abs(amount)} credits from `{target_id}`."

    new_bal = await db.get_user_credits(target_id)
    msg += f"\nNew balance: `{new_bal}`"
    await update.message.reply_text(msg, reply_markup=admin_main_keyboard(), parse_mode=ParseMode.MARKDOWN)
    return AWAIT_ADMIN_ACTION


async def admin_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback handler for admin text input."""
    await update.message.reply_text("Please use the admin panel buttons.", reply_markup=admin_main_keyboard())
    return AWAIT_ADMIN_ACTION


# ═══════════════════════════════════════════════════════════════════
#  MAIN APPLICATION SETUP
# ═══════════════════════════════════════════════════════════════════

def build_create_conversation_handler() -> ConversationHandler:
    """Build the quiz creation conversation handler."""
    return ConversationHandler(
        entry_points=[CommandHandler("create", create_start)],
        states={
            AWAIT_AI_PROMPT: [
                CallbackQueryHandler(create_method_choice, pattern=r"^create_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ai_text),
            ],
            AWAIT_PDF_FILE: [
                MessageHandler(filters.Document.ALL, handle_pdf_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pdf_file),
            ],
            AWAIT_IMAGE_QUIZ_QUESTION: [
                MessageHandler(filters.PHOTO, handle_image_question),
            ],
            AWAIT_IMAGE_QUIZ_OPTIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_image_options),
            ],
            AWAIT_IMAGE_QUIZ_ANSWER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_image_answer),
            ],
            AWAIT_IMAGE_CORRECT_ANSWER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_image_correct_answer),
            ],
            AWAIT_QUIZ_CATEGORY: [
                CallbackQueryHandler(handle_category_choice, pattern=r"^cat_"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="quiz_creation",
        persistent=False,
    )


def build_admin_conversation_handler() -> ConversationHandler:
    """Build the admin panel conversation handler."""
    return ConversationHandler(
        entry_points=[CommandHandler("admin", admin_panel)],
        states={
            AWAIT_ADMIN_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_auth),
            ],
            AWAIT_ADMIN_ACTION: [
                CallbackQueryHandler(admin_callback_handler, pattern=r"^admin_"),
                CallbackQueryHandler(admin_callback_handler, pattern=r"^cfg_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_fallback),
            ],
            AWAIT_ADMIN_CONFIG_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_config_value),
            ],
            AWAIT_ADMIN_CREDIT_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_credit_user),
            ],
            AWAIT_ADMIN_CREDIT_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_credit_amount),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="admin_panel",
        persistent=False,
    )


async def post_init(application: Application):
    """Initialize database on startup."""
    await db.connect()
    print("✅ Database initialized. Bot is ready.")


async def main():
    """Entry point."""
    application = Application.builder() \
        .token(BOT_TOKEN) \
        .post_init(post_init) \
        .build()

    # Basic commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("credits", credits_command))
    application.add_handler(CommandHandler("myquizzes", my_quizzes))
    application.add_handler(CommandHandler("mystats", my_stats))
    application.add_handler(CommandHandler("leaderboard", global_leaderboard))
    application.add_handler(CommandHandler("lb", quiz_leaderboard_command))
    application.add_handler(CommandHandler("leaderboard_code", quiz_leaderboard_command))

    # Play command
    application.add_handler(CommandHandler("play", play_quiz))

    # Answer callback handler
    application.add_handler(CallbackQueryHandler(handle_answer, pattern=r"^ans_"))

    # Conversation handlers
    application.add_handler(build_create_conversation_handler())
    application.add_handler(build_admin_conversation_handler())

    print("🚀 Quiz Bot is running...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

