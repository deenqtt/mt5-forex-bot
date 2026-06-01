# Log Perbaikan Teknis - MT5 Forex Bot

Dokumen ini mencatat masalah teknis yang ditemukan selama setup awal pada lingkungan Windows dengan Python 3.14.4.

## 1. Masalah Kompatibilitas Python 3.14 (Numba & Pandas-TA)
**Masalah:** 
`pip install -r requirements.txt` gagal karena `pandas-ta` membutuhkan `numba==0.61.2`. Versi numba tersebut tidak mendukung Python 3.14 dan gagal saat proses build (wheel).

**Solusi:**
- Menggunakan `numba>=0.65.1` (versi pre-release) yang mendukung Python 3.14.
- Menginstal `pandas-ta` dengan flag `--no-deps` untuk melewati pengecekan dependensi strict.
- Disediakan script `setup_fix.bat` untuk otomatisasi proses ini.

## 2. AttributeError pada Library MetaTrader5
**Masalah:** 
Error `AttributeError: module 'MetaTrader5' has no attribute 'TRADE_RETCODE_SERVER_DISCON'`. Konstanta ini tidak tersedia di versi library yang terinstal.

**Solusi:**
- Melakukan audit konstanta `TRADE_RETCODE` yang tersedia di library sistem.
- Menghapus referensi ke `TRADE_RETCODE_SERVER_DISCON` di `core/connection/mt5_session.py`.
- Memperbarui `RETRYABLE_CODES` dan `FATAL_CODES` agar sesuai dengan mapping konstanta terbaru (misal: `CONNECTION` adalah `10031`, bukan `10006`).

## 3. Konfigurasi Environment (.env)
**Masalah:** 
Bot gagal inisialisasi Telegram karena menggunakan placeholder `your_telegram_bot_token`.

**Status:** 
Koneksi MT5 sudah berhasil (`Authorization Success`). User perlu memperbarui Token Telegram dan Chat ID agar bot aktif sepenuhnya.

---
*Dibuat untuk referensi asisten AI (Claude/Gemini) agar memahami state teknis terakhir.*
