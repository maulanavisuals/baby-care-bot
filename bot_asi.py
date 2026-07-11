import os
import sqlite3
import logging
import secrets
import tempfile
from io import BytesIO
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak

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
DEFAULT_PUMP_MINUTES = int(os.getenv("DEFAULT_PUMP_MINUTES", "120"))      # 2 jam
DEFAULT_SNOOZE_MINUTES = int(os.getenv("DEFAULT_SNOOZE_MINUTES", "15"))
DEFAULT_REPEAT_REMINDER_MINUTES = int(os.getenv("DEFAULT_REPEAT_REMINDER_MINUTES", "15"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =========================
# DATABASE
# =========================

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn



def generate_family_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "BAYI-" + "".join(secrets.choice(alphabet) for _ in range(6))


def get_family_by_chat(chat_id: int):
    with db() as conn:
        return conn.execute(
            """
            SELECT f.*, fm.role_name
            FROM family_members fm
            JOIN families f ON f.id = fm.family_id
            WHERE fm.chat_id = ?
            """,
            (chat_id,),
        ).fetchone()


def get_family_members(chat_id: int):
    family = get_family_by_chat(chat_id)
    if not family:
        return []
    with db() as conn:
        return conn.execute(
            """
            SELECT chat_id, role_name, joined_at
            FROM family_members
            WHERE family_id = ?
            ORDER BY joined_at ASC
            """,
            (family["id"],),
        ).fetchall()


def get_data_chat_id(chat_id: int) -> int:
    family = get_family_by_chat(chat_id)
    return int(family["owner_chat_id"]) if family else chat_id


def ensure_family(chat_id: int, role_name: str = None):
    existing = get_family_by_chat(chat_id)
    if existing:
        return existing

    with db() as conn:
        code_value = generate_family_code()
        while conn.execute(
            "SELECT 1 FROM families WHERE family_code = ?",
            (code_value,),
        ).fetchone():
            code_value = generate_family_code()

        cur = conn.execute(
            """
            INSERT INTO families (family_code, owner_chat_id, created_at)
            VALUES (?, ?, ?)
            """,
            (code_value, chat_id, iso(now_local())),
        )
        family_id = cur.lastrowid
        conn.execute(
            """
            INSERT INTO family_members (chat_id, family_id, role_name, joined_at)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, family_id, role_name or "Pemilik", iso(now_local())),
        )

    return get_family_by_chat(chat_id)


def join_family(chat_id: int, family_code: str, role_name: str = None):
    family_code = family_code.strip().upper()
    with db() as conn:
        family = conn.execute(
            "SELECT * FROM families WHERE family_code = ?",
            (family_code,),
        ).fetchone()

        if not family:
            return False, "Kode keluarga tidak ditemukan."

        existing = conn.execute(
            "SELECT * FROM family_members WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()

        if existing:
            conn.execute(
                "DELETE FROM family_members WHERE chat_id = ?",
                (chat_id,),
            )

        conn.execute(
            """
            INSERT INTO family_members (chat_id, family_id, role_name, joined_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                chat_id,
                family["id"],
                role_name or "Anggota",
                iso(now_local()),
            ),
        )

    return True, None


def leave_family(chat_id: int):
    family = get_family_by_chat(chat_id)
    if not family:
        return False, "Belum tergabung dalam keluarga."

    if int(family["owner_chat_id"]) == chat_id:
        return False, "Pemilik keluarga tidak bisa keluar. Anggota lain tetap bisa menggunakan kode keluarga."

    with db() as conn:
        conn.execute("DELETE FROM family_members WHERE chat_id = ?", (chat_id,))
    return True, None


async def notify_family_members(
    context: ContextTypes.DEFAULT_TYPE,
    source_chat_id: int,
    text: str,
):
    members = get_family_members(source_chat_id)
    for member in members:
        target = int(member["chat_id"])
        if target == source_chat_id:
            continue
        try:
            await context.bot.send_message(chat_id=target, text=text)
        except Exception as exc:
            logger.warning("Gagal mengirim sinkronisasi keluarga ke %s: %s", target, exc)


def recorder_name(chat_id: int) -> str:
    family = get_family_by_chat(chat_id)
    if family and family["role_name"]:
        return family["role_name"]
    return "Keluarga"


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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS families (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                family_code TEXT UNIQUE NOT NULL,
                owner_chat_id INTEGER UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS family_members (
                chat_id INTEGER PRIMARY KEY,
                family_id INTEGER NOT NULL,
                role_name TEXT,
                joined_at TEXT NOT NULL,
                FOREIGN KEY (family_id) REFERENCES families(id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS growth_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                record_type TEXT NOT NULL,
                record_date TEXT NOT NULL,
                weight_kg REAL,
                length_cm REAL,
                notes TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # Migrasi database lama: tambahkan kolom tanggal lahir jika belum ada
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(chats)").fetchall()]
        if "birth_date" not in columns:
            conn.execute("ALTER TABLE chats ADD COLUMN birth_date TEXT")


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def iso(dt: datetime) -> str:
    return dt.astimezone(TIMEZONE).isoformat()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(TIMEZONE)


def ensure_chat(chat_id: int):
    chat_id = get_data_chat_id(chat_id)
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO chats (chat_id, created_at, asi_minutes, popok_minutes)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, iso(now_local()), DEFAULT_ASI_MINUTES, DEFAULT_POPOK_MINUTES),
        )


def get_chat(chat_id: int):
    chat_id = get_data_chat_id(chat_id)
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
    chat_id = get_data_chat_id(chat_id)
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


def last_event_any(chat_id: int):
    chat_id = get_data_chat_id(chat_id)
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM events
            WHERE chat_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()


def delete_event_by_id(event_id: int):
    with db() as conn:
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))


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
    chat_id = get_data_chat_id(chat_id)
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
    chat_id = get_data_chat_id(chat_id)
    if field not in {"asi_minutes", "popok_minutes"}:
        return
    with db() as conn:
        conn.execute(f"UPDATE chats SET {field} = ? WHERE chat_id = ?", (minutes, chat_id))


def update_baby_name(chat_id: int, name: str):
    chat_id = get_data_chat_id(chat_id)
    with db() as conn:
        conn.execute("UPDATE chats SET baby_name = ? WHERE chat_id = ?", (name, chat_id))


def add_growth_record(
    chat_id: int,
    record_type: str,
    record_date: str,
    weight_kg: float,
    length_cm: float,
    notes: Optional[str] = None,
):
    chat_id = get_data_chat_id(chat_id)
    ensure_chat(chat_id)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO growth_records
            (chat_id, record_type, record_date, weight_kg, length_cm, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                record_type,
                record_date,
                weight_kg,
                length_cm,
                notes,
                iso(now_local()),
            ),
        )


def get_growth_records(chat_id: int):
    chat_id = get_data_chat_id(chat_id)
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM growth_records
            WHERE chat_id = ?
            ORDER BY date(record_date) ASC, id ASC
            """,
            (chat_id,),
        ).fetchall()


def delete_last_growth_record(chat_id: int):
    chat_id = get_data_chat_id(chat_id)
    with db() as conn:
        row = conn.execute(
            """
            SELECT id FROM growth_records
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()

        if not row:
            return False

        conn.execute("DELETE FROM growth_records WHERE id = ?", (row["id"],))
        return True


def update_birth_date(chat_id: int, birth_date: str):
    chat_id = get_data_chat_id(chat_id)
    with db() as conn:
        conn.execute("UPDATE chats SET birth_date = ? WHERE chat_id = ?", (birth_date, chat_id))


def calculate_age(birth_date_str: str) -> str:
    birth = datetime.strptime(birth_date_str, "%Y-%m-%d").date()
    today = now_local().date()

    if birth > today:
        return "Tanggal lahir tidak valid"

    years = today.year - birth.year
    months = today.month - birth.month
    days = today.day - birth.day

    if days < 0:
        months -= 1
        prev_month = today.month - 1 or 12
        prev_year = today.year if today.month > 1 else today.year - 1
        import calendar
        days += calendar.monthrange(prev_year, prev_month)[1]

    if months < 0:
        years -= 1
        months += 12

    parts = []
    if years > 0:
        parts.append(f"{years} tahun")
    if months > 0 or years > 0:
        parts.append(f"{months} bulan")
    parts.append(f"{days} hari")
    return " ".join(parts)


def format_birth_date(birth_date_str: str) -> str:
    nama_bulan = {
        1: "Januari", 2: "Februari", 3: "Maret", 4: "April",
        5: "Mei", 6: "Juni", 7: "Juli", 8: "Agustus",
        9: "September", 10: "Oktober", 11: "November", 12: "Desember"
    }
    d = datetime.strptime(birth_date_str, "%Y-%m-%d").date()
    return f"{d.day} {nama_bulan[d.month]} {d.year}"


# =========================
# TAMPILAN MENU
# =========================

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍼 Menyusu", callback_data="menu_menyusu"),
         InlineKeyboardButton("🍼 ASIP", callback_data="menu_susu")],
        [InlineKeyboardButton("🤱 Pompa ASI", callback_data="pompa_asi"),
         InlineKeyboardButton("💩 Popok", callback_data="menu_popok")],
        [InlineKeyboardButton("😴 Mulai Tidur", callback_data="mulai_tidur"),
         InlineKeyboardButton("☀️ Bangun", callback_data="bangun_tidur")],
        [InlineKeyboardButton("⏳ Jadwal Berikutnya", callback_data="dashboard")],
        [InlineKeyboardButton("📊 Hari Ini", callback_data="statistik"),
         InlineKeyboardButton("📈 7 Hari", callback_data="statistik_7")],
        [InlineKeyboardButton("📄 Export PDF Mingguan", callback_data="export_weekly_pdf")],
        [InlineKeyboardButton("🕘 Riwayat", callback_data="riwayat"),
         InlineKeyboardButton("↩️ Hapus Terakhir", callback_data="reset_last")],
        [InlineKeyboardButton("👶 Profil Bayi", callback_data="profil"),
         InlineKeyboardButton("📏 Perkembangan", callback_data="growth_menu")],
        [InlineKeyboardButton("👨‍👩‍👧 Mode Keluarga", callback_data="family_menu"),
         InlineKeyboardButton("⚙️ Pengaturan", callback_data="pengaturan")],
    ])


def reminder_menu(kind: str):
    if kind == "asi":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Sudah Menyusu", callback_data="catat_asi")],
            [InlineKeyboardButton("⏰ Ingatkan Lagi 15 Menit", callback_data="snooze_asi")],
            [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu")],
        ])

    if kind == "pump":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Sudah Pompa ASI", callback_data="pompa_asi")],
            [InlineKeyboardButton("⏰ Ingatkan Lagi 15 Menit", callback_data="snooze_pump")],
            [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu")],
        ])

    if kind == "asip":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Sudah Minum ASIP", callback_data="menu_susu")],
            [InlineKeyboardButton("⏰ Ingatkan Lagi 15 Menit", callback_data="snooze_asip")],
            [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu")],
        ])

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Sudah Ganti Popok", callback_data="catat_popok")],
        [InlineKeyboardButton("⏰ Ingatkan Lagi 15 Menit", callback_data="snooze_popok")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu")],
    ])


def breastfeeding_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Mulai Kiri", callback_data="bf_start_left"),
         InlineKeyboardButton("▶️ Mulai Kanan", callback_data="bf_start_right")],
        [InlineKeyboardButton("⏹️ Selesai Menyusu", callback_data="bf_stop")],
        [InlineKeyboardButton("✅ Catat Cepat Tanpa Timer", callback_data="catat_asi")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu")],
    ])


def diaper_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💧 Pipis", callback_data="popok_pipis"),
         InlineKeyboardButton("💩 BAB", callback_data="popok_bab")],
        [InlineKeyboardButton("💧💩 Pipis + BAB", callback_data="popok_keduanya")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu")],
    ])


def pump_amount_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("30 ml", callback_data="pump_ml_30"),
         InlineKeyboardButton("60 ml", callback_data="pump_ml_60"),
         InlineKeyboardButton("90 ml", callback_data="pump_ml_90")],
        [InlineKeyboardButton("120 ml", callback_data="pump_ml_120"),
         InlineKeyboardButton("150 ml", callback_data="pump_ml_150"),
         InlineKeyboardButton("180 ml", callback_data="pump_ml_180")],
        [InlineKeyboardButton("✍️ Input Manual", callback_data="pump_manual")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu")],
    ])


def milk_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("30 ml", callback_data="asip_30"),
         InlineKeyboardButton("60 ml", callback_data="asip_60"),
         InlineKeyboardButton("90 ml", callback_data="asip_90")],
        [InlineKeyboardButton("120 ml", callback_data="asip_120"),
         InlineKeyboardButton("150 ml", callback_data="asip_150"),
         InlineKeyboardButton("180 ml", callback_data="asip_180")],
        [InlineKeyboardButton("✍️ Input Manual", callback_data="asip_manual")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu")],
    ])


def family_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Lihat Kode Keluarga", callback_data="family_code")],
        [InlineKeyboardButton("➕ Gabung Keluarga", callback_data="family_join")],
        [InlineKeyboardButton("✏️ Atur Nama/Peran Saya", callback_data="family_role")],
        [InlineKeyboardButton("👥 Lihat Anggota", callback_data="family_members")],
        [InlineKeyboardButton("🚪 Keluar dari Keluarga", callback_data="family_leave")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu")],
    ])


def growth_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚖️ Data Lahir", callback_data="growth_birth")],
        [InlineKeyboardButton("🏥 Data Keluar RS", callback_data="growth_discharge")],
        [InlineKeyboardButton("🩺 Tambah Kontrol DSA", callback_data="growth_checkup")],
        [InlineKeyboardButton("📊 Lihat Perkembangan", callback_data="growth_history")],
        [InlineKeyboardButton("↩️ Hapus Data Terakhir", callback_data="growth_delete_last")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu")],
    ])


def settings_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍼 ASIP 2 jam", callback_data="set_asi_120"),
         InlineKeyboardButton("🍼 ASIP 2,5 jam", callback_data="set_asi_150"),
         InlineKeyboardButton("🍼 ASIP 3 jam", callback_data="set_asi_180")],
        [InlineKeyboardButton("💩 Popok 3 jam", callback_data="set_popok_180"),
         InlineKeyboardButton("💩 Popok 4 jam", callback_data="set_popok_240"),
         InlineKeyboardButton("💩 Popok 5 jam", callback_data="set_popok_300")],
        [InlineKeyboardButton("👶 Ubah Nama Bayi", callback_data="ubah_nama")],
        [InlineKeyboardButton("📅 Atur Tanggal Lahir", callback_data="ubah_tanggal_lahir")],
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
    schedule_once(context, chat_id, "pump", DEFAULT_PUMP_MINUTES)


def reschedule_from_last(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    chat = get_chat(chat_id)

    # Reminder hanya untuk popok, ASIP, dan pompa ASI.
    last_popok = last_event(chat_id, "popok")
    popok_interval = int(chat["popok_minutes"])

    if last_popok:
        next_popok = parse_dt(last_popok["created_at"]) + timedelta(minutes=popok_interval)
        if next_popok <= now_local():
            schedule_once(context, chat_id, "popok", 1)
        else:
            schedule_at(context, chat_id, "popok", next_popok)
    else:
        schedule_once(context, chat_id, "popok", popok_interval)

    last_asip = last_event(chat_id, "asip")
    if last_asip:
        next_asip = parse_dt(last_asip["created_at"]) + timedelta(minutes=int(chat["asi_minutes"]))
        if next_asip <= now_local():
            schedule_once(context, chat_id, "asip", 1)
        else:
            schedule_at(context, chat_id, "asip", next_asip)
    else:
        schedule_once(context, chat_id, "asip", int(chat["asi_minutes"]))

    last_pump = last_event(chat_id, "pump")
    if last_pump:
        next_pump = parse_dt(last_pump["created_at"]) + timedelta(minutes=DEFAULT_PUMP_MINUTES)
        if next_pump <= now_local():
            schedule_once(context, chat_id, "pump", 1)
        else:
            schedule_at(context, chat_id, "pump", next_pump)
    else:
        schedule_once(context, chat_id, "pump", DEFAULT_PUMP_MINUTES)


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    kind = context.job.data["kind"]

    if kind == "asi":
        last = last_event(chat_id, "asi")
        last_text = parse_dt(last["created_at"]).strftime("%H:%M") if last else "-"
        text = (
            "🍼 Waktunya Menyusu\n\n"
            f"Terakhir menyusu: {last_text}\n"
            f"Belum ada catatan baru. Aku akan mengingatkan lagi dalam "
            f"{DEFAULT_REPEAT_REMINDER_MINUTES} menit sampai aktivitas dicatat."
        )
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reminder_menu("asi"))
        schedule_once(context, chat_id, "asi", DEFAULT_REPEAT_REMINDER_MINUTES)
        return

    if kind == "asip":
        last = last_event(chat_id, "asip")
        last_text = parse_dt(last["created_at"]).strftime("%H:%M") if last else "-"
        text = (
            "🍼 Waktunya ASIP\n\n"
            f"Terakhir minum ASIP: {last_text}\n"
            "Setelah selesai, catat jumlah ASIP yang diminum dalam ml.\n"
            f"Kalau belum dicatat, aku akan mengingatkan lagi dalam "
            f"{DEFAULT_REPEAT_REMINDER_MINUTES} menit."
        )
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reminder_menu("asip"))
        schedule_once(context, chat_id, "asip", DEFAULT_REPEAT_REMINDER_MINUTES)
        return

    if kind == "pump":
        last = last_event(chat_id, "pump")
        last_text = parse_dt(last["created_at"]).strftime("%H:%M") if last else "-"
        text = (
            "🤱 Waktunya Pompa ASI\n\n"
            f"Terakhir pompa: {last_text}\n"
            "Setelah selesai, catat jumlah ASI yang berhasil dipompa.\n"
            f"Kalau belum dicatat, aku akan mengingatkan lagi dalam "
            f"{DEFAULT_REPEAT_REMINDER_MINUTES} menit."
        )
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reminder_menu("pump"))
        schedule_once(context, chat_id, "pump", DEFAULT_REPEAT_REMINDER_MINUTES)
        return

    last = last_event(chat_id, "popok")
    last_text = parse_dt(last["created_at"]).strftime("%H:%M") if last else "-"
    text = (
        "💩 Waktunya Cek Popok\n\n"
        f"Terakhir ganti popok: {last_text}\n"
        "Silakan cek apakah popok sudah basah atau penuh."
    )
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reminder_menu("popok"))


async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id

    nama_hari = {
        0: "Senin",
        1: "Selasa",
        2: "Rabu",
        3: "Kamis",
        4: "Jumat",
        5: "Sabtu",
        6: "Minggu",
    }

    nama_bulan = {
        1: "Januari",
        2: "Februari",
        3: "Maret",
        4: "April",
        5: "Mei",
        6: "Juni",
        7: "Juli",
        8: "Agustus",
        9: "September",
        10: "Oktober",
        11: "November",
        12: "Desember",
    }

    sekarang = now_local()
    tanggal = (
        f"{nama_hari[sekarang.weekday()]}, "
        f"{sekarang.day} {nama_bulan[sekarang.month]} {sekarang.year}"
    )

    chat = get_chat(chat_id)
    baby_name = chat["baby_name"] or "Belum diisi"

    birth_date = chat["birth_date"] if "birth_date" in chat.keys() else None
    age_text = calculate_age(birth_date) if birth_date else "Belum dapat dihitung"

    growth_rows = get_growth_records(chat_id)
    latest_growth = growth_rows[-1] if growth_rows else None

    if latest_growth:
        weight_text = f"{latest_growth['weight_kg']:.2f} kg"
        length_text = f"{latest_growth['length_cm']:.1f} cm"
    else:
        weight_text = "Belum ada data"
        length_text = "Belum ada data"

    summary = (
        "🌙 Ringkasan Harian\n"
        f"📅 {tanggal}\n"
        "🕚 Pukul 23.59 WIB\n\n"
        "👶 Profil Bayi\n"
        f"Nama: {baby_name}\n"
        f"Umur: {age_text}\n"
        f"Berat terbaru: {weight_text}\n"
        f"Panjang terbaru: {length_text}\n\n"
        + build_stats(chat_id)
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=summary,
        reply_markup=main_menu(),
    )


def schedule_daily_summary(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    clear_jobs(context, chat_id, "summary")
    context.job_queue.run_daily(
        daily_summary_job,
        time=now_local().replace(hour=23, minute=59, second=0, microsecond=0).timetz(),
        chat_id=chat_id,
        name=f"summary_{chat_id}",
    )


def format_duration_minutes(total_minutes: int) -> str:
    h = total_minutes // 60
    m = total_minutes % 60
    if h and m:
        return f"{h} jam {m} menit"
    if h:
        return f"{h} jam"
    return f"{m} menit"


def recent_events(chat_id: int, limit: int = 10):
    chat_id = get_data_chat_id(chat_id)
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM events
            WHERE chat_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()


def events_between(chat_id: int, start: datetime, end: datetime):
    chat_id = get_data_chat_id(chat_id)
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


def growth_type_label(record_type: str) -> str:
    labels = {
        "birth": "Lahir",
        "discharge": "Keluar RS",
        "checkup": "Kontrol DSA",
    }
    return labels.get(record_type, record_type)


def format_growth_history(chat_id: int) -> str:
    rows = get_growth_records(chat_id)

    if not rows:
        return (
            "📏 Perkembangan Bayi\n\n"
            "Belum ada data berat dan panjang yang dicatat."
        )

    lines = ["📏 Perkembangan Bayi", ""]
    first_weight = None

    for row in rows:
        record_date = datetime.strptime(row["record_date"], "%Y-%m-%d").date()
        date_text = f"{record_date.day:02d}-{record_date.month:02d}-{record_date.year}"
        weight = row["weight_kg"]
        length = row["length_cm"]

        if first_weight is None and weight is not None:
            first_weight = weight

        change_text = ""
        if first_weight is not None and weight is not None:
            diff = weight - first_weight
            sign = "+" if diff > 0 else ""
            if abs(diff) >= 0.001:
                change_text = f" ({sign}{diff:.2f} kg dari lahir)"

        lines.append(f"{growth_type_label(row['record_type'])} — {date_text}")
        lines.append(f"⚖️ {weight:.2f} kg{change_text}")
        lines.append(f"📐 {length:.1f} cm")
        if row["notes"]:
            lines.append(f"📝 {row['notes']}")
        lines.append("")

    return "\n".join(lines).strip()


def event_label(event_type: str) -> str:
    labels = {
        "asi": "🍼 Menyusu",
        "popok": "💩 Ganti popok",
        "asip": "🍼 ASIP",
        "formula": "🥛 Susu formula",
        "pump": "🤱 Pompa ASI",
        "sleep_start": "😴 Mulai tidur",
        "sleep_end": "☀️ Bangun",
        "bf_start_left": "🍼 Mulai menyusu kiri",
        "bf_start_right": "🍼 Mulai menyusu kanan",
        "bf_session": "🍼 Sesi menyusu",
        "popok_pipis": "💧 Popok pipis",
        "popok_bab": "💩 Popok BAB",
        "popok_keduanya": "💧💩 Popok pipis + BAB",
    }
    return labels.get(event_type, event_type)



def get_weekly_data(chat_id: int):
    end = now_local()
    start = (end - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_exclusive = (end + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    rows = events_between(chat_id, start, end_exclusive)
    days = [start + timedelta(days=i) for i in range(7)]

    result = []
    for day in days:
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        daily = [
            r for r in rows
            if day_start <= parse_dt(r["created_at"]) < day_end
        ]

        sleep_minutes = 0
        for e in daily:
            if e["event_type"] == "sleep_end" and e["started_at"] and e["ended_at"]:
                sleep_minutes += max(
                    0,
                    int(
                        (
                            parse_dt(e["ended_at"]) - parse_dt(e["started_at"])
                        ).total_seconds() // 60
                    ),
                )

        result.append({
            "date": day_start,
            "asi_count": sum(1 for e in daily if e["event_type"] == "asi"),
            "asip_ml": sum((e["amount_ml"] or 0) for e in daily if e["event_type"] == "asip"),
            "pump_ml": sum((e["amount_ml"] or 0) for e in daily if e["event_type"] == "pump"),
            "popok_count": sum(1 for e in daily if e["event_type"] == "popok"),
            "sleep_hours": sleep_minutes / 60,
        })

    return start, end, result


def build_weekly_pdf(chat_id: int) -> str:
    data_chat_id = get_data_chat_id(chat_id)
    start, end, weekly = get_weekly_data(data_chat_id)

    chat = get_chat(data_chat_id)
    baby_name = chat["baby_name"] or "Belum diisi"
    birth_date = chat["birth_date"] if "birth_date" in chat.keys() else None
    age_text = calculate_age(birth_date) if birth_date else "Belum dapat dihitung"

    growth_rows = get_growth_records(data_chat_id)
    latest_growth = growth_rows[-1] if growth_rows else None
    weight_text = f"{latest_growth['weight_kg']:.2f} kg" if latest_growth else "Belum ada data"
    length_text = f"{latest_growth['length_cm']:.1f} cm" if latest_growth else "Belum ada data"

    tmp_dir = tempfile.mkdtemp(prefix="babycare_weekly_")
    pdf_path = os.path.join(
        tmp_dir,
        f"ringkasan_mingguan_{baby_name.replace(' ', '_')}_{end.strftime('%Y%m%d')}.pdf",
    )
    activity_chart = os.path.join(tmp_dir, "activity_chart.png")
    growth_chart = os.path.join(tmp_dir, "growth_chart.png")

    labels = [d["date"].strftime("%d/%m") for d in weekly]

    # Grafik aktivitas mingguan
    fig = plt.figure(figsize=(9, 5))
    ax = fig.add_subplot(111)
    x = list(range(7))
    ax.plot(x, [d["asip_ml"] for d in weekly], marker="o", label="ASIP diminum (ml)")
    ax.plot(x, [d["pump_ml"] for d in weekly], marker="o", label="Hasil pompa (ml)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_title("ASIP dan Hasil Pompa - 7 Hari")
    ax.set_xlabel("Tanggal")
    ax.set_ylabel("ml")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(activity_chart, dpi=160)
    plt.close(fig)

    # Grafik pertumbuhan
    growth_for_chart = growth_rows[-12:] if growth_rows else []
    if growth_for_chart:
        g_labels = [
            datetime.strptime(r["record_date"], "%Y-%m-%d").strftime("%d/%m")
            for r in growth_for_chart
        ]
        weights = [r["weight_kg"] for r in growth_for_chart]
        lengths = [r["length_cm"] for r in growth_for_chart]

        fig = plt.figure(figsize=(9, 5))
        ax1 = fig.add_subplot(111)
        xg = list(range(len(g_labels)))
        ax1.plot(xg, weights, marker="o", label="Berat (kg)")
        ax1.set_xticks(xg)
        ax1.set_xticklabels(g_labels, rotation=30)
        ax1.set_xlabel("Tanggal")
        ax1.set_ylabel("Berat (kg)")
        ax1.grid(True, alpha=0.25)

        ax2 = ax1.twinx()
        ax2.plot(xg, lengths, marker="s", linestyle="--", label="Panjang (cm)")
        ax2.set_ylabel("Panjang (cm)")
        ax1.set_title("Perkembangan Berat dan Panjang Bayi")

        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2, loc="best")

        fig.tight_layout()
        fig.savefig(growth_chart, dpi=160)
        plt.close(fig)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "BabyTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        fontSize=18,
        leading=22,
        spaceAfter=12,
    )
    heading = styles["Heading2"]
    body = styles["BodyText"]

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )

    story = []
    story.append(Paragraph("Laporan Mingguan Baby Care", title_style))
    story.append(
        Paragraph(
            f"Periode: {start.strftime('%d/%m/%Y')} - {end.strftime('%d/%m/%Y')}",
            body,
        )
    )
    story.append(Spacer(1, 12))

    story.append(Paragraph("Profil Bayi", heading))
    profile_data = [
        ["Nama", baby_name],
        ["Umur", age_text],
        ["Berat terbaru", weight_text],
        ["Panjang terbaru", length_text],
    ]
    profile_table = Table(profile_data, colWidths=[4 * cm, 11 * cm])
    profile_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(profile_table)
    story.append(Spacer(1, 16))

    total_asi = sum(d["asi_count"] for d in weekly)
    total_asip = sum(d["asip_ml"] for d in weekly)
    total_pump = sum(d["pump_ml"] for d in weekly)
    total_popok = sum(d["popok_count"] for d in weekly)
    total_sleep = sum(d["sleep_hours"] for d in weekly)

    story.append(Paragraph("Ringkasan 7 Hari", heading))
    summary_data = [
        ["Menyusu langsung", f"{total_asi} kali"],
        ["ASIP diminum", f"{total_asip} ml"],
        ["Hasil pompa ASI", f"{total_pump} ml"],
        ["Ganti popok", f"{total_popok} kali"],
        ["Total tidur", f"{total_sleep:.1f} jam"],
    ]
    summary_table = Table(summary_data, colWidths=[7 * cm, 8 * cm])
    summary_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 16))

    story.append(Paragraph("Detail Harian", heading))
    table_data = [["Tanggal", "Menyusu", "ASIP", "Pompa", "Popok", "Tidur"]]
    for d in weekly:
        table_data.append([
            d["date"].strftime("%d/%m"),
            str(d["asi_count"]),
            f"{d['asip_ml']} ml",
            f"{d['pump_ml']} ml",
            str(d["popok_count"]),
            f"{d['sleep_hours']:.1f} j",
        ])

    daily_table = Table(
        table_data,
        colWidths=[2.3 * cm, 2.4 * cm, 2.4 * cm, 2.4 * cm, 2.2 * cm, 2.2 * cm],
        repeatRows=1,
    )
    daily_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(daily_table)
    story.append(Spacer(1, 18))

    story.append(Paragraph("Grafik Mingguan", heading))
    story.append(Image(activity_chart, width=17 * cm, height=9.4 * cm))

    if growth_for_chart:
        story.append(PageBreak())
        story.append(Paragraph("Grafik Perkembangan Bayi", heading))
        story.append(Image(growth_chart, width=17 * cm, height=9.4 * cm))

        growth_table_data = [["Tanggal", "Tahap", "Berat", "Panjang"]]
        for r in growth_for_chart:
            growth_table_data.append([
                datetime.strptime(r["record_date"], "%Y-%m-%d").strftime("%d/%m/%Y"),
                growth_type_label(r["record_type"]),
                f"{r['weight_kg']:.2f} kg",
                f"{r['length_cm']:.1f} cm",
            ])
        growth_table = Table(
            growth_table_data,
            colWidths=[4 * cm, 5 * cm, 3 * cm, 3 * cm],
            repeatRows=1,
        )
        growth_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("ALIGN", (2, 1), (-1, -1), "CENTER"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(Spacer(1, 12))
        story.append(growth_table)

    doc.build(story)
    return pdf_path


async def send_weekly_pdf(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    pdf_path = build_weekly_pdf(chat_id)
    try:
        with open(pdf_path, "rb") as pdf_file:
            await context.bot.send_document(
                chat_id=chat_id,
                document=pdf_file,
                filename=os.path.basename(pdf_path),
                caption="📄 Laporan Mingguan Baby Care",
            )
    finally:
        try:
            tmp_dir = os.path.dirname(pdf_path)
            for name in os.listdir(tmp_dir):
                os.remove(os.path.join(tmp_dir, name))
            os.rmdir(tmp_dir)
        except Exception:
            pass


async def weekly_pdf_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await send_weekly_pdf(context, chat_id)


def schedule_weekly_pdf(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    clear_jobs(context, chat_id, "weekly_pdf")
    # Minggu pukul 23.59 WIB.
    context.job_queue.run_daily(
        weekly_pdf_job,
        time=now_local().replace(hour=23, minute=59, second=30, microsecond=0).timetz(),
        days=(6,),
        chat_id=chat_id,
        name=f"weekly_pdf_{chat_id}",
    )


# =========================
# HANDLER COMMAND
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_chat(chat_id)
    reschedule_from_last(context, chat_id)
    schedule_daily_summary(context, chat_id)
    schedule_weekly_pdf(context, chat_id)

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


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot aktif dan berjalan normal.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Perintah:\n"
        "/start - mulai bot\n"
        "/stop - hentikan reminder\n"
        "/menu - tampilkan menu\n"
        "/stats - statistik hari ini\n\n"
        "Tips: tekan tombol setiap selesai menyusui, ganti popok, atau si kecil tidur.\n"
        "Menyusu langsung tidak memakai reminder. Reminder hanya untuk ASIP, popok, dan pompa ASI.\nKalau salah input, pilih ↩️ Hapus Input Terakhir."
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

    if data == "menu_menyusu":
        await query.edit_message_text(
            "🍼 Menyusu Langsung\n\n"
            "Pilih sisi untuk mulai timer, atau catat cepat tanpa timer.",
            reply_markup=breastfeeding_menu(),
        )
        return

    if data in {"bf_start_left", "bf_start_right"}:
        last_left = last_event(chat_id, "bf_start_left")
        last_right = last_event(chat_id, "bf_start_right")
        last_session = last_event(chat_id, "bf_session")

        latest_start = None
        if last_left and last_right:
            latest_start = last_left if parse_dt(last_left["created_at"]) > parse_dt(last_right["created_at"]) else last_right
        else:
            latest_start = last_left or last_right

        if latest_start and (not last_session or parse_dt(latest_start["created_at"]) > parse_dt(last_session["created_at"])):
            await query.edit_message_text(
                "⏱️ Timer menyusu masih berjalan. Tekan ⏹️ Selesai Menyusu dulu.",
                reply_markup=breastfeeding_menu(),
            )
            return

        add_event(chat_id, data)
        side = "kiri" if data == "bf_start_left" else "kanan"
        await query.edit_message_text(
            f"🍼 Timer menyusu {side} dimulai pukul {now_local().strftime('%H:%M')}.",
            reply_markup=breastfeeding_menu(),
        )
        return

    if data == "bf_stop":
        last_left = last_event(chat_id, "bf_start_left")
        last_right = last_event(chat_id, "bf_start_right")
        latest = None
        if last_left and last_right:
            latest = last_left if parse_dt(last_left["created_at"]) > parse_dt(last_right["created_at"]) else last_right
        else:
            latest = last_left or last_right

        last_session = last_event(chat_id, "bf_session")
        if not latest or (last_session and parse_dt(last_session["created_at"]) > parse_dt(latest["created_at"])):
            await query.edit_message_text(
                "Belum ada timer menyusu yang berjalan.",
                reply_markup=breastfeeding_menu(),
            )
            return

        start_dt = parse_dt(latest["created_at"])
        end_dt = now_local()
        duration_min = max(1, int((end_dt - start_dt).total_seconds() // 60))
        side = "kiri" if latest["event_type"] == "bf_start_left" else "kanan"

        add_event(chat_id, "bf_session", amount_ml=duration_min, started_at=start_dt, ended_at=end_dt)
        add_event(chat_id, "asi")

        chat = get_chat(chat_id)
        clear_jobs(context, chat_id, "asi")

        await query.edit_message_text(
            f"✅ Menyusu selesai.\n\n"
            f"Sisi: {side}\n"
            f"Durasi: {duration_min} menit\n"
            f"⏰ Reminder berikutnya sekitar {next_time.strftime('%H:%M')}.",
            reply_markup=main_menu(),
        )
        return

    if data == "menu_popok":
        await query.edit_message_text(
            "💩 Catat Popok\n\nPilih kondisi popok:",
            reply_markup=diaper_menu(),
        )
        return

    if data in {"popok_pipis", "popok_bab", "popok_keduanya"}:
        add_event(chat_id, data)
        add_event(chat_id, "popok")
        await notify_family_members(
            context,
            chat_id,
            f"{event_label(data)} dicatat oleh {recorder_name(chat_id)} pukul {now_local().strftime('%H:%M')}."
        )
        chat = get_chat(chat_id)
        schedule_once(context, chat_id, "popok", int(chat["popok_minutes"]))
        label = event_label(data)
        await query.edit_message_text(
            f"✅ {label} tercatat pukul {now_local().strftime('%H:%M')}.",
            reply_markup=main_menu(),
        )
        return

    if data == "catat_asi":
        add_event(chat_id, "asi")
        clear_jobs(context, chat_id, "asi")
        await notify_family_members(
            context,
            chat_id,
            f"🍼 Menyusu langsung dicatat oleh {recorder_name(chat_id)} pukul {now_local().strftime('%H:%M')}."
        )
        await query.edit_message_text(
            f"✅ Menyusu langsung berhasil dicatat\n\n"
            f"Jam: {now_local().strftime('%H:%M')}\n"
            f"Dicatat oleh: {recorder_name(chat_id)}\n\n"
            "Menyusu langsung tidak memakai reminder otomatis.",
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

    if data == "pompa_asi":
        await query.edit_message_text(
            "🤱 Catat Pompa ASI\n\n"
            "Berapa total ASI yang berhasil dipompa?\n"
            "Pilih jumlah di bawah atau masukkan secara manual.",
            reply_markup=pump_amount_menu(),
        )
        return

    if data.startswith("pump_ml_"):
        amount = int(data.split("_")[-1])
        add_event(chat_id, "pump", amount_ml=amount)
        await notify_family_members(
            context,
            chat_id,
            f"🤱 Pompa ASI {amount} ml dicatat oleh {recorder_name(chat_id)} pukul {now_local().strftime('%H:%M')}."
        )
        schedule_once(context, chat_id, "pump", DEFAULT_PUMP_MINUTES)
        next_pump = now_local() + timedelta(minutes=DEFAULT_PUMP_MINUTES)

        await query.edit_message_text(
            f"✅ Pompa ASI berhasil dicatat\n\n"
            f"Hasil pompa: {amount} ml\n"
            f"Jam: {now_local().strftime('%H:%M')}\n"
            f"⏰ Reminder berulang dihentikan.\n"
            f"Jadwal pompa berikutnya sekitar {next_pump.strftime('%H:%M')}.\n\n"
            "Semangat, Bunda ❤️",
            reply_markup=main_menu(),
        )
        return

    if data == "pump_manual":
        set_state(chat_id, "WAIT_PUMP_ML")
        await query.edit_message_text(
            "✍️ Input Manual Hasil Pompa\n\n"
            "Ketik jumlah ASI yang berhasil dipompa dalam ml.\n\n"
            "Contoh: 75"
        )
        return

    if data == "menu_susu":
        await query.edit_message_text("🍼 Berapa ml ASIP yang diminum si kecil?", reply_markup=milk_menu())
        return

    if data.startswith("asip_") and data != "asip_manual":
        amount = int(data.split("_")[-1])
        add_event(chat_id, "asip", amount_ml=amount)
        await notify_family_members(
            context,
            chat_id,
            f"🍼 ASIP {amount} ml dicatat oleh {recorder_name(chat_id)} pukul {now_local().strftime('%H:%M')}."
        )
        chat = get_chat(chat_id)
        schedule_once(context, chat_id, "asip", int(chat["asi_minutes"]))
        next_asip = now_local() + timedelta(minutes=int(chat["asi_minutes"]))
        await query.edit_message_text(
            f"✅ ASIP berhasil dicatat\n\n"
            f"Jumlah diminum: {amount} ml\n"
            f"Jam: {now_local().strftime('%H:%M')}\n"
            f"⏰ Reminder ASIP berikutnya sekitar {next_asip.strftime('%H:%M')}.",
            reply_markup=main_menu(),
        )
        return

    if data == "asip_manual":
        set_state(chat_id, "WAIT_ASIP_ML")
        await query.edit_message_text(
            "✍️ Input Manual ASIP\n\n"
            "Ketik jumlah ASIP yang diminum dalam ml.\n\n"
            "Contoh: 75"
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

    if data == "reset_last":
        last = last_event_any(chat_id)
        if not last:
            await query.edit_message_text(
                "Belum ada catatan yang bisa dihapus.",
                reply_markup=main_menu(),
            )
            return

        label = event_label(last["event_type"])
        time_text = parse_dt(last["created_at"]).strftime("%H:%M")
        extra = ""
        if last["amount_ml"]:
            extra = f"\nJumlah: {last['amount_ml']} ml"

        await query.edit_message_text(
            f"↩️ Hapus input terakhir?\n\n"
            f"{label}\n"
            f"Jam: {time_text}{extra}\n\n"
            "Data ini akan dihapus permanen.",
            reply_markup=reset_confirm_menu(),
        )
        return

    if data == "confirm_reset_last":
        last = last_event_any(chat_id)
        if not last:
            await query.edit_message_text(
                "Tidak ada catatan yang bisa dihapus.",
                reply_markup=main_menu(),
            )
            return

        deleted_type = last["event_type"]
        label = event_label(deleted_type)
        delete_event_by_id(int(last["id"]))

        # Pulihkan jadwal reminder yang terkait setelah penghapusan
        if deleted_type == "asi":
            chat = get_chat(chat_id)
            prev = last_event(chat_id, "asi")
            if prev:
                next_time = parse_dt(prev["created_at"]) + timedelta(minutes=int(chat["asi_minutes"]))
                schedule_at(context, chat_id, "asi", next_time) if next_time > now_local() else schedule_once(context, chat_id, "asi", 1)
            else:
                schedule_once(context, chat_id, "asi", int(chat["asi_minutes"]))

        elif deleted_type == "popok":
            chat = get_chat(chat_id)
            prev = last_event(chat_id, "popok")
            if prev:
                next_time = parse_dt(prev["created_at"]) + timedelta(minutes=int(chat["popok_minutes"]))
                schedule_at(context, chat_id, "popok", next_time) if next_time > now_local() else schedule_once(context, chat_id, "popok", 1)
            else:
                schedule_once(context, chat_id, "popok", int(chat["popok_minutes"]))

        elif deleted_type == "pump":
            prev = last_event(chat_id, "pump")
            if prev:
                next_time = parse_dt(prev["created_at"]) + timedelta(minutes=DEFAULT_PUMP_MINUTES)
                schedule_at(context, chat_id, "pump", next_time) if next_time > now_local() else schedule_once(context, chat_id, "pump", 1)
            else:
                schedule_once(context, chat_id, "pump", DEFAULT_PUMP_MINUTES)

        await query.edit_message_text(
            f"✅ Input terakhir berhasil dihapus.\n\n{label}",
            reply_markup=main_menu(),
        )
        return

    if data == "dashboard":
        chat = get_chat(chat_id)

        def next_text(kind, minutes):
            last = last_event(chat_id, kind)
            if not last:
                return "belum ada catatan"
            next_dt = parse_dt(last["created_at"]) + timedelta(minutes=minutes)
            delta = int((next_dt - now_local()).total_seconds() // 60)
            if delta <= 0:
                return "sekarang / sudah lewat"
            return f"{format_duration_minutes(delta)} lagi ({next_dt.strftime('%H:%M')})"

        text = (
            "⏳ Jadwal Berikutnya\n\n"
            f"🤱 Pompa ASI: {next_text('pump', DEFAULT_PUMP_MINUTES)}\n"
            f"🍼 ASIP: {next_text('asip', int(chat['asi_minutes']))}\n"
            f"💩 Cek popok: {next_text('popok', int(chat['popok_minutes']))}"
        )
        await query.edit_message_text(text, reply_markup=main_menu())
        return

    if data == "riwayat":
        rows = recent_events(chat_id, 10)
        if not rows:
            text = "Belum ada riwayat."
        else:
            lines = ["🕘 10 Catatan Terakhir", ""]
            for r in rows:
                label = event_label(r["event_type"])
                t = parse_dt(r["created_at"]).strftime("%d/%m %H:%M")
                extra = ""
                if r["event_type"] in {"asip", "formula", "pump"} and r["amount_ml"]:
                    extra = f" — {r['amount_ml']} ml"
                elif r["event_type"] == "bf_session" and r["amount_ml"]:
                    extra = f" — {r['amount_ml']} menit"
                lines.append(f"• {t} | {label}{extra}")
            text = "\n".join(lines)
        await query.edit_message_text(text, reply_markup=main_menu())
        return

    if data == "statistik_7":
        end = now_local()
        start = end - timedelta(days=7)
        events = events_between(chat_id, start, end)

        count_asi = sum(1 for e in events if e["event_type"] == "asi")
        count_popok = sum(1 for e in events if e["event_type"] == "popok")
        count_pump = sum(1 for e in events if e["event_type"] == "pump")
        ml_pump = sum((e["amount_ml"] or 0) for e in events if e["event_type"] == "pump")
        ml_asip = sum((e["amount_ml"] or 0) for e in events if e["event_type"] == "asip")
        ml_formula = sum((e["amount_ml"] or 0) for e in events if e["event_type"] == "formula")
        sleep_minutes = 0
        bf_minutes = 0

        for e in events:
            if e["event_type"] == "sleep_end" and e["started_at"] and e["ended_at"]:
                sleep_minutes += max(0, int((parse_dt(e["ended_at"]) - parse_dt(e["started_at"])).total_seconds() // 60))
            if e["event_type"] == "bf_session" and e["amount_ml"]:
                bf_minutes += int(e["amount_ml"])

        text = (
            "📈 Statistik 7 Hari Terakhir\n\n"
            f"🍼 Menyusu: {count_asi} kali\n"
            f"⏱️ Durasi menyusu: {format_duration_minutes(bf_minutes)}\n"
            f"🤱 Pompa ASI: {count_pump} kali / {ml_pump} ml\n"
            f"🍼 ASIP diminum: {ml_asip} ml\n"

            f"💩 Ganti popok: {count_popok} kali\n"
            f"😴 Total tidur: {format_duration_minutes(sleep_minutes)}"
        )
        await query.edit_message_text(text, reply_markup=main_menu())
        return

    if data == "statistik":
        await query.edit_message_text(build_stats(chat_id), reply_markup=main_menu())
        return

    if data == "family_menu":
        ensure_family(chat_id)
        await query.edit_message_text(
            "👨‍👩‍👧 Mode Keluarga\n\n"
            "Hubungkan Telegram Ayah, Bunda, atau pengasuh ke satu data bayi yang sama.",
            reply_markup=family_menu(),
        )
        return

    if data == "family_code":
        family = ensure_family(chat_id)
        await query.edit_message_text(
            "🔑 Kode Keluarga\n\n"
            f"`{family['family_code']}`\n\n"
            "Kirim kode ini hanya kepada keluarga yang ingin diberi akses. "
            "Di akun Telegram kedua, buka Mode Keluarga → Gabung Keluarga.",
            reply_markup=family_menu(),
            parse_mode="Markdown",
        )
        return

    if data == "family_join":
        set_state(chat_id, "WAIT_FAMILY_CODE")
        await query.edit_message_text(
            "➕ Gabung Keluarga\n\n"
            "Ketik kode keluarga dari akun utama.\n"
            "Contoh: BAYI-ABC123"
        )
        return

    if data == "family_role":
        set_state(chat_id, "WAIT_FAMILY_ROLE")
        await query.edit_message_text(
            "✏️ Atur Nama/Peran Saya\n\n"
            "Ketik nama atau peran yang ingin ditampilkan.\n"
            "Contoh: Ayah, Bunda, Nenek, atau Pengasuh"
        )
        return

    if data == "family_members":
        family = ensure_family(chat_id)
        members = get_family_members(chat_id)
        lines = ["👥 Anggota Keluarga", ""]
        for i, member in enumerate(members, 1):
            role = member["role_name"] or "Anggota"
            owner_mark = " 👑" if int(member["chat_id"]) == int(family["owner_chat_id"]) else ""
            lines.append(f"{i}. {role}{owner_mark}")
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=family_menu(),
        )
        return

    if data == "family_leave":
        ok, error = leave_family(chat_id)
        if ok:
            await query.edit_message_text(
                "✅ Kamu sudah keluar dari keluarga.\n\n"
                "Catatan keluarga lama tidak terhapus.",
                reply_markup=main_menu(),
            )
        else:
            await query.edit_message_text(
                f"ℹ️ {error}",
                reply_markup=family_menu(),
            )
        return

    if data == "export_weekly_pdf":
        await query.edit_message_text(
            "📄 Laporan mingguan sedang dibuat...",
            reply_markup=main_menu(),
        )
        try:
            await send_weekly_pdf(context, chat_id)
        except Exception as exc:
            logger.exception("Gagal membuat PDF mingguan: %s", exc)
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Laporan PDF mingguan gagal dibuat. Silakan coba lagi.",
                reply_markup=main_menu(),
            )
        return

    if data == "growth_menu":
        await query.edit_message_text(
            "📏 Perkembangan Bayi\n\n"
            "Catat berat dan panjang bayi dari lahir, saat keluar RS, sampai setiap kontrol DSA.",
            reply_markup=growth_menu(),
        )
        return

    if data in {"growth_birth", "growth_discharge", "growth_checkup"}:
        record_type = {
            "growth_birth": "birth",
            "growth_discharge": "discharge",
            "growth_checkup": "checkup",
        }[data]

        context.user_data["growth_record_type"] = record_type
        set_state(chat_id, "WAIT_GROWTH_DATE")

        label = growth_type_label(record_type)
        await query.edit_message_text(
            f"📅 {label}\n\n"
            "Ketik tanggal dengan format DD-MM-YYYY.\n\n"
            "Contoh: 11-07-2026"
        )
        return

    if data == "growth_history":
        await query.edit_message_text(
            format_growth_history(chat_id),
            reply_markup=growth_menu(),
        )
        return

    if data == "growth_delete_last":
        deleted = delete_last_growth_record(chat_id)
        text = (
            "✅ Data perkembangan terakhir berhasil dihapus."
            if deleted
            else "Belum ada data perkembangan yang bisa dihapus."
        )
        await query.edit_message_text(text, reply_markup=growth_menu())
        return

    if data == "profil":
        chat = get_chat(chat_id)
        baby_name = chat["baby_name"] or "belum diisi"
        birth_date = chat["birth_date"] if "birth_date" in chat.keys() else None

        if birth_date:
            birth_text = format_birth_date(birth_date)
            age_text = calculate_age(birth_date)
        else:
            birth_text = "belum diisi"
            age_text = "belum dapat dihitung"

        growth_rows = get_growth_records(chat_id)
        latest_growth = growth_rows[-1] if growth_rows else None

        growth_text = ""
        if latest_growth:
            growth_text = (
                f"\n⚖️ Berat terakhir: {latest_growth['weight_kg']:.2f} kg"
                f"\n📐 Panjang terakhir: {latest_growth['length_cm']:.1f} cm"
            )

        await query.edit_message_text(
            f"👶 Profil Bayi\n\n"
            f"Nama: {baby_name}\n"
            f"📅 Tanggal lahir: {birth_text}\n"
            f"🎂 Umur: {age_text}"
            f"{growth_text}\n\n"
            "Umur dihitung otomatis setiap hari.",
            reply_markup=main_menu(),
        )
        return

    if data == "pengaturan":
        chat = get_chat(chat_id)
        await query.edit_message_text(
            "⚙️ Pengaturan\n\n"
            f"🍼 Interval ASIP: {int(chat['asi_minutes'])} menit\n"
            f"💩 Interval popok: {int(chat['popok_minutes'])} menit\n\n"
            "Pilih pengaturan:",
            reply_markup=settings_menu(),
        )
        return

    if data.startswith("set_asi_"):
        minutes = int(data.split("_")[-1])
        update_interval(chat_id, "asi_minutes", minutes)
        schedule_once(context, chat_id, "asip", minutes)
        await query.edit_message_text(
            f"✅ Interval ASIP diubah menjadi {minutes} menit.",
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

    if data == "ubah_tanggal_lahir":
        set_state(chat_id, "WAIT_BIRTH_DATE")
        await query.edit_message_text(
            "📅 Ketik tanggal lahir bayi dengan format:\n\n"
            "DD-MM-YYYY\n\n"
            "Contoh: 03-07-2026"
        )
        return

    if data == "snooze_asi":
        schedule_once(context, chat_id, "asi", DEFAULT_SNOOZE_MINUTES)
        await query.edit_message_text(
            f"⏰ Reminder menyusu ditunda {DEFAULT_SNOOZE_MINUTES} menit.",
            reply_markup=main_menu(),
        )
        return

    if data == "snooze_asip":
        schedule_once(context, chat_id, "asip", DEFAULT_SNOOZE_MINUTES)
        await query.edit_message_text(
            f"⏰ Reminder ASIP ditunda {DEFAULT_SNOOZE_MINUTES} menit.",
            reply_markup=main_menu(),
        )
        return

    if data == "snooze_pump":
        schedule_once(context, chat_id, "pump", DEFAULT_SNOOZE_MINUTES)
        await query.edit_message_text(
            f"⏰ Reminder pompa ASI ditunda {DEFAULT_SNOOZE_MINUTES} menit.",
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

    if state == "WAIT_FAMILY_CODE":
        ok, error = join_family(chat_id, text)
        set_state(chat_id, None)

        if not ok:
            await update.message.reply_text(
                f"❌ {error}\n\nPeriksa kembali kode keluarga."
            )
            return

        ensure_chat(chat_id)
        # Jadwalkan reminder untuk akun yang baru bergabung.
        reschedule_from_last(context, chat_id)

        await update.message.reply_text(
            "✅ Berhasil bergabung ke keluarga.\n\n"
            "Sekarang data bayi, riwayat, statistik, dan perkembangan memakai data yang sama.",
            reply_markup=main_menu(),
        )
        return

    if state == "WAIT_FAMILY_ROLE":
        role = text.strip()[:30]
        family = ensure_family(chat_id)
        with db() as conn:
            conn.execute(
                "UPDATE family_members SET role_name = ? WHERE chat_id = ?",
                (role, chat_id),
            )
        set_state(chat_id, None)
        await update.message.reply_text(
            f"✅ Nama/peran disimpan: {role}",
            reply_markup=family_menu(),
        )
        return

    if state == "WAIT_GROWTH_DATE":
        try:
            record_date = datetime.strptime(text, "%d-%m-%Y").date()
            if record_date > now_local().date():
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Tanggal tidak valid. Gunakan format DD-MM-YYYY.\n"
                "Contoh: 11-07-2026"
            )
            return

        context.user_data["growth_record_date"] = record_date.strftime("%Y-%m-%d")
        set_state(chat_id, "WAIT_GROWTH_WEIGHT")

        await update.message.reply_text(
            "⚖️ Masukkan berat bayi dalam kilogram.\n\n"
            "Contoh: 3.25"
        )
        return

    if state == "WAIT_GROWTH_WEIGHT":
        try:
            weight = float(text.replace(",", "."))
            if weight <= 0 or weight > 30:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Berat tidak valid. Masukkan angka dalam kilogram.\n"
                "Contoh: 3.25"
            )
            return

        context.user_data["growth_weight"] = weight
        set_state(chat_id, "WAIT_GROWTH_LENGTH")

        await update.message.reply_text(
            "📐 Masukkan panjang badan bayi dalam cm.\n\n"
            "Contoh: 50"
        )
        return

    if state == "WAIT_GROWTH_LENGTH":
        try:
            length_cm = float(text.replace(",", "."))
            if length_cm <= 0 or length_cm > 150:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Panjang badan tidak valid. Masukkan angka dalam cm.\n"
                "Contoh: 50"
            )
            return

        record_type = context.user_data.get("growth_record_type")
        record_date = context.user_data.get("growth_record_date")
        weight = context.user_data.get("growth_weight")

        if not record_type or not record_date or weight is None:
            set_state(chat_id, None)
            await update.message.reply_text(
                "Data perkembangan belum lengkap. Silakan ulangi dari menu 📏 Perkembangan.",
                reply_markup=main_menu(),
            )
            return

        add_growth_record(
            chat_id=chat_id,
            record_type=record_type,
            record_date=record_date,
            weight_kg=weight,
            length_cm=length_cm,
        )

        set_state(chat_id, None)

        await update.message.reply_text(
            f"✅ Data perkembangan berhasil disimpan\n\n"
            f"Jenis: {growth_type_label(record_type)}\n"
            f"Tanggal: {datetime.strptime(record_date, '%Y-%m-%d').strftime('%d-%m-%Y')}\n"
            f"Berat: {weight:.2f} kg\n"
            f"Panjang: {length_cm:.1f} cm",
            reply_markup=growth_menu(),
        )
        return

    if state == "WAIT_BABY_NAME":
        update_baby_name(chat_id, text)
        set_state(chat_id, None)
        await update.message.reply_text(
            f"✅ Nama bayi disimpan: {text}",
            reply_markup=main_menu(),
        )
        return

    if state == "WAIT_BIRTH_DATE":
        try:
            birth = datetime.strptime(text, "%d-%m-%Y").date()
            if birth > now_local().date():
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Tanggal tidak valid. Gunakan format DD-MM-YYYY.\n\n"
                "Contoh: 03-07-2026"
            )
            return

        stored_date = birth.strftime("%Y-%m-%d")
        update_birth_date(chat_id, stored_date)
        set_state(chat_id, None)

        await update.message.reply_text(
            f"✅ Tanggal lahir berhasil disimpan.\n\n"
            f"📅 {format_birth_date(stored_date)}\n"
            f"🎂 Umur sekarang: {calculate_age(stored_date)}",
            reply_markup=main_menu(),
        )
        return

    if state == "WAIT_PUMP_ML":
        try:
            amount = int(text)
            if amount <= 0 or amount > 1000:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Mohon ketik jumlah ASI hasil pompa dalam angka ml. Contoh: 80"
            )
            return

        add_event(chat_id, "pump", amount_ml=amount)
        set_state(chat_id, None)
        schedule_once(context, chat_id, "pump", DEFAULT_PUMP_MINUTES)
        next_pump = now_local() + timedelta(minutes=DEFAULT_PUMP_MINUTES)
        await update.message.reply_text(
            f"✅ Pompa ASI berhasil dicatat\n\n"
            f"Hasil pompa: {amount} ml\n"
            f"Jam: {now_local().strftime('%H:%M')}\n"
            f"⏰ Reminder berulang dihentikan.\n"
            f"Jadwal pompa berikutnya sekitar {next_pump.strftime('%H:%M')}.\n\n"
            "Semangat, Bunda ❤️",
            reply_markup=main_menu(),
        )
        return

    if state == "WAIT_ASIP_ML":
        try:
            amount = int(text)
            if amount <= 0 or amount > 500:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Mohon ketik jumlah ASIP dalam angka ml. Contoh: 75")
            return

        add_event(chat_id, "asip", amount_ml=amount)
        set_state(chat_id, None)
        chat = get_chat(chat_id)
        schedule_once(context, chat_id, "asip", int(chat["asi_minutes"]))
        next_asip = now_local() + timedelta(minutes=int(chat["asi_minutes"]))
        await update.message.reply_text(
            f"✅ ASIP berhasil dicatat\n\n"
            f"Jumlah diminum: {amount} ml\n"
            f"Jam: {now_local().strftime('%H:%M')}\n"
            f"⏰ Reminder ASIP berikutnya sekitar {next_asip.strftime('%H:%M')}.",
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
    count_pump = sum(1 for e in events if e["event_type"] == "pump")
    count_pipis = sum(1 for e in events if e["event_type"] == "popok_pipis")
    count_bab = sum(1 for e in events if e["event_type"] == "popok_bab")
    count_keduanya = sum(1 for e in events if e["event_type"] == "popok_keduanya")
    bf_minutes = sum((e["amount_ml"] or 0) for e in events if e["event_type"] == "bf_session")

    ml_asip = sum((e["amount_ml"] or 0) for e in events if e["event_type"] == "asip")
    ml_formula = sum((e["amount_ml"] or 0) for e in events if e["event_type"] == "formula")
    ml_pump = sum((e["amount_ml"] or 0) for e in events if e["event_type"] == "pump")

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
        "📊 Ringkasan Hari Ini\n\n"
        f"🍼 Menyusu: {count_asi} kali\n"
        f"⏱️ Durasi menyusu: {format_duration_minutes(bf_minutes)}\n"
        f"💩 Ganti popok: {count_popok} kali\n"
        f"   💧 Pipis: {count_pipis} | 💩 BAB: {count_bab} | 💧💩 Keduanya: {count_keduanya}\n"
        f"😴 Tidur: {sleep_h} jam {sleep_m} menit\n\n"
        f"🍼 ASIP: {count_asip} kali / {ml_asip} ml\n"

        f"🤱 Pompa ASI: {count_pump} kali / {ml_pump} ml\n\n"
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
        ("health", "Cek status bot"),
    ])

    # Kalau bot restart, jadwal reminder akan dibuat ulang dari data terakhir
    for chat_id in get_all_chats():
        reschedule_from_last(application, chat_id)
        schedule_daily_summary(application, chat_id)
        schedule_weekly_pdf(application, chat_id)

    logger.info("Baby Care Bot siap berjalan dengan mode hemat resource.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN belum diisi. Tambahkan environment variable BOT_TOKEN.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("Baby Care Bot berjalan...")
    app.run_polling(
        drop_pending_updates=True,
        poll_interval=2.0,
        timeout=20,
    )


if __name__ == "__main__":
    main()
