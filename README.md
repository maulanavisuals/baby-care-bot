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

- Profil bayi menyimpan tanggal lahir dan menghitung umur otomatis dalam tahun, bulan, dan hari.

- Reminder menyusu dan pompa ASI akan diulang setiap 15 menit sampai aktivitas dicatat.


- Timer menyusu kiri/kanan dan durasi otomatis.
- Detail popok: pipis, BAB, atau keduanya.
- Dashboard jadwal berikutnya.
- Riwayat 10 catatan terakhir.
- Statistik 7 hari terakhir.

- Menu Catat ASIP sekarang khusus ASIP.
- Pilihan cepat 30, 60, 90, 120, 150, 180 ml dan input manual.
- Setelah dicatat, reminder ASIP berikutnya mengikuti interval menyusu.
- Audio alarm dihapus. Reminder sekarang berupa notifikasi teks yang lebih rapi dan tetap berulang.

- Fitur perkembangan bayi: berat dan panjang saat lahir, keluar RS, dan setiap kontrol DSA.
- Setiap kontrol DSA dapat memilih tanggal pencatatan.
- Riwayat perkembangan menampilkan perubahan berat dibanding berat lahir.
- Data perkembangan terakhir ditampilkan di Profil Bayi.

- Menyusu langsung tidak memiliki reminder otomatis.
- Reminder aktif hanya untuk ASIP, ganti popok, dan pompa ASI.
- Interval sebelumnya yang bernama interval menyusu kini dipakai sebagai interval ASIP.

- Mode Keluarga: beberapa akun Telegram dapat memakai satu data bayi yang sama.
- Akun kedua dapat bergabung menggunakan kode keluarga.
- Statistik, riwayat, profil, dan perkembangan bayi tersinkron.
- Setiap anggota dapat mengatur nama/peran seperti Ayah, Bunda, Nenek, atau Pengasuh.
- Aktivitas penting dapat memberi notifikasi sinkronisasi ke anggota keluarga lain.
- Reminder berjalan pada setiap akun keluarga yang sudah membuka/mengaktifkan bot.
