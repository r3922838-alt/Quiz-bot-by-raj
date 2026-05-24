"""
Database Module - SQLite abstraction with efficient indexing.
Manages Users, Quizzes, Questions, Admin Config, and Leaderboard.
"""
import sqlite3
import asyncio
from typing import Optional, List, Tuple, Dict, Any
from datetime import datetime, timedelta
import random
import string

from config import DATABASE_PATH, FREE_TRIAL_CREDITS, CATEGORIES


class DatabasePool:
    """
    Thread-safe SQLite connection pool using a single connection
    with write-lock serialization for SQLite's simplicity.
    """

    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    async def connect(self):
        """Initialize database and create tables."""
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._create_tables()

    async def _create_tables(self):
        """Define and migrate the database schema."""
        cursor = self._conn.cursor()

        # ── Users Table ──────────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY,
                username      TEXT,
                first_name    TEXT,
                credits       INTEGER NOT NULL DEFAULT ?,
                joined_at     TEXT NOT NULL DEFAULT (datetime('now')),
                is_banned     INTEGER NOT NULL DEFAULT 0,
                total_quizzes_played INTEGER NOT NULL DEFAULT 0,
                total_correct     INTEGER NOT NULL DEFAULT 0
            )
        """, (FREE_TRIAL_CREDITS,))

        # ── Admin Config Table (singleton row) ──────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_config (
                id               INTEGER PRIMARY KEY CHECK (id = 1),
                owner_contact    TEXT NOT NULL DEFAULT '@Owner',
                channel_plans    TEXT NOT NULL DEFAULT 'https://t.me/PlansChannel',
                channel_updates  TEXT NOT NULL DEFAULT 'https://t.me/UpdatesChannel',
                channel_support  TEXT NOT NULL DEFAULT 'https://t.me/SupportGroup',
                force_sub_channel TEXT NOT NULL DEFAULT 'https://t.me/ForceSubChannel'
            )
        """)
        # Ensure singleton row exists
        cursor.execute("""
            INSERT OR IGNORE INTO admin_config (id, owner_contact)
            VALUES (1, '@Owner')
        """)

        # ── Quizzes Table ───────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS quizzes (
                quiz_code     TEXT PRIMARY KEY,
                creator_id    INTEGER NOT NULL,
                category      TEXT NOT NULL CHECK (category IN ('Science','Arts','Commerce','General')),
                title         TEXT NOT NULL,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                is_active     INTEGER NOT NULL DEFAULT 1,
                play_count    INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (creator_id) REFERENCES users(user_id)
            )
        """)

        # ── Questions Table ─────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_code   TEXT NOT NULL,
                question_text TEXT NOT NULL,
                option_a    TEXT NOT NULL,
                option_b    TEXT NOT NULL,
                option_c    TEXT NOT NULL,
                option_d    TEXT NOT NULL,
                correct     TEXT NOT NULL CHECK (correct IN ('A','B','C','D')),
                image_path  TEXT,
                FOREIGN KEY (quiz_code) REFERENCES quizzes(quiz_code)
            )
        """)

        # ── Leaderboard / Scores Table ─────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scores (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                quiz_code   TEXT NOT NULL,
                score       INTEGER NOT NULL DEFAULT 0,
                total       INTEGER NOT NULL DEFAULT 0,
                played_at   TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (quiz_code) REFERENCES quizzes(quiz_code)
            )
        """)

        # ── Indexes for performance ─────────────────────────────────
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_quizzes_creator ON quizzes(creator_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_quizzes_category ON quizzes(category)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_questions_quiz ON questions(quiz_code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_scores_user ON scores(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_scores_quiz ON scores(quiz_code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_scores_date ON scores(played_at)")

        self._conn.commit()

    # ── User Methods ────────────────────────────────────────────────

    async def get_or_create_user(self, user_id: int, username: str = "",
                                  first_name: str = "") -> sqlite3.Row:
        """Fetch existing user or register a new one with free credits."""
        async with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row is None:
                cursor.execute("""
                    INSERT INTO users (user_id, username, first_name, credits)
                    VALUES (?, ?, ?, ?)
                """, (user_id, username or "", first_name or "", FREE_TRIAL_CREDITS))
                self._conn.commit()
                cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                row = cursor.fetchone()
            else:
                # Update username/name if changed
                if username and row["username"] != username:
                    cursor.execute("UPDATE users SET username = ? WHERE user_id = ?",
                                   (username, user_id))
                    self._conn.commit()
            return row

    async def get_user(self, user_id: int) -> Optional[sqlite3.Row]:
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return cursor.fetchone()

    async def deduct_credit(self, user_id: int, amount: int = 1) -> bool:
        """Deduct credits. Returns True if successful, False if insufficient."""
        async with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row is None or row["credits"] < amount:
                return False
            cursor.execute("UPDATE users SET credits = credits - ? WHERE user_id = ?",
                           (amount, user_id))
            self._conn.commit()
            return True

    async def add_credits(self, user_id: int, amount: int):
        async with self._lock:
            self._conn.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?",
                               (amount, user_id))
            self._conn.commit()

    async def set_credits(self, user_id: int, amount: int):
        async with self._lock:
            self._conn.execute("UPDATE users SET credits = ? WHERE user_id = ?",
                               (amount, user_id))
            self._conn.commit()

    async def get_user_credits(self, user_id: int) -> int:
        cursor = self._conn.cursor()
        cursor.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row["credits"] if row else 0

    # ── Quiz Methods ────────────────────────────────────────────────

    @staticmethod
    def generate_quiz_code() -> str:
        """Generate a short unique quiz code like #QZ7A3B."""
        chars = string.ascii_uppercase + string.digits
        suffix = ''.join(random.choices(chars, k=5))
        return f"{QUIZ_CODE_PREFIX}{suffix}"

    async def create_quiz(self, creator_id: int, category: str, title: str,
                          questions: List[Dict]) -> Optional[str]:
        """
        Create a quiz with questions.
        Returns the quiz code or None if category invalid.
        """
        if category not in CATEGORIES:
            return None

        quiz_code = self.generate_quiz_code()

        async with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("""
                INSERT INTO quizzes (quiz_code, creator_id, category, title)
                VALUES (?, ?, ?, ?)
            """, (quiz_code, creator_id, category, title))

            for q in questions:
                cursor.execute("""
                    INSERT INTO questions (quiz_code, question_text,
                        option_a, option_b, option_c, option_d, correct, image_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    quiz_code,
                    q["question"],
                    q["a"], q["b"], q["c"], q["d"],
                    q["correct"].upper(),
                    q.get("image_path")
                ))

            self._conn.commit()
        return quiz_code

    async def get_quiz(self, quiz_code: str) -> Optional[sqlite3.Row]:
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM quizzes WHERE quiz_code = ?", (quiz_code,))
        return cursor.fetchone()

    async def get_quiz_questions(self, quiz_code: str) -> List[sqlite3.Row]:
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM questions WHERE quiz_code = ? ORDER BY id", (quiz_code,))
        return cursor.fetchall()

    async def increment_play_count(self, quiz_code: str):
        async with self._lock:
            self._conn.execute("UPDATE quizzes SET play_count = play_count + 1 WHERE quiz_code = ?",
                               (quiz_code,))
            self._conn.commit()

    async def get_user_quizzes(self, user_id: int) -> List[sqlite3.Row]:
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM quizzes WHERE creator_id = ? ORDER BY created_at DESC",
                       (user_id,))
        return cursor.fetchall()

    # ── Score / Leaderboard Methods ─────────────────────────────────

    async def record_score(self, user_id: int, quiz_code: str, score: int, total: int):
        async with self._lock:
            self._conn.execute("""
                INSERT INTO scores (user_id, quiz_code, score, total) VALUES (?, ?, ?, ?)
            """, (user_id, quiz_code, score, total))
            self._conn.execute("""
                UPDATE users SET total_quizzes_played = total_quizzes_played + 1,
                                 total_correct = total_correct + ?
                WHERE user_id = ?
            """, (score, user_id))
            self._conn.commit()

    async def get_leaderboard(self, quiz_code: str, limit: int = 10) -> List[sqlite3.Row]:
        cursor = self._conn.cursor()
        cursor.execute("""
            SELECT s.user_id, u.username, u.first_name, s.score, s.total, s.played_at
            FROM scores s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.quiz_code = ?
            ORDER BY s.score DESC, s.played_at ASC
            LIMIT ?
        """, (quiz_code, limit))
        return cursor.fetchall()

    async def get_global_leaderboard(self, limit: int = 10) -> List[sqlite3.Row]:
        cursor = self._conn.cursor()
        cursor.execute("""
            SELECT user_id, username, first_name,
                   total_quizzes_played, total_correct
            FROM users
            WHERE total_quizzes_played > 0
            ORDER BY total_correct DESC, total_quizzes_played ASC
            LIMIT ?
        """, (limit,))
        return cursor.fetchall()

    # ── Admin Config Methods ────────────────────────────────────────

    async def get_admin_config(self) -> sqlite3.Row:
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM admin_config WHERE id = 1")
        return cursor.fetchone()

    async def update_admin_config(self, **kwargs):
        """Dynamically update admin config fields."""
        allowed = {"owner_contact", "channel_plans", "channel_updates",
                   "channel_support", "force_sub_channel"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        async with self._lock:
            self._conn.execute(f"UPDATE admin_config SET {set_clause} WHERE id = 1", values)
            self._conn.commit()

    # ── Ban / Utility ───────────────────────────────────────────────

    async def is_banned(self, user_id: int) -> bool:
        cursor = self._conn.cursor()
        cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row is not None and row["is_banned"] == 1

    async def set_ban(self, user_id: int, banned: bool):
        async with self._lock:
            self._conn.execute("UPDATE users SET is_banned = ? WHERE user_id = ?",
                               (1 if banned else 0, user_id))
            self._conn.commit()

    async def close(self):
        if self._conn:
            self._conn.close()


# Singleton instance
db = DatabasePool()
