@echo off
echo [1/4] Installing core dependencies...
python -m pip install MetaTrader5 pandas scikit-learn joblib numpy python-telegram-bot[job-queue] python-dotenv tqdm colorama

echo [2/4] Installing Python 3.14 compatible numba (pre-release)...
python -m pip install numba>=0.65.1 --pre

echo [3/4] Installing pandas-ta (bypassing strict dependency check)...
python -m pip install pandas-ta>=0.3.14b --no-deps

echo [4/4] Verifying installation...
python -c "import pandas_ta; print('pandas-ta version:', pandas_ta.__version__)"
python -c "import MetaTrader5; print('MT5 version:', MetaTrader5.__version__)"

echo.
echo Setup selesai! Silakan jalankan bot dengan: python main.py
pause
