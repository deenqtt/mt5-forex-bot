# Windows Setup Guide — MT5 Forex Bot

> Panduan lengkap setup dari nol sampai bot jalan di Windows.
> Ikuti urutan step — jangan skip.

---

## Daftar Isi

1. [Software yang Dibutuhkan](#1-software-yang-dibutuhkan)
2. [Setup Akun Exness](#2-setup-akun-exness)
3. [Setup MT5 Terminal](#3-setup-mt5-terminal)
4. [Clone dan Install Project](#4-clone-dan-install-project)
5. [Konfigurasi .env](#5-konfigurasi-env)
6. [Jalankan Bot](#6-jalankan-bot)
7. [Setup Telegram Bot](#7-setup-telegram-bot)
8. [Auto-Start saat Windows Nyala](#8-auto-start-saat-windows-nyala)
9. [Demo Testing Checklist](#9-demo-testing-checklist)
10. [Troubleshooting](#10-troubleshooting)
11. [Catatan Penting Exness](#11-catatan-penting-exness)

---

## 1. Software yang Dibutuhkan

### Python 3.12

1. Buka browser → `https://python.org/downloads`
2. Download **Python 3.12.x** (pilih yang terbaru)
3. Jalankan installer
4. **WAJIB** centang `Add Python to PATH` sebelum klik Install
5. Verifikasi setelah install:

```cmd
python --version
```

Output harus: `Python 3.12.x`

---

### Git

1. Buka `https://git-scm.com/download/win`
2. Download dan install (semua setting default OK)
3. Verifikasi:

```cmd
git --version
```

---

### MT5 Terminal (Exness)

1. Login ke `my.exness.com`
2. Pergi ke **Platforms → MetaTrader 5 → Download for Windows**
3. Install MT5
4. **Jangan login dulu** — setup akun dulu di Step 2

---

## 2. Setup Akun Exness

### Buat Akun Demo (Wajib Dulu Sebelum Live)

1. Login `my.exness.com`
2. Klik **Open New Account**
3. Pilih:
   - Platform: **MetaTrader 5**
   - Type: **Standard** (recommended untuk mulai)
   - Mode: **Demo**
   - Currency: **USD**
   - Leverage: **1:100** atau **1:200** (jangan 1:2000 untuk testing awal)
4. Catat informasi ini — diperlukan untuk `.env`:

```
Login     : [nomor 8 digit, contoh: 12345678]
Password  : [password yang kamu set]
Server    : Exness-MT5Trial
```

### Aktifkan Keamanan Akun

1. `my.exness.com → Security`
2. Aktifkan **Two-Factor Authentication (2FA)**
3. Simpan backup codes di tempat aman

### Catatan Server

| Mode | Server Name |
|------|-------------|
| Demo | `Exness-MT5Trial` |
| Live | `Exness-MT5Real` |

**Pastikan `.env` pakai server yang benar sesuai mode yang digunakan.**

---

## 3. Setup MT5 Terminal

### Login ke Akun Demo

1. Buka MT5 Terminal
2. Klik **File → Login to Trade Account**
3. Isi:
   - Login: [nomor akun demo]
   - Password: [password demo]
   - Server: `Exness-MT5Trial`
4. Klik **Login**
5. Pastikan indikator koneksi di kanan bawah berwarna **hijau**

### Aktifkan Auto Trading

> Wajib agar Python dapat mengirim order ke MT5.

1. `Tools → Options → Expert Advisors`
2. Centang semua:
   - ✅ Allow automated trading
   - ✅ Allow DLL imports
   - ✅ Allow imports of external experts
3. Klik **OK**
4. Pastikan tombol **Auto Trading** di toolbar MT5 berwarna **hijau**

### Pastikan Symbols Tersedia

1. Klik kanan di **Market Watch** (panel kiri)
2. Pilih **Show All**
3. Verifikasi symbol ini ada dan visible:
   - EURUSD
   - GBPUSD
   - USDJPY
   - XAUUSD
   - GBPJPY
   - EURJPY

Jika ada yang tidak muncul: klik kanan → **Show** → cari symbol secara manual.

### Biarkan MT5 Tetap Berjalan

> **Bot membutuhkan MT5 Terminal aktif dan login.**
> Jangan tutup MT5 saat bot sedang berjalan.

---

## 4. Clone dan Install Project

Buka **Command Prompt** atau **PowerShell**, jalankan perintah berikut satu per satu:

```cmd
cd C:\Users\%USERNAME%\Desktop

git clone git@github.com:deenqtt/mt5-forex-bot.git

cd mt5-forex-bot

python -m venv venv

venv\Scripts\activate

pip install -r requirements.txt
```

### Verifikasi MetaTrader5 Terinstall

```cmd
python -c "import MetaTrader5 as mt5; print('MT5 OK:', mt5.initialize())"
```

Output harus: `MT5 OK: True`

Jika `False`: pastikan MT5 Terminal sudah buka dan login terlebih dahulu.

---

## 5. Konfigurasi .env

Di folder project, buat file baru bernama `.env` (tanpa ekstensi lain):

```
MT5_LOGIN=12345678
MT5_PASSWORD=your_demo_password_here
MT5_SERVER=Exness-MT5Trial

TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id

GEMINI_API_KEY=
```

> **Jangan commit file `.env` ke GitHub.**
> File ini sudah ada di `.gitignore` — aman.

### Cara Dapat Telegram Bot Token

1. Buka Telegram → cari `@BotFather`
2. Kirim `/newbot`
3. Ikuti instruksi (isi nama bot)
4. BotFather akan kirim **token** seperti: `1234567890:ABCdef...`
5. Copy token tersebut ke `TELEGRAM_BOT_TOKEN`

### Cara Dapat Telegram Chat ID

1. Buka Telegram → cari `@userinfobot`
2. Klik **Start** atau kirim sembarang pesan
3. Bot akan balas dengan **ID** kamu (angka)
4. Copy angka tersebut ke `TELEGRAM_CHAT_ID`

---

## 6. Jalankan Bot

Pastikan **MT5 Terminal sudah login** sebelum jalankan bot.

```cmd
cd C:\Users\%USERNAME%\Desktop\mt5-forex-bot
venv\Scripts\activate
python main.py
```

### Output Yang Benar

```
INFO MT5 connected: server=Exness-MT5Trial login=12345678 balance=10000.00
INFO Running startup reconciliation...
INFO Reconcile clean: {'matched': 0, 'closed': 0, 'orphans': 0, 'errors': 0}
INFO Forex Bot MT5 starting — polling Telegram
```

### Test Via Telegram

Kirim perintah ini ke bot kamu:

```
/start          → bot harus reply menu
/balance        → harus tampil saldo demo
/analyze EURUSD → harus tampil analisis signal
/status         → harus tampil status sistem
```

---

## 7. Setup Telegram Bot

### Perintah yang Tersedia

| Command | Fungsi |
|---------|--------|
| `/start` | Tampilkan menu |
| `/balance` | Cek saldo dan equity |
| `/analyze SYMBOL` | Analisis + tombol BUY/SELL manual |
| `/status` | Posisi terbuka + circuit breaker status |
| `/close SYMBOL` | Tutup posisi manual |
| `/closeall` | Tutup semua posisi |
| `/train SYMBOL TF` | Training ulang ML model |
| `/report` | Performance summary |
| `/auto_on` | Aktifkan alert scan |
| `/auto_off` | Matikan alert scan |
| `/auto_exec_on` | ⚡ Aktifkan auto execute |
| `/auto_exec_off` | Matikan auto execute |

### Mode Operasi

**Mode 1 — Manual:**
```
/analyze EURUSD → lihat analisis → klik BUY atau SELL → konfirmasi
```

**Mode 2 — Alert Only:**
```
/auto_on → bot scan otomatis, kirim alert signal → kamu decide manual
```

**Mode 3 — Full Auto:**
```
/auto_exec_on → bot scan + execute otomatis dengan semua gate aktif
```

> Mulai dengan Mode 1 atau 2 selama demo testing.
> Mode 3 hanya setelah kamu familiar dengan perilaku bot.

---

## 8. Auto-Start saat Windows Nyala

Agar bot dan MT5 otomatis start setelah Windows restart (penting untuk VPS).

### MT5 Auto-Login

1. `MT5 → Tools → Options → Server`
2. Centang **Keep personal settings and data on this computer**
3. MT5 akan auto-login menggunakan credentials terakhir saat dibuka

### Bot Auto-Start via Task Scheduler

1. Buka **Task Scheduler** (cari di Start Menu)
2. Klik **Create Basic Task**
3. Isi:
   - Name: `MT5 Forex Bot`
   - Description: Auto-start trading bot
4. **Trigger:** When the computer starts
5. **Action:** Start a program
   - Program: `C:\Users\%USERNAME%\Desktop\mt5-forex-bot\venv\Scripts\python.exe`
   - Arguments: `main.py`
   - Start in: `C:\Users\%USERNAME%\Desktop\mt5-forex-bot`
6. Klik **Finish**
7. Klik kanan task yang baru dibuat → **Properties**
8. Tab **General:**
   - ✅ Run whether user is logged on or not
   - ✅ Run with highest privileges
9. Tab **Settings:**
   - ✅ If the task fails, restart every: **5 minutes**
   - Attempt to restart up to: **3 times**
10. Klik **OK**

### Tambah Delay untuk MT5 (Opsional)

Buat file `start_bot.bat` di folder project:

```bat
@echo off
echo Waiting for MT5 to initialize...
timeout /t 30 /nobreak
cd /d C:\Users\%USERNAME%\Desktop\mt5-forex-bot
call venv\Scripts\activate
python main.py
```

Gunakan `start_bot.bat` sebagai program di Task Scheduler.
Delay 30 detik memberi waktu MT5 login sebelum bot start.

---

## 9. Demo Testing Checklist

Jalankan semua test ini sebelum live trading.

### Test Koneksi

```
□ MT5 terhubung (indikator hijau di MT5)
□ /balance menampilkan saldo demo yang benar
□ python main.py output tidak ada ERROR di log
```

### Test Signal

```
□ /analyze EURUSD berjalan tanpa error
□ Analisis menampilkan regime, composite score, HTF bias
□ Tombol BUY/SELL muncul
```

### Test Eksekusi Manual

```
□ /analyze EURUSD → klik BUY → konfirmasi Ya
□ Order masuk di MT5 Terminal (cek Trade tab)
□ /status menampilkan posisi terbuka
□ /close EURUSD → posisi tertutup
□ Cek file data/trade_history.csv — trade tercatat
```

### Test Auto Mode

```
□ /auto_on → bot mulai scan (tunggu alert signal)
□ /auto_exec_on → tunggu 1 trade masuk otomatis
□ /status → posisi terbuka terbaca
□ Biarkan SL/TP hit → bot kirim notifikasi otomatis
```

### Test Recovery

```
□ Tutup bot (Ctrl+C) saat ada posisi terbuka
□ Restart bot
□ /status → posisi masih terbaca (state persist)
□ Bot langsung monitoring posisi lama
```

### Test Circuit Breaker

```
□ Cek /status → tampilkan circuit breaker status
□ Pastikan daily RR limit, drawdown terbaca
```

### Test Weekend Scenario

```
□ Cek bot tidak buka posisi baru Sabtu/Minggu
□ Friday sebelum jam 14:00 UTC: bot masih bisa execute
□ Friday setelah jam 14:00 UTC: bot close semua posisi otomatis
```

---

## 10. Troubleshooting

### `MT5 init failed` saat bot start

```
Penyebab : MT5 Terminal belum buka atau belum login
Solusi   : Buka MT5 → login ke akun → tunggu koneksi hijau → restart bot
```

### `No tick for EURUSD`

```
Penyebab : MT5 tidak terhubung ke broker atau market tutup
Solusi   : Cek koneksi MT5 (kanan bawah harus hijau)
           Cek apakah market sedang tutup (weekend/hari libur)
```

### Order gagal `TRADE_RETCODE_INVALID_STOPS`

```
Penyebab : SL/TP terlalu dekat dengan harga (broker stop level)
Solusi   : ATR terlalu kecil → pasar sedang sangat quiet
           Bot akan skip otomatis, tidak perlu action manual
```

### Bot tidak reply di Telegram

```
Penyebab : TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_ID salah di .env
Solusi   : Cek kembali .env
           Pastikan tidak ada spasi atau karakter extra
           Coba /start dari akun Telegram yang CHAT_ID-nya terdaftar
```

### `pip install` error di Windows

```
Penyebab : Python atau pip tidak di PATH
Solusi   :
1. Buka Control Panel → System → Advanced → Environment Variables
2. Di PATH, pastikan ada: C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\
3. Atau reinstall Python dengan centang "Add to PATH"
```

### Task Scheduler tidak jalan

```
Penyebab : Path salah atau permission tidak cukup
Solusi   :
1. Test dulu manual: jalankan start_bot.bat langsung
2. Pastikan path di Task Scheduler menggunakan path absolut penuh
3. Run as administrator saat setup Task Scheduler
```

---

## 11. Catatan Penting Exness

### Demo Account

- Kedaluwarsa setelah **30 hari tidak aktif**
- Saldo bisa di-reset kapan saja dari portal Exness
- Spread demo ≈ spread live untuk Exness Standard

### Margin & Stop Out

| Setting | Exness Standard |
|---------|----------------|
| Margin Call | 100% |
| Stop Out | 50% |
| Buffer aman (bot) | 300% |

Bot sudah dikonfigurasi dengan `MIN_MARGIN_LEVEL_PCT = 300` — tidak akan buka posisi baru jika margin level di bawah 300%.

### Leverage yang Direkomendasikan

| Mode | Leverage |
|------|----------|
| Demo testing | 1:100 |
| Live awal | 1:100 atau 1:200 |
| Live setelah profitable | Sesuaikan |

> Jangan gunakan leverage 1:2000 saat awal.
> High leverage memperbesar profit DAN loss.

### Server yang Benar

| Akun | Server di .env |
|------|---------------|
| Demo | `Exness-MT5Trial` |
| Live | `Exness-MT5Real` |

**Salah server = bot tidak bisa connect ke akun.**

---

## File Struktur Project

```
mt5-forex-bot/
├── main.py                    ← Entry point, jalankan ini
├── .env                       ← Credentials (jangan di-share)
├── .env.example               ← Template .env
├── requirements.txt           ← Dependencies
├── config/
│   └── settings.py            ← Semua parameter konfigurasi
├── core/
│   ├── connection/            ← MT5 singleton connection
│   ├── execution/             ← Order engine dengan retry
│   ├── risk/                  ← Semua filter dan circuit breaker
│   ├── state/                 ← Persistence posisi dan history
│   └── monitoring/            ← Monitor posisi terbuka
├── strategy/
│   ├── indicators.py          ← Kalkulasi indikator teknikal
│   └── ml_model.py            ← RandomForest prediction
├── telegram_interface/
│   └── bot_handlers.py        ← Semua command Telegram
└── data/                      ← Generated saat runtime
    ├── positions.json          ← Posisi terbuka (auto-created)
    └── trade_history.csv       ← History semua trade (auto-created)
```

---

## Parameter Penting di settings.py

Sebelum live, review dan sesuaikan parameter ini:

```python
# Risk per trade
DEFAULT_RISK_PERCENT = 0.01    # 1% equity — recommended untuk mulai

# Batas posisi
MAX_OPEN_POSITIONS   = 5       # Maksimum posisi simultan
MAX_TOTAL_RISK_PCT   = 5.0     # Total risk maksimum % dari equity

# Circuit breaker
AUTO_EXEC_DAILY_LOSS_LIMIT    = -2.0   # Stop jika rugi 2R per hari
AUTO_EXEC_EQUITY_DRAWDOWN_PCT = 5.0    # Stop jika equity turun 5%

# Margin protection
MIN_MARGIN_LEVEL_PCT = 300.0   # Jangan entry jika margin level < 300%

# Weekend close
WEEKEND_CLOSE_HOUR_UTC = 14    # Tutup semua posisi Jumat jam 14:00 UTC
```

---

*Dokumen ini dibuat untuk deployment bot ke Windows.*
*Selalu mulai dengan demo account sebelum live trading.*
