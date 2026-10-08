"""이벤트 드리븐 스윙 콕핏 v3.0 — 실적 런업 + 숏스퀴즈. LDPB 폐기."""
from __future__ import annotations

APP_TITLE = "이벤트 드리븐 스윙 콕핏 v3.0"
APP_PORT = 8502

# --- 공용 유니버스 (실적런업 + 숏스퀴즈 두 스캐너를 모두 포괄하는 폭) ---
PRICE_MIN = 3.0                    # 숏스퀴즈 하한. 실적런업은 자체 필터로 $10 을 별도 적용.
MARKET_CAP_MIN = 50_000_000        # $50M. 숏스퀴즈 하한.
MARKET_CAP_MAX = 50_000_000_000    # $50B. 실적런업 상한.
VOLUME_MORE_THAN = 300_000
ADV_USD_20D_MIN = 3_000_000
SCREENER_LIMIT = 4000
SCREENER_EXCHANGES = ("NASDAQ", "NYSE")
EOD_WORKERS = 8

# --- Data / heartbeat ---
YF_PERIOD = "1y"
YF_BATCH = 10
QUOTE_POLL_SEC = 1.0
HEARTBEAT_SEC = 1.0
CHART_SYNC_SEC = 0.5
UI_REFRESH_MS = 1000

# --- 실적 런업 (탭1 좌측) ---
EARN_HORIZON_DAYS = 15
EARN_PRICE_MIN = 10.0
EARN_CAP_MIN = 1_500_000_000
EARN_CAP_MAX = 50_000_000_000
EARN_ADV_MIN = 1_000_000
EARN_TOP_N = 10
EARN_MAX_ANALYST_CALLS = 60
EARN_FIRE_MIN_SCORE = 75.0
EARN_RATING_FULL_PCT = 70.0
EARN_UPSIDE_FULL_PCT = 15.0
EARN_BUDGET_KRW = 1_000_000

# --- 숏스퀴즈 (탭1 우측). 공매도는 yfinance shortPercentOfFloat. FINRA 미사용. ---
SQUEEZE_PRICE_MIN = 10.0
SQUEEZE_CAP_MIN = 1_000_000_000
SQUEEZE_CAP_MAX = 50_000_000_000   # 스캐너는 상한을 자르지 않는다. 하한만 적용.
SQUEEZE_ADV_MIN = 300_000          # 미사용. 1차 압축은 50일 거래량 배수.
SQUEEZE_RVOL_MIN = 1.5             # 당일거래량 / 50일 평균
SQUEEZE_TOP_N = 10
SQUEEZE_SHORT_FLOAT_MIN = 15.0     # % — 하드 필터
SQUEEZE_SHORTFLOAT_FULL = 30.0     # % — 40점 만점
SQUEEZE_RVOL_FULL = 3.0            # 당일/직전 50일 — 40점 만점
SQUEEZE_DTC_FULL = 5.0             # 참고용 표시 전용. 스코어에는 미반영.
SQUEEZE_UPSIDE_MIN_PCT = 5.0       # 하드필터: 월가 목표가 기준 상승여력이 이 값 미만이면 후보에서 제외
SQUEEZE_UPSIDE_FULL_PCT = 20.0     # 상승여력 20%+ → 20점 만점
SQUEEZE_FIRE_MIN_SCORE = 70.0
SQUEEZE_CHASE_5D_PCT = 40.0        # 최근 5거래일 +40% 초과 시 추격 금지
SQUEEZE_SI_MAX_CALLS = 100         # 1차 압축 후 yfinance 조회 상한
SQUEEZE_YF_WORKERS = 16
SQUEEZE_SI_CACHE_DAYS = 3          # 미사용. FINRA 캐시 폐기.
SQUEEZE_MAX_ANALYST_CALLS = 30     # 공매도 15%+ 통과한 후보에만 컨센서스 조회 (상한)

# --- 청산 헌법. 실적런업은 트레일링, 숏스퀴즈는 고정 익절 + 시간 손절. ---
# EXIT_TP_PCT 는 사냥 카드(event_driven)만 읽는다. guardian() 은 보지 않는다.
EXIT_TP_PCT = 4.0
EARN_TP_ARM_PCT = 4.0              # +4% 도달 시 트레일링 무장
EARN_TRAIL_DROP_PCT = 3.0          # 무장 후 고점 대비 -3% 반락이면 익절
SQZ_TP_PCT = 12.0                  # 스퀴즈 폭발 익절선. 트레일링 없음
SQZ_TIME_DAYS = 3                  # 스퀴즈 보유 영업일 한도
EXIT_SL_PCT = 4.0
EXIT_D3_DAYS = 3
FRIDAY_FLAT_ET_HOUR = 15
FRIDAY_FLAT_ET_MINUTE = 30
# TRAIL 과 SQZ_TP 의 rank 3 동점은 의도다. 한 포지션에서 동시에 안 뜬다.
EXIT_PRIORITY = ["FRIDAY", "SL", "D3", "TRAIL", "SQZ_TP", "SQZ_TIME"]
# 구이름. 테스트·표시 코드가 아직 이 심볼을 읽는다.
RUNUP_TRAILING_TRIGGER_PCT = EARN_TP_ARM_PCT
RUNUP_TRAILING_DROP_PCT = EARN_TRAIL_DROP_PCT
SQUEEZE_EXIT_TP_PCT = SQZ_TP_PCT
SQUEEZE_MAX_HOLD_BDAYS = SQZ_TIME_DAYS

# 고점 디스크 쓰기. 20초 바닥을 두어 1초 루프가 파일을 두드리지 못하게 한다.
PEAK_SAVE_MIN_INTERVAL_SEC = 20
PEAK_SAVE_MIN_DELTA_PCT = 0.3

CLOSED_TRADES_LOG_PATH = "closed_trades.jsonl"

# --- LEGACY (LDPB, unused — kept only so nothing else silently breaks) ---
LEADER_LOOKBACK_BARS = 126
LEADER_RALLY_MIN = 0.80
PULLBACK_MIN = -0.40
PULLBACK_MAX = -0.22
PULLBACK_DEAD = -0.50
BASE_BARS_MIN = 10
BASE_BARS_MAX = 20
BASE_BARS = 15
VOLUME_DRYUP_MAX = 0.65
BB_LOOKBACK = 20
BB_WIDTH_PCTILE_MAX = 0.25
ATR_PERIOD = 14
ATR_CONTRACT_MAX = 0.75
PIVOT_VOLUME_MULT = 1.5
STOP_ATR_MULT = 0.5
NEAR_PIVOT_PCT = 0.015
TARGET_RR = 2.0
SMA_FAST = 20
SMA_SLOW = 200
TOP_N = 10
