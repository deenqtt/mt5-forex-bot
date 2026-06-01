# Project Readiness & System Audit - MT5 Forex Bot

Dokumen ini ditujukan untuk Claude/AI Assistant untuk melakukan analisa mendalam terhadap kesiapan strategi dan manajemen risiko bot.

## 1. Status Lingkungan (Environment)
- **OS:** Windows
- **Python:** 3.14.4 (Kondisi khusus, menggunakan pre-release numba)
- **Broker Connection:** MT5 (Verified & Connected)
- **Library Utama:** 
  - `pandas-ta`: Terinstal (Bypass dependencies)
  - `MetaTrader5`: Terinstal & Configured
  - `python-telegram-bot`: Terinstal

## 2. Struktur Core yang Perlu Dicek
Asisten diharapkan mengecek modul berikut:
- `core/risk/`: Logika `circuit_breaker` dan `exposure_gate`. Apakah kalkulasi lot dan risk-per-trade sudah aman?
- `strategy/indicators.py`: Apakah penggunaan `pandas-ta` sudah optimal meskipun tanpa dependensi strict?
- `core/state/reconciler.py`: Logika sinkronisasi jika bot mati mendadak.
- `ai_reasoner/analyzer.py`: Logika analisa market berdasarkan regime (Trending/Ranging).

## 3. Tugas untuk Claude/AI
Mohon lakukan analisa pada poin-poin berikut:
1.  **Risk Management Audit**: Cek apakah ada celah di mana bot bisa membuka posisi melebihi batas margin atau drawdown yang ditetapkan.
2.  **Strategy Validation**: Validasi logika `IndicatorManager.get_latest_signals`. Apakah perhitungan crossover dan ADX sudah sesuai standar teknikal?
3.  **Error Handling**: Apakah flow `OrderEngine` sudah cukup kuat menghadapi requote atau lonjakan spread?
4.  **Python 3.14 Edge Cases**: Karena menggunakan Python versi sangat baru, apakah ada fungsi async/await di `main.py` yang berpotensi menimbulkan race condition?

## 4. Cara Menjalankan untuk Debug
- Jalankan `.\setup_fix.bat` jika environment rusak.
- Jalankan `python main.py` untuk start.

---
*Silakan berikan feedback mengenai struktur kode dan potensi bug strategis.*
