"""
Configuration Module - Centralized settings for the Telegram Quiz Bot.
All tunable parameters, tokens, and constants live here.
"""
import os

# ─── BOT CREDENTIALS ───────────────────────────────────────────────
BOT_TOKEN = os.getenv("QUIZ_BOT_TOKEN", "8861233667:AAFxYRKUkv2MUTFdflGpiWK8HnRbOaDdQe4")

# ─── ADMIN AUTH ────────────────────────────────────────────────────
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "thakur8888")
OWNER_ID = int(os.getenv("OWNER_ID", "1234567890"))  # Set your Telegram user ID here

# ─── DATABASE ──────────────────────────────────────────────────────
DATABASE_PATH = os.getenv("DATABASE_PATH", "quiz_bot.db")

# ─── CATEGORIES ────────────────────────────────────────────────────
CATEGORIES = ["Science", "Arts", "Commerce", "General"]

# ─── CREDITS / MONETIZATION ────────────────────────────────────────
FREE_TRIAL_CREDITS = 20
CREDIT_COST_PER_QUIZ = 1

# ─── QUIZ SETTINGS ─────────────────────────────────────────────────
QUIZ_CODE_PREFIX = "#QZ"
QUIZ_TIME_LIMIT_SECONDS = 30          # Seconds per question in group play
QUESTIONS_PER_QUIZ_DEFAULT = 5

# ─── IMAGE PROCESSING ──────────────────────────────────────────────
IMAGE_MAX_WIDTH = 800
IMAGE_QUALITY = 55  # 0-100, lower = smaller file

# ─── FILE PATHS ────────────────────────────────────────────────────
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
