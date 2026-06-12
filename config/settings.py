import os
from dotenv import load_dotenv

load_dotenv()

# MetaTrader 5 / Exness
MT5_LOGIN    = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER   = os.getenv("MT5_SERVER", "Exness-MT5Trial")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")


# Currency Handling
ACCOUNT_CURRENCY = "IDR"  # Set to "USD" if your account is in USD
IDR_TO_USD_RATE  = 16200.0  # Fallback rate — overridden at runtime by live USDIDR tick

# Risk Management
DEFAULT_RISK_PERCENT  = 0.01    # 1% equity per trade
DEFAULT_SL_PIPS       = 15      # pips (Scalping: tight SL)
DEFAULT_TP_PIPS       = 30      # pips (RR 2:1)
PIP_VALUE_USD         = 10.0    # USD per pip per standard lot (EURUSD)
MIN_LOT               = 0.01
MAX_LOT               = 10.0

# JPY pairs use different pip multiplier (0.01 instead of 0.0001)
JPY_PAIRS = {"USDJPY", "EURJPY", "GBPJPY", "CADJPY", "AUDJPY", "CHFJPY", "NZDJPY"}

# ATR-based SL/TP (Tightened for scalping)
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 3.0
ATR_LENGTH        = 14

# Market Regime (ADX)
ADX_TRENDING_THRESHOLD = 28.0
ADX_RANGING_THRESHOLD  = 20.0
ADX_LENGTH             = 14

# Analyzer
RSI_OVERBOUGHT    = 70.0
RSI_OVERSOLD      = 30.0
RSI_BULLISH_ZONE  = 60.0
RSI_BEARISH_ZONE  = 40.0
BB_NEAR_BAND      = 0.1
EMA_RANGING_GAP_PCT = 0.05

# Auto-execute circuit breaker
AUTO_EXEC_DAILY_LOSS_LIMIT    = -2.0   # cumulative RR — halts execution below this
AUTO_EXEC_DAILY_USD_LIMIT     = None   # optional: halt if USD loss ≤ this (e.g. -200.0)
AUTO_EXEC_EQUITY_DRAWDOWN_PCT = 5.0    # halt if equity drops ≥ this % from session start

# Exposure limits
MAX_OPEN_POSITIONS  = 5      # hard cap on simultaneous positions
MAX_TOTAL_RISK_PCT  = 5.0    # total open risk as % of equity
MAX_CORRELATED      = 2      # max positions from same correlation group
MIN_MARGIN_LEVEL_PCT = 300.0 # halt new entries if margin level < this %
                              # Exness Standard margin call = 100%, buffer 3×
                              # Exness Pro margin call = 50%, buffer 6×

# Scan config
TOP_SYMBOLS_N        = 10
TOP_SYMBOLS_CACHE_TTL = 1800  # 30 min

# Weekend close — Jumat 21:00 WIB = 14:00 UTC
WEEKEND_CLOSE_HOUR_UTC    = 14
WEEKEND_CLOSE_MINUTE_UTC  = 0

# Reconciler periodic interval (seconds)
RECONCILE_INTERVAL = 60    # every 1 minute

# ML
ML_MODEL_PATH               = "data/ml_model.joblib"
ML_RANDOM_FOREST_ESTIMATORS = 100
ML_TARGET_LOOKAHEAD_PERIODS = 6   # 6 × 5m = 30 min — more robust scalping lookahead
ML_FEATURE_COLUMNS = [
    "rsi", "ema_20", "ema_50",
    "MACD_12_26_9", "MACDs_12_26_9", "MACDh_12_26_9",
    "ADX_14", "DMP_14", "DMN_14", "ATRr_14",
    "BBP_20_2.0",  # BB percent — used in signal logic, must match ML features
]
