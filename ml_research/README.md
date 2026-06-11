# AutoML Research Pipeline

Pipeline riset ML mandiri — melatih model dengan Walk-Forward Optimization (WFO) yang jauh lebih robust dibanding training biasa di bot.

**Hasil:** Model `.joblib` baru di folder `data/` yang langsung dipakai bot tanpa perubahan apapun.

---

## Persiapan (Sekali Saja)

### 1. Install dependencies tambahan

```bash
pip install optuna lightgbm
```

> **Catatan:** `optuna` dan `lightgbm` opsional. Kalau tidak diinstall, pipeline tetap jalan pakai RandomForest default (tanpa HPO).

### 2. Pastikan MT5 sudah nyala

Bot harus bisa konek ke MT5. Pipeline ini butuh koneksi MT5 untuk fetch data historis.

---

## Cara Jalankan

Semua command dijalankan dari **root folder project** (`mt5-forex-bot/`).

### Run dasar (4 symbol default)

```bash
python ml_research/run_research.py
```

Symbol default: `EURUSD`, `GBPUSD`, `USDJPY`, `XAUUSD`

---

### Pilih symbol sendiri

```bash
python ml_research/run_research.py --symbols EURUSD GBPJPY XAUUSD
```

---

### Test dulu tanpa overwrite model (Recommended pertama kali)

```bash
python ml_research/run_research.py --dry-run
```

Pipeline jalan penuh, tapi file `.joblib` **tidak ditimpa**. Berguna untuk lihat hasil sebelum deploy.

---

### Tambah jumlah Optuna trials (akurasi lebih tinggi, lebih lambat)

```bash
python ml_research/run_research.py --trials 50
```

Default: 30 trials per fold. Lebih banyak trials = lebih lama tapi model lebih optimal.

---

### Force refresh data (bypass cache)

```bash
python ml_research/run_research.py --refresh
```

Default: data di-cache 24 jam. Pakai `--refresh` kalau mau fetch ulang dari MT5.

---

### Kombinasi semua opsi

```bash
python ml_research/run_research.py --symbols EURUSD GBPUSD --trials 50 --dry-run
```

---

## Output

### Log per symbol (contoh)

```
─────────────────────────────────────────────────────────────────
SYMBOL: EURUSD
─────────────────────────────────────────────────────────────────
Data: 58432 candles  2024-08-01 → 2026-06-11
EURUSD WFO: 58432 candles | cpm≈2880 | train=17280 val=2880 oos=2880 step=2880 → ~8 folds

  Fold 1/8  train[0:17280] val[17280:20160] oos[20160:23040]
    → OOS PF=1.42 WR=54.2% Exp=2.1pip Sharpe=1.23 DD=8.1% composite=0.412 ✅

  Fold 2/8  train[2880:20160] ...
    → OOS PF=1.31 WR=51.8% Exp=1.6pip Sharpe=0.98 DD=11.2% composite=0.334 ✅

  ...

EURUSD WFO done: 8 folds | best fold=1 composite=0.412 | mean PF=1.38 WR=53.1% passes_all=True

✅ EURUSD: Model deployed → data/ml_model_EURUSD.joblib
```

### Tabel ringkasan akhir

```
═════════════════════════════════════════════════════════════════
FINAL SUMMARY
═════════════════════════════════════════════════════════════════
Symbol     Status     Folds     PF      WR   Exp(p)   Score
─────────────────────────────────────────────────────────────────
EURUSD     DEPLOYED       8   1.38   53.1%     2.1   0.412
GBPUSD     DEPLOYED       8   1.29   51.4%     1.8   0.358
USDJPY     SKIPPED        8   1.12   49.2%     0.9   -1.000
XAUUSD     DEPLOYED       7   1.51   55.3%     3.2   0.487
```

**DEPLOYED** = model baru aktif di `data/`
**SKIPPED** = ada fold yang gagal spread check → model lama tetap dipakai

---

## Cara Kerja (Singkat)

```
Fetch 65.000 candle 5m dari MT5  →  cache CSV 24 jam
         ↓
Walk-Forward Optimization (rolling window):
  Train 6 bulan → Val 1 bulan → OOS 1 bulan → geser 1 bulan → ulangi
         ↓
Per fold: Optuna cari hyperparameter terbaik (RF atau LightGBM)
         ↓
Backtest OOS dengan spread penalty:
  Expectancy harus ≥ 1.5× spread pip — kalau tidak, fold GAGAL
         ↓
Composite Score = 0.40×PF + 0.25×Exp + 0.15×Sharpe + 0.10×WR - 0.10×DD
         ↓
Semua fold LULUS? → export joblib
Ada satu fold GAGAL? → skip, model lama aman
```

---

## Jadwal yang Disarankan

Jalankan **setiap Sabtu pagi** sebelum market Asia buka (sebelum 06:00 WIB):

```bash
python ml_research/run_research.py --symbols EURUSD GBPUSD USDJPY XAUUSD --trials 40
```

Estimasi waktu:
- 4 symbol × ~8 fold × 30 trials = **15–30 menit** (tergantung CPU)
- Dengan `--trials 50` = **25–45 menit**

---

## Troubleshooting

### "Not enough data for WFO"

```
ValueError: Not enough data for WFO: need 49152 candles, got 12000.
```

MT5 tidak kasih cukup data historis. Solusi:
1. Pastikan MT5 terminal sudah download history — buka chart EURUSD 5m, scroll ke kiri sampai mentok
2. Di MT5: Tools → Options → Charts → Max bars in chart → set ke 100000+

---

### "Cannot connect to MT5"

Pastikan:
- MT5 terminal sudah terbuka dan logged in
- `config/.env` sudah berisi `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` yang benar

---

### Model SKIPPED terus

Artinya model tidak profitable setelah dikurangi spread + slippage. Kemungkinan penyebab:
- Symbol terlalu volatile / spread terlalu lebar (misal GBPJPY)
- Timeframe terlalu kecil untuk target lookahead yang diset
- Data tidak cukup bervariasi (hanya satu regime market)

Coba: `--trials 50` untuk HPO lebih agresif, atau kurangi symbol ke pair mayor saja.

---

### Optuna tidak terinstall

Pipeline tetap jalan dengan RandomForest default parameter. Output tetap valid, hanya tidak di-tune.

```
WARNING: Optuna not installed — using default RF (pip install optuna)
```

---

## File yang Dihasilkan

| File | Keterangan |
|------|-----------|
| `data/ml_model_EURUSD.joblib` | Model EURUSD siap pakai bot |
| `data/ml_model_GBPUSD.joblib` | Model GBPUSD siap pakai bot |
| `ml_research/data/EURUSD_5m.csv` | Cache data historis + indikator |

Bot (`strategy/ml_model.py`) otomatis load model baru saat:
- Restart bot
- Command `/train EURUSD` (untuk quick retrain ulang)
