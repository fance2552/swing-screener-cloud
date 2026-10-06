# LDPB 스윙 스크리너 v1.0 — 사냥 / 수확 콕핏

미국 NYSE/NASDAQ 우량 주도주 **스윙 종목 발굴 콕핏**.  
AI는 스크리너와 실시간 랭킹만 한다. 주문은 토스 HTS에서 사람이 **예약 지정가**로 넣는다.

경로: `~/Desktop/swing-screener`  
포트: `http://localhost:8502`

시드/리스크 계산기 없음. 수량 추천 없음. 차트 없음.  
1위는 피봇에 가장 가까운 종목이 아니다. **사격 신호 우선**, 그다음 거리다.

---

## 실행

1. 바탕화면 **`LDPB 스윙 스크리너.command`** 더블클릭
2. 터미널이 뜨고 Chrome이 `http://localhost:8502` 를 연다
3. 상단 **🚀 스캔 시작** (하단 중복 버튼 없음)
4. 터미널에 `유니버스 2XXX개 확보` 가 찍힌 뒤 일봉을 받는다. **첫 확장 스캔은 수 분** 걸린다. 2~3초에 끝나면 옛 프로세스다. 터미널을 끄고 `.command`를 다시 연다
5. 배너 `🟢 레이더 가동` → Top 10이 뜨고 **Alpaca 1초 체결가**가 계속 덮인다
6. 터미널에 `[ALPACA] 티커 가격 …` 이 1초마다 찍혀야 정상이다. Alpaca가 비면 FMP quote로 폴백한다
7. 좌측 행 클릭 → 그 종목으로 전술 카드 고정. **🎯 1위 자동추적**으로 복귀

**⏹️ 스캔 중지** = 레이더 끔. Top 10 시세 고정. 종목 목록은 남는다.  
보유 종목이 있으면 우측 시세 폴링은 중지 후에도 계속된다. 레이더 스캔 상태는 켜지지 않는다.  
종료: 터미널 닫기 또는 `Ctrl+C`. 주문은 나가지 않는다.

코드 고친 뒤에는 반드시 `.command`를 다시 실행한다. Streamlit 파일 워처는 꺼져 있다.

좌·우 본문은 `@st.fragment(run_every="1s")` 다. 1초 시세가 전체 페이지를 다시 그리지 않는다.

---

## API 키

사이드바 **FMP / Alpaca** 또는 `~/Desktop/swing-screener/.env`

```
FMP_API_KEY=
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
```

기존 초단타 봇 키가 있으면 사이드바 **기존 봇 .env에서 가져오기**. FMP/Alpaca만 복사한다. 토스 키는 이 프로젝트에 넣지 않는다.

---

## 화면 (1920×1080, 50/50, 스크롤 없음)

| 위치 | 동작 |
|---|---|
| 헤더 | KST 세션 뱃지(프리/정규/애프터/휴장), FMP/Alpaca 신호등, SPY 국면(50/200), 배너, 스캔 시작/중지 |
| 좌측 | **사냥 데스크**. 2줄 전술 카드 + Top 10 (`height=420`). LIVE 1초 |
| 우측 | **수확 & 수호 데스크**. `portfolio.json` 보유 카드, 사이렌, 개별 청산, 3필드 등록 |

### 3단계 사격 신호

P = 현재가, Pivot = 피봇돌파가 (`Base_High × 1.002`).

| 조건 | 신호 | 스윗스팟 안내 |
|---|---|---|
| P > Pivot × 1.025 | 🚫 추격 금지 | 과열 (+X.X%) |
| Pivot × 1.002 ≤ P ≤ Pivot × 1.020 | 🎯 지금 사격! | `$Pivot ~ $(Pivot×1.015)` |
| P < Pivot × 1.002 | ⏳ 눌림 관망 | 반등 대기 |

+2.0% 초과 ~ +2.5%도 🚫 추격 금지.

### 랭킹 (과열 강등)

거리(%) = `((피봇돌파가 − 현재가) / 피봇돌파가) × 100`  
이미 피봇 위면 음수다. 거리만으로 정렬하면 COHR처럼 −2.6% 과열이 1위를 먹는다. 그래서 금지.

1. `🎯 지금 사격!` → 우선순위 1 (무조건 상위)
2. `⏳ 눌림 관망` → 우선순위 2
3. `🚫 추격 금지` → 우선순위 3 (최하위)

같은 우선순위 안에서만 거리(%) 오름차순. 정렬 후 순위를 1부터 다시 매긴다.  
전술 카드는 그 1위에 자동 락온한다. 다른 행을 누르면 그 종목으로 고정된다.

테이블 컬럼: `순위, 티커, 실시간 사격 신호, 현재가, 피봇돌파가, 스윗스팟 진입가, 목표가, 거리(%)`

엄격 LDPB 0종이면, 대장(+80%)+조정(−22~−40%)은 통과하고 거래량/변동성 마름만 부족한 **근접 Top 10**을 띄운다.

### 우측 보유 등록

입력은 3개만: **티커, 평단가, 수량**.

| 항목 | 자동 |
|---|---|
| 진입일 | 오늘 (`YYYY-MM-DD`). 진입 당일 = 1일 차 |
| 손절가 | 좌측 레이더에 있으면 LDPB 권장손절. 없으면 평단 × 0.92 (−8%) |
| 월가 | 등록 순간 FMP 1회. `portfolio.json`에 영구 저장. 1초 루프에서 재호출 없음 |

월가: 목표가 컨센서스(`targetConsensus`) + 매수 비율(Buy+StrongBuy).  
레거시 `/api/v3/analyst-stock-recommendations` 가 403이면 `/stable/grades-consensus` 로 매수 비율을 받는다.

파일: `~/Desktop/swing-screener/portfolio.json` (git 제외)

카드: 티커, 수량, 진입가, 현재가, 수익률, 평가손익, R배수, 손절선, 보유 일수,  
`🏛️ 월가 컨센서스: 목표가 $… (상승 여력 …%) | 투자의견: 매수 …%`  
하단 `🗑️ {티커}만 청산` — 그 티커만 삭제. 나머지 보유는 유지.

현재가는 Alpaca 1초 체결가. Top 10 밖 보유 티커도 같은 배치에 넣는다.

1R = `max(평단 − 손절, 0.01)`  
R배수 = `(현재가 − 평단) / 1R`

사이렌은 보유 중 가장 급한 것 하나:

| 순위 | 조건 | 표시 |
|---|---|---|
| 1 | 현재가 ≤ 손절 | 빨강 손절 경보. 토스 조건주문으로 기계적 탈출 |
| 2 | R ≥ +2.0 또는 수익률 ≥ +10% | 초록 50% 분할 익절. 잔여 손절을 본전으로 |
| 3 | +1.0R ~ +2.0R | 파랑 본전 방어. 손절을 진입가로 |
| 4 | 보유 ≥ 3일 이고 R < +1.0 | 주황 시간 손절 |
| 기본 | 그 외 | 정상 순항 |

---

## 파이프라인

1. FMP `company-screener` 1회  
   `priceMoreThan=10`, `marketCapMoreThan=1_000_000_000`, `volumeMoreThan=1_000_000`, `exchange=NASDAQ,NYSE`, `limit=3000`  
   거래량 필터로 2000 미만이면 거래량 조건 없이 보충. 실측 약 **2400**종. SPY 500 캐시가 아니다
2. 일봉: 캐시는 씨앗. 마지막 미국 정규장 세션보다 오래된 봉은 FMP EOD 병렬 백필. 없는 종목은 신규 다운로드
3. LDPB 게이트 → Top 10을 `SharedState`에 저장
4. `start_heartbeat()` 데몬, 1.0초  
   Alpaca `trades/latest` — IEX + delayed_sip + overnight 중 가장 최근 체결  
   대상: 레이더가 켜져 있으면 Top 10, 그리고 등록된 보유 티커. FMP quote는 Alpaca가 빌 때만
5. UI: 좌측 사냥 / 우측 수확 각각 fragment 1초. 전체 `st.rerun` 루프 없음

캐시 파일: `~/Library/Caches/ldpb-screener/eod_YYYY-MM-DD_1y.pkl`  
터미널 로그: `[LDPB HH:MM:SS] …` / `[ALPACA] …`

---

## LDPB 규칙 (완화 금지)

- 유니버스: 주가 > $10, 시총 > $1B, 20일 거래대금 > $25M
- 대장: 6개월 고점 **이전** 저점 대비 +80% 이상
- 조정: 그 고점 대비 −22% ~ −40%. −50% 이하는 영구 제외
- 마름: 10일 거래량 ≤ 50일 평균의 65%, ATR·볼린저 수축
- 진입: 피봇(`Base_High × 1.002`) 예약 지정가, 거래량 1.5배
- 손절: `Base_Low − 0.5 × ATR` (보통 −4% ~ −7%)

---

## 수동 실행 (바로가기 없이)

```bash
cd ~/Desktop/swing-screener
ulimit -n 10240
./.venv/bin/streamlit run app.py --server.port 8502 --server.fileWatcherType none
```

---

## 안 하는 것

초단타 자동매매, 토스 주문 전송, Alpaca 웹소켓 테이프, 시드/리스크 수량 계산기, Plotly 캔들, 마이크로캡, 개장 1분 ORB, 0.01초 자동진입, 1초 루프의 FMP 애널리스트 재호출.

---

## 수정 이력

### 2026-10-03 — Claude 코드 감사 반영 (6건)
- **data_feed.py**: `_write_live_price()`에 1초 이내 IEX_TRADE 보호 가드 추가.
  기존에는 Yahoo/Nasdaq 폴링(1초 주기)이 Alpaca 실거래가를 조건 없이
  덮어쓸 수 있었음.
- **data_feed.py**: 모듈 docstring을 실제 동작(Yahoo+Nasdaq+Alpaca)과
  일치시키고, 미사용 `fmp_realtime_polling_loop()`에 경고 주석 추가.
- **screener/portfolio.py**: `guardian()`의 rank를 `config.EXIT_PRIORITY`
  인덱스로 계산하도록 변경. 기존에는 FRIDAY/SL/D3가 전부 rank=0으로
  동률이라, 서로 다른 종목에서 동시에 발생하면 `lead_guardian()`이
  임의 순서로 하나만 상단에 노출했음.
- **app.py**: `_live_hunt_payload()`가 `portfolio.guardian()` 판정
  (exit_badge/exit_order/exit_code)을 포함해 AI 프롬프트에 넘기도록 수정.
  기존에는 원시 포지션 데이터만 전달되어, Gemini가 이미 확정된 청산
  판정을 독자적으로 재평가할 여지가 있었음.
- **app.py**: 포지션 카드의 손절선 표시를 진입 시점 고정값 대신
  `config.EXIT_SL_PCT` 기준 실시간 계산으로 변경(목표가 표시 방식과 통일).
- **env_settings.py**: `get_api_keys()`가 `os.environ`을 `.env` 파일보다
  우선하도록 수정(`fmp_key()`/`alpaca_keys()`와 동일한 우선순위로 통일).

tests/selftest_price_freshness.py, tests/selftest_guardian_rank.py,
tests/selftest_env_keys.py 로 회귀 테스트 가능.

#### 적용 메모
- **ai_advisor.py** (지시서 외 1줄): 보유 종목 필드 허용 목록 `_PF_KEYS`에
  `exit_code`, `exit_badge`, `exit_order`, `pct`, `r_mult`를 추가. 이 목록에
  없는 필드는 프롬프트 직렬화 단계에서 버려지므로, 추가하지 않으면 위
  app.py 보강이 Gemini에 도달하지 않는다. 실제 `_live_hunt_payload()` →
  `build_prompt_text()` 경로로 `"exit_code": "SL"`이 프롬프트에 들어가는 것을 확인.
- **selftest 3종**: `python tests/selftest_*.py`로 실행하면 파이썬이 `tests/`만
  모듈 경로에 넣어 루트 모듈을 못 찾는다. `tests/test_event_driven.py`와 같은
  루트 경로 추가 2줄을 각 파일 상단에 넣었다. 검증 로직은 그대로.
- **rank 순서 (해결)**: rank를 `EXIT_PRIORITY` 위치로 매기면서, 기존 순서
  `["FRIDAY", "TP", "SL", "D3"]`에서는 한 종목이 손절이고 다른 종목이 익절이면
  상단 배너에 익절이 먼저 떴다. **config.py**의 순서를
  `["FRIDAY", "SL", "D3", "TP"]`로 바꿔 손실 방어를 익절보다 앞에 두었다.
  같은 종목에서 D-3와 +4%가 겹치면 이제 D-3 강제 청산이 뜬다.
  `tests/test_event_driven.py`의 해당 기대값 1줄을 `"TP"` → `"D3"`로 맞췄다.
