import os
import sqlite3
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# KONFIGURASI
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "baby_care.db")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Jakarta"))

DEFAULT_ASI_MINUTES = int(os.getenv("DEFAULT_ASI_MINUTES", "150"))       # 2 jam 30 menit
DEFAULT_POPOK_MINUTES = int(os.getenv("DEFAULT_POPOK_MINUTES", "240"))   # 4 jam
DEFAULT_SNOOZE_MINUTES = int(os.getenv("DEFAULT_SNOOZE_MINUTES", "15"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =========================
# DATABASE
# =========================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                asi_minutes INTEGER NOT NULL DEFAULT 150,
                popok_minutes INTEGER NOT NULL DEFAULT 240,
                baby_name TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                amount_ml INTEGER,
                started_at TEXT,
                ended_at TEXT,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS states (
                chat_id INTEGER PRIMARY KEY,
                state TEXT,
                updated_at TEXT NOT NULL
            )
        """)


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def iso(dt: datetime) -> str:
    return dt.astimezone(TIMEZONE).isoformat()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(TIMEZONE)


def ensure_chat(chat_id: int):
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO chats (chat_id, created_at, asi_minutes, popok_minutes)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, iso(now_local()), DEFAULT_ASI_MINUTES, DEFAULT_POPOK_MINUTES),
        )


def get_chat(chat_id: int):
    ensure_chat(chat_id)
    with db() as conn:
        return conn.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()


def get_all_chats() -> List[int]:
    with db() as conn:
        rows = conn.execute("SELECT chat_id FROM chats").fetchall()
        return [int(r["chat_id"]) for r in rows]


def add_event(chat_id: int, event_type: str, amount_ml: Optional[int] = None,
              started_at: Optional[datetime] = None, ended_at: Optional[datetime] = None):
    ensure_chat(chat_id)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO events (chat_id, event_type, amount_ml, started_at, ended_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                event_type,
                amount_ml,
                iso(started_at) if started_at else None,
                iso(ended_at) if ended_at else None,
                iso(now_local()),
            ),
        )


def last_event(chat_id: int, event_type: str):
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM events
            WHERE chat_id = ? AND event_type = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (chat_id, event_type),
        ).fetchone()


def today_events(chat_id: int):
    start = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM events
            WHERE chat_id = ?
              AND datetime(created_at) >= datetime(?)
              AND datetime(created_at) < datetime(?)
            ORDER BY created_at ASC
            """,
            (chat_id, iso(start), iso(end)),
        ).fetchall()


def set_state(chat_id: int, state: Optional[str]):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO states (chat_id, state, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at
            """,
            (chat_id, state, iso(now_local())),
        )


def get_state(chat_id: int) -> Optional[str]:
    with db() as conn:
        row = conn.execute("SELECT state FROM states WHERE chat_id = ?", (chat_id,)).fetchone()
        return row["state"] if row else None


def update_interval(chat_id: int, field: str, minutes: int):
    if field not in {"asi_minutes", "popok_minutes"}:
        return
    with db() as conn:
        conn.execute(f"UPDATE chats SET {field} = ? WHERE chat_id = ?", (minutes, chat_id))


def update_baby_name(chat_id: int, name: str):
    with db() as conn:
        conn.execute("UPDATE chats SET baby_name = ? WHERE chat_id = ?", (name, chat_id))


# =========================
# TAMPILAN MENU
# =========================

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍼 Catat Menyusu", callback_data="catat_asi")],
        [InlineKeyboardButton("💩 Catat Ganti Popok", callback_data="catat_popok")],
        [InlineKeyboardButton("🥛 Catat ASIP / Formula", callback_data="menu_susu")],
        [InlineKeyboardButton("😴 Mulai Tidur", callback_data="mulai_tidur"),
         InlineKeyboardButton("☀️ Bangun", callback_data="bangun_tidur")],
        [InlineKeyboardButton("📊 Statistik Hari Ini", callback_data="statistik")],
        [InlineKeyboardButton("👶 Profil Bayi", callback_data="profil"),
         InlineKeyboardButton("⚙️ Pengaturan", callback_data="pengaturan")],
    ])


def reminder_menu(kind: str):
    if kind == "asi":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Sudah Menyusu", callback_data="catat_asi")],
            [InlineKeyboardButton("⏰ Tunda 15 Menit", callback_data="snooze_asi")],
            [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu")],
        ])

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Sudah Ganti Popok", callback_data="catat_popok")],
        [InlineKeyboardButton("⏰ Tunda 15 Menit", callback_data="snooze_popok")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu")],
    ])


def milk_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍼 ASIP 30 ml", callback_data="milk_asip_30"),
         InlineKeyboardButton("🍼 ASIP 60 ml", callback_data="milk_asip_60")],
        [InlineKeyboardButton("🥛 Formula 30 ml", callback_data="milk_formula_30"),
         InlineKeyboardButton("🥛 Formula 60 ml", callback_data="milk_formula_60")],
        [InlineKeyboardButton("✍️ Input Manual ml", callback_data="manual_milk")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu")],
    ])


def settings_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍼 ASI 2 jam", callback_data="set_asi_120"),
         InlineKeyboardButton("🍼 ASI 2,5 jam", callback_data="set_asi_150"),
         InlineKeyboardButton("🍼 ASI 3 jam", callback_data="set_asi_180")],
        [InlineKeyboardButton("💩 Popok 3 jam", callback_data="set_popok_180"),
         InlineKeyboardButton("💩 Popok 4 jam", callback_data="set_popok_240"),
         InlineKeyboardButton("💩 Popok 5 jam", callback_data="set_popok_300")],
        [InlineKeyboardButton("👶 Ubah Nama Bayi", callback_data="ubah_nama")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu")],
    ])


# =========================
# REMINDER JOB
# =========================

def clear_jobs(context: ContextTypes.DEFAULT_TYPE, chat_id: int, name: str):
    for job in context.job_queue.get_jobs_by_name(f"{name}_{chat_id}"):
        job.schedule_removal()


def schedule_once(context: ContextTypes.DEFAULT_TYPE, chat_id: int, name: str, minutes: int):
    clear_jobs(context, chat_id, name)
    context.job_queue.run_once(
        reminder_job,
        when=timedelta(minutes=minutes),
        chat_id=chat_id,
        name=f"{name}_{chat_id}",
        data={"kind": name},
    )


def schedule_at(context: ContextTypes.DEFAULT_TYPE, chat_id: int, name: str, when_dt: datetime):
    clear_jobs(context, chat_id, name)
    delay = max(5, int((when_dt - now_local()).total_seconds()))
    context.job_queue.run_once(
        reminder_job,
        when=delay,
        chat_id=chat_id,
        name=f"{name}_{chat_id}",
        data={"kind": name},
    )


def reschedule_default(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    chat = get_chat(chat_id)
    schedule_once(context, chat_id, "asi", int(chat["asi_minutes"]))
    schedule_once(context, chat_id, "popok", int(chat["popok_minutes"]))


def reschedule_from_last(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    chat = get_chat(chat_id)

    for kind, interval_field in [("asi", "asi_minutes"), ("popok", "popok_minutes")]:
        last = last_event(chat_id, kind)
        interval = int(chat[interval_field])

        if last:
            next_time = parse_dt(last["created_at"]) + timedelta(minutes=interval)
            if next_time <= now_local():
                schedule_once(context, chat_id, kind, 1)
            else:
                schedule_at(context, chat_id, kind, next_time)
        else:
            schedule_once(context, chat_id, kind, interval)


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    kind = context.job.data["kind"]

    if kind == "asi":
        last = last_event(chat_id, "asi")
        last_text = parse_dt(last["created_at"]).strftime("%H:%M") if last else "belum ada catatan"
        text = (
            "🍼 Waktunya cek si kecil ya, Ayah & Bunda ❤️\n\n"
            f"Terakhir menyusu: {last_text}\n\n"
            "Kalau si kecil sudah menunjukkan tanda lapar, waktunya menyusu."
        )
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reminder_menu("asi"))
    else:
        last = last_event(chat_id, "popok")
        last_text = parse_dt(last["created_at"]).strftime("%H:%M") if last else "belum ada catatan"
        text = (
            "💩 Yuk cek popok si kecil.\n\n"
            f"Terakhir ganti popok: {last_text}\n\n"
            "Kalau sudah basah atau penuh, sebaiknya diganti supaya tetap nyaman."
        )
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reminder_menu("popok"))


async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await context.bot.send_message(chat_id=chat_id, text=build_stats(chat_id), reply_markup=main_menu())


def schedule_daily_summary(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    clear_jobs(context, chat_id, "summary")
    context.job_queue.run_daily(
        daily_summary_job,
        time=now_local().replace(hour=21, minute=0, second=0, microsecond=0).timetz(),
        chat_id=chat_id,
        name=f"summary_{chat_id}",
    )


# =========================
# HANDLER COMMAND
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_chat(chat_id)
    reschedule_from_last(context, chat_id)
    schedule_daily_summary(context, chat_id)

    text = (
        "👶 Halo Ayah & Bunda!\n\n"
        "Baby Care Bot aktif ✅\n\n"
        "Aku bantu ingatkan:\n"
        "🍼 Menyusu tiap 2–3 jam\n"
        "💩 Cek/ganti popok tiap beberapa jam\n"
        "😴 Catat tidur si kecil\n"
        "📊 Lihat ringkasan harian\n\n"
        "Tekan tombol di bawah ya."
    )
    await update.message.reply_text(text, reply_markup=main_menu())


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    for job in context.job_queue.jobs():
        if str(chat_id) in job.name:
            job.schedule_removal()

    await update.message.reply_text(
        "Reminder sudah dihentikan ✅\n\n"
        "Ketik /start untuk mengaktifkan lagi."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Perintah:\n"
        "/start - mulai bot\n"
        "/stop - hentikan reminder\n"
        "/menu - tampilkan menu\n"
        "/stats - statistik hari ini\n\n"
        "Tips: tekan tombol setiap selesai menyusui, ganti popok, atau si kecil tidur."
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_chat(update.effective_chat.id)
    await update.message.reply_text("Pilih menu:", reply_markup=main_menu())


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_chat(chat_id)
    await update.message.reply_text(build_stats(chat_id), reply_markup=main_menu())


# =========================
# HANDLER TOMBOL
# =========================

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    ensure_chat(chat_id)
    data = query.data

    if data == "menu":
        await query.edit_message_text("👶 Menu Baby Care Bot", reply_markup=main_menu())
        return

    if data == "catat_asi":
        add_event(chat_id, "asi")
        chat = get_chat(chat_id)
        schedule_once(context, chat_id, "asi", int(chat["asi_minutes"]))
        next_time = now_local() + timedelta(minutes=int(chat["asi_minutes"]))
        await query.edit_message_text(
            f"🎉 Sip, menyusu sudah tercatat!\n\n"
            f"Jam: {now_local().strftime('%H:%M')}\n"
            f"⏰ Aku ingatkan lagi sekitar {next_time.strftime('%H:%M')}.\n\n"
            "Semoga si kecil kenyang ya ❤️",
            reply_markup=main_menu(),
        )
        return

    if data == "catat_popok":
        add_event(chat_id, "popok")
        chat = get_chat(chat_id)
        schedule_once(context, chat_id, "popok", int(chat["popok_minutes"]))
        next_time = now_local() + timedelta(minutes=int(chat["popok_minutes"]))
        await query.edit_message_text(
            f"✅ Popok sudah tercatat.\n\n"
            f"Jam: {now_local().strftime('%H:%M')}\n"
            f"⏰ Aku ingatkan lagi sekitar {next_time.strftime('%H:%M')}.\n\n"
            "Si kecil jadi lebih nyaman 😊",
            reply_markup=main_menu(),
        )
        return

    if data == "menu_susu":
        await query.edit_message_text("🥛 Pilih jenis susu dan jumlahnya:", reply_markup=milk_menu())
        return

    if data.startswith("milk_"):
        _, milk_type, amount = data.split("_")
        event_type = "asip" if milk_type == "asip" else "formula"
        add_event(chat_id, event_type, amount_ml=int(amount))
        label = "ASIP" if event_type == "asip" else "Susu formula"
        await query.edit_message_text(
            f"✅ {label} tercatat.\n\n"
            f"Jumlah: {amount} ml\n"
            f"Jam: {now_local().strftime('%H:%M')}",
            reply_markup=main_menu(),
        )
        return

    if data == "manual_milk":
        set_state(chat_id, "WAIT_MILK")
        await query.edit_message_text(
            "✍️ Ketik jumlah susu dalam ml.\n\n"
            "Contoh: 60\n\n"
            "Catatan: default akan dicatat sebagai susu formula."
        )
        return

    if data == "mulai_tidur":
        last_sleep = last_event(chat_id, "sleep_start")
        last_wake = last_event(chat_id, "sleep_end")

        if last_sleep and (not last_wake or parse_dt(last_sleep["created_at"]) > parse_dt(last_wake["created_at"])):
            await query.edit_message_text(
                "😴 Timer tidur masih berjalan.\n\n"
                "Tekan ☀️ Bangun kalau si kecil sudah bangun.",
                reply_markup=main_menu(),
            )
            return

        add_event(chat_id, "sleep_start")
        await query.edit_message_text(
            f"😴 Selamat tidur ya, Dek.\n\n"
            f"Timer tidur dimulai pukul {now_local().strftime('%H:%M')}.",
            reply_markup=main_menu(),
        )
        return

    if data == "bangun_tidur":
        last_sleep = last_event(chat_id, "sleep_start")
        last_wake = last_event(chat_id, "sleep_end")

        if not last_sleep or (last_wake and parse_dt(last_wake["created_at"]) > parse_dt(last_sleep["created_at"])):
            await query.edit_message_text(
                "☀️ Belum ada timer tidur yang berjalan.\n\n"
                "Tekan 😴 Mulai Tidur dulu saat si kecil mulai tidur.",
                reply_markup=main_menu(),
            )
            return

        start_dt = parse_dt(last_sleep["created_at"])
        end_dt = now_local()
        add_event(chat_id, "sleep_end", started_at=start_dt, ended_at=end_dt)

        duration = end_dt - start_dt
        hours = int(duration.total_seconds() // 3600)
        minutes = int((duration.total_seconds() % 3600) // 60)

        await query.edit_message_text(
            f"☀️ Si kecil sudah bangun 😊\n\n"
            f"Durasi tidur: {hours} jam {minutes} menit",
            reply_markup=main_menu(),
        )
        return

    if data == "statistik":
        await query.edit_message_text(build_stats(chat_id), reply_markup=main_menu())
        return

    if data == "profil":
        chat = get_chat(chat_id)
        baby_name = chat["baby_name"] or "belum diisi"
        await query.edit_message_text(
            f"👶 Profil Bayi\n\n"
            f"Nama: {baby_name}\n\n"
            "Untuk mengubah nama, buka ⚙️ Pengaturan lalu pilih 👶 Ubah Nama Bayi.",
            reply_markup=main_menu(),
        )
        return

    if data == "pengaturan":
        chat = get_chat(chat_id)
        await query.edit_message_text(
            "⚙️ Pengaturan\n\n"
            f"🍼 Interval menyusu: {int(chat['asi_minutes'])} menit\n"
            f"💩 Interval popok: {int(chat['popok_minutes'])} menit\n\n"
            "Pilih pengaturan:",
            reply_markup=settings_menu(),
        )
        return

    if data.startswith("set_asi_"):
        minutes = int(data.split("_")[-1])
        update_interval(chat_id, "asi_minutes", minutes)
        schedule_once(context, chat_id, "asi", minutes)
        await query.edit_message_text(
            f"✅ Interval menyusu diubah menjadi {minutes} menit.",
            reply_markup=settings_menu(),
        )
        return

    if data.startswith("set_popok_"):
        minutes = int(data.split("_")[-1])
        update_interval(chat_id, "popok_minutes", minutes)
        schedule_once(context, chat_id, "popok", minutes)
        await query.edit_message_text(
            f"✅ Interval popok diubah menjadi {minutes} menit.",
            reply_markup=settings_menu(),
        )
        return

    if data == "ubah_nama":
        set_state(chat_id, "WAIT_BABY_NAME")
        await query.edit_message_text("👶 Ketik nama bayi ya.")
        return

    if data == "snooze_asi":
        schedule_once(context, chat_id, "asi", DEFAULT_SNOOZE_MINUTES)
        await query.edit_message_text(
            f"⏰ Reminder menyusu ditunda {DEFAULT_SNOOZE_MINUTES} menit.",
            reply_markup=main_menu(),
        )
        return

    if data == "snooze_popok":
        schedule_once(context, chat_id, "popok", DEFAULT_SNOOZE_MINUTES)
        await query.edit_message_text(
            f"⏰ Reminder popok ditunda {DEFAULT_SNOOZE_MINUTES} menit.",
            reply_markup=main_menu(),
        )
        return


# =========================
# HANDLER TEXT
# =========================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_chat(chat_id)

    text = (update.message.text or "").strip()
    state = get_state(chat_id)

    if state == "WAIT_BABY_NAME":
        update_baby_name(chat_id, text)
        set_state(chat_id, None)
        await update.message.reply_text(
            f"✅ Nama bayi disimpan: {text}",
            reply_markup=main_menu(),
        )
        return

    if state == "WAIT_MILK":
        try:
            amount = int(text)
            if amount <= 0 or amount > 500:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Mohon ketik angka ml yang benar. Contoh: 60")
            return

        add_event(chat_id, "formula", amount_ml=amount)
        set_state(chat_id, None)
        await update.message.reply_text(
            f"✅ Susu formula tercatat.\n\nJumlah: {amount} ml\nJam: {now_local().strftime('%H:%M')}",
            reply_markup=main_menu(),
        )
        return

    await update.message.reply_text(
        "Aku belum paham pesan itu 😊\n\n"
        "Pakai tombol menu di bawah ya.",
        reply_markup=main_menu(),
    )


# =========================
# STATISTIK
# =========================

def build_stats(chat_id: int) -> str:
    events = today_events(chat_id)

    count_asi = sum(1 for e in events if e["event_type"] == "asi")
    count_popok = sum(1 for e in events if e["event_type"] == "popok")
    count_asip = sum(1 for e in events if e["event_type"] == "asip")
    count_formula = sum(1 for e in events if e["event_type"] == "formula")

    ml_asip = sum((e["amount_ml"] or 0) for e in events if e["event_type"] == "asip")
    ml_formula = sum((e["amount_ml"] or 0) for e in events if e["event_type"] == "formula")

    sleep_minutes = 0
    for e in events:
        if e["event_type"] == "sleep_end" and e["started_at"] and e["ended_at"]:
            start_dt = parse_dt(e["started_at"])
            end_dt = parse_dt(e["ended_at"])
            sleep_minutes += max(0, int((end_dt - start_dt).total_seconds() // 60))

    sleep_h = sleep_minutes // 60
    sleep_m = sleep_minutes % 60

    last_asi = last_event(chat_id, "asi")
    last_popok = last_event(chat_id, "popok")

    last_asi_text = parse_dt(last_asi["created_at"]).strftime("%H:%M") if last_asi else "-"
    last_popok_text = parse_dt(last_popok["created_at"]).strftime("%H:%M") if last_popok else "-"

    return (
        "📊 Catatan Hari Ini\n\n"
        f"🍼 Menyusu: {count_asi} kali\n"
        f"💩 Ganti popok: {count_popok} kali\n"
        f"😴 Tidur: {sleep_h} jam {sleep_m} menit\n\n"
        f"🍼 ASIP: {count_asip} kali / {ml_asip} ml\n"
        f"🥛 Formula: {count_formula} kali / {ml_formula} ml\n\n"
        f"Terakhir menyusu: {last_asi_text}\n"
        f"Terakhir ganti popok: {last_popok_text}\n\n"
        "Semangat ya, Ayah & Bunda ❤️"
    )


# =========================
# STARTUP
# =========================

async def post_init(application: Application):
    init_db()

    # Set command list di Telegram
    await application.bot.set_my_commands([
        ("start", "Mulai Baby Care Bot"),
        ("menu", "Tampilkan menu"),
        ("stats", "Statistik hari ini"),
        ("stop", "Hentikan reminder"),
        ("help", "Bantuan"),
    ])

    # Kalau bot restart, jadwal reminder akan dibuat ulang dari data terakhir
    for chat_id in get_all_chats():
        reschedule_from_last(application, chat_id)
        schedule_daily_summary(application, chat_id)

    logger.info("Baby Care Bot siap berjalan.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN belum diisi. Tambahkan environment variable BOT_TOKEN.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("Baby Care Bot berjalan...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
