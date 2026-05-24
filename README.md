# 🧠 Telegram Quiz Bot — Dual-System Advanced Bot

A production-ready, resource-optimized Telegram Quiz Bot featuring an **Advanced User Ecosystem** and a **Dynamic Admin Panel**. Designed to run smoothly on free-tier cloud hosting.

---

## ✨ Features

### 👤 User Ecosystem
- **4 Categories:** Science, Arts, Commerce, General
- **3 Creation Methods:** AI Text Prompt, PDF/Document Parsing, Image Quiz (with auto-compression)
- **Unique Quiz Codes:** Every quiz gets a short code like `#QZ7A3B`
- **Shuffling:** Questions AND options shuffled every play
- **Group Admin Lock:** Only admins/owner can start `/play` in groups
- **Global & Quiz-specific Leaderboards**

### 💰 Credit System
- 20 free credits on signup
- 1 credit per quiz creation
- Auto-redirect to owner when credits exhausted

### 🛠 Dynamic Admin Panel (`/admin`)
- Update owner contact, channel links (on-the-fly, no restart)
- Manage user credits (add/remove/reset)
- Ban/unban users
- Password protected (`thakur8888`)

### ⚡ Performance Optimized
- SQLite with WAL mode + indexes
- Image compression (Pillow, ~55% quality)
- Fully async (asyncio + python-telegram-bot v20+)
- Minimal memory footprint

---

## 📁 File Structure
