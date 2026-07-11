# Baby Care Bot Telegram

Bot Telegram untuk membantu Ayah & Bunda mencatat dan mengingat:
- Menyusu / ASI
- Ganti popok
- ASIP / susu formula
- Tidur
- Statistik harian
- Reminder otomatis
- Hapus/reset input terakhir jika salah mencatat

## File

- `bot_asi.py`
- `requirements.txt`
- `Procfile`

## Cara Deploy ke Railway

1. Buat bot di Telegram lewat @BotFather.
2. Ambil token bot.
3. Upload semua file ini ke GitHub.
4. Buka Railway.
5. Deploy from GitHub Repo.
6. Tambahkan Variable:

BOT_TOKEN=token_bot_kamu

7. Restart/Deploy ulang.
8. Buka bot di Telegram HP, lalu klik Start.

## Perintah Bot

/start - mulai bot
/menu - buka menu
/stats - statistik hari ini
/stop - hentikan reminder
/help - bantuan


## Optimasi Railway

Versi ini dibuat lebih ringan untuk pemakaian pribadi:

- SQLite tetap digunakan agar ringan.
- SQLite memakai WAL dan synchronous NORMAL.
- Long polling dibuat lebih santai.
- Reminder hanya berjalan saat diperlukan.
- Gunakan `/health` untuk mengecek status bot.

Environment variable yang disarankan:

```
BOT_TOKEN=token_bot_kamu
TIMEZONE=Asia/Jakarta
DEFAULT_ASI_MINUTES=150
DEFAULT_POPOK_MINUTES=240
DEFAULT_PUMP_MINUTES=120
DEFAULT_SNOOZE_MINUTES=15
```

- Statistik harian otomatis dikirim setiap pukul 23.59 WIB.

- Ringkasan harian menampilkan hari dan tanggal lengkap dalam Bahasa Indonesia.

- Catatan pompa ASI menyediakan pilihan cepat 30–180 ml dan input jumlah ml secara manual.
