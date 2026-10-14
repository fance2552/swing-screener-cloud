# swing-screener-cloud 시스템 설명

이 문서만 읽고 앱의 목적, 화면, 전략 규칙, 데이터 흐름, 중요 함수, 알려진 한계를 파악할 수 있게 썼다.
기준 코드는 GitHub `fance2552/swing-screener-cloud` 브랜치 `main`, 커밋 `9c00f2e` 이후의 동작이다.
비밀값, PIN, API 키는 적지 않는다.

평가자에게: 이 앱은 주문을 보내지 않는다. 사람이 토스 HTS에 예약 지정가를 넣는다. 숏스퀴즈, 초단타 자동진입, Gemini는 현재 제품이 아니다.

---

## 1. 제품이 하는 일

미국 주식 스윙 스크리너다. AI와 규칙 엔진이 이름을 고르고, 사람은 토스에서 산다.

- 목표 기대값: 하루 복리로 약 +1~2%를 노리는 다중일~수주 보유. 마이크로캡 초단타가 아니다.
- 유니버스 뼈대: 가격 > $3, 시총 $50M~$50B, NYSE/NASDAQ. 전략마다 더 좁은 필터가 있다.
- 예산 가이드(화면 문구, 주문 수량은 계산하지 않음): PEAD 120만 원, 런업 100만 원, RSI2 80만 원.
- 보유 파일: `portfolio.json`. 필드 최소 단위는 티커, 평단, 수량, 전략. 앱이 보유를 자동 삭제하지 않는다.
- 배포: `main`에 푸시하면 Streamlit Cloud가 재배포한다. 앱 URL은 `https://swing-screener-cloud-aj9rzaec4p6d2emixhjpg8.streamlit.app`.
- 화면: Streamlit 1.64, 1920×1080 전제. 위젯 너비는 `width="stretch"`. `use_container_width`는 쓰지 않는다.

하지 않는 일:

- 브로커 주문, 청산 전송, 토스 API.
- 0.01초 자동 진입, ORB, HFT.
- 숏스퀴즈 스캔과 숏스퀴즈 청산. 관련 함수는 클라우드 코드에서 제거됐다.
- Gemini. 재도입하지 않는다.

죽은 이름: LDPB, PMH, ORB. `config.py` 하단 `LEADER_*` 상수는 옛 코드가 import하다 죽지 않게 남은 값이다. 스캔은 읽지 않는다.

---

## 2. 화면 3개

헤더(`app.py`의 `_header_live`)는 `@st.fragment(run_every="1s")`다. 시계, 세션, FMP/IEX 수신 나이, 국면, 사격 잠금, 사냥터 구축, 순위 새로고침, 레이더 토글, 데스크 전환, AI 버튼이 여기 있다.

클릭을 받아야 하는 버튼은 이 1초 헤더 안에 둔다. 헤더 밖에 두면 1초 갱신이 클릭을 삼킨다. 헤더 안에서 상태를 바꾼 뒤 `st.rerun(scope="app")`를 호출해야 아래 본문이 다시 그려진다. 프래그먼트 안의 맨 `st.rerun()`은 본문을 갱신하지 않는다.

| 데스크 | 하는 일 |
| --- | --- |
| 사냥 | PEAD / 런업 압축 / RSI2 3열 카드. 풀은 `SHARED.hunting_pool`. |
| 수호 | `portfolio.json` 보유 카드, 손절 사이렌, 등록 폼. |
| AI | 보유 감리 카드 + 티커 5인 결재판. 버튼은 헤더, 긴 결과는 본문. |

빈 사냥 열 문구:

- 한 번도 스캔하지 않음 (`hunting_pool_updated_ts`가 없음): `스캔 전 IDLE. 상단 [🚀 사냥터 구축] 실행 필요`
- 스캔은 끝났는데 0건: `💤 조건 충족 우량주 없음 (스캔 완료 · 주가 $10+ & 시총 우량주 필터 통과 0건)`

스캔 중이나 AI 중에는 사냥 데스크와 수호 데스크의 1초 본문이 `⏸️ 스캔/AI 분석 진행 중`만 남기고 멈춘다. 헤더의 시계와 진행 바는 계속 돈다. 네 버튼(구축, 새로고침, 감리, 발굴/심의)은 그 동안 비활성이다.

사격 표시는 09:45 ET 이후 정규장에서만 초록이다. 그 전은 `radar.fire_unlock_time_label()`이 서머타임에 따라 `밤 10:45` 또는 `밤 11:45`를 보여 준다. 판정 함수는 `radar.session_allows_fire_alert()`.

---

## 3. 세 전략

### PEAD — 실적 발표 이후 드리프트

- 조건: EPS 서프라이즈 ≥ +5%. 매출이 둘 다 있으면 매출은 추정치 이상. 반응일 갭(반응일 시가 / 직전 종가 − 1) ≥ +2%. 발표 후 달력일 D+1~D+2.
- 가격: 스캔 시점 FMP 시세 `fmp.quotes()`. 시세가 없거나 $10 미만이면 후보에서 삭제.
- 점수: `min(40 + min(EPS비트, 35) + min(갭 * 2.5, 25), 100)`. 윈도우 통과 기본 40점 + EPS/갭 가산, 상한 100점. 상위 7개.
- 카드 목표가: 컨센서스가 있으면 그 값, 없으면 현재가 +10%. 카드 손절 표시는 현재가 −4%.
- 보유 청산(`guardian`): 손익 ≤ −4.5%면 `SL`. 영업일 보유 ≥ 15일이면 `TIME_EXIT`. 시세 없으면 `NO_QUOTE`. 아니면 `HOLD`. 하드 익절은 없다.
- 등록: 실적일이 없어도 통과한다.

`scan_pead`는 일봉으로 갭을 계산한 뒤 시세를 붙인다. `_reaction_gap_pct(df, earnings_date)`의 첫 인자는 티커 문자열이 아니라 일봉 DataFrame이다.

### 런업 — 실적 직전 D-7~D-3

- 후보 창: 실적까지 달력일 3일에서 7일. 그 밖은 `scan_earnings`에서 `continue`.
- 점수 100점: D-day 40 (창 안이면 만점, 아니면 0) + 애널리스트 매수비율 30 + 50일선 위 15 + 목표가 상승여력 15.
- 사격: D-7~D-3이고 점수 ≥ 75면 `🎯 런업 사격`. 점수 미달이면 관망. D-2 이하는 `🚫 진입 금지`.
- 카드 목표가: 컨센서스, 없으면 +8%. 카드 손절 표시는 −4%.
- 보유 청산: 손익 ≤ −4%면 `SL`. `d_day <= 2`면 `D2_EXIT` (수익률 무관). 시세 없으면 `NO_QUOTE`.
- 등록: 실적일이 필수. 폼이 비어 있으면 FMP에서 한 번 더 찾고, 그래도 없으면 등록을 거절한다. 이미 지난 날짜면 `add_position`이 거절한다.
- 필터: 가격 > $10, 시총 $1.5B~$50B, 20일 달러 거래대금 하한 `EARN_ADV_MIN`.

`earn_fire_signal`은 D-2 이하만 금지하고 D-7~D-3은 사격 가능하다. 보유 청산 D-2는 `RUNUP_FORCE_EXIT_DDAY`만 본다.

### RSI2 — 대형주 2일 RSI 과매도 반등

- 체제: SPY 종가가 200일 이동평균 위일 때만 후보를 낸다. 아래면 빈 리스트. 이것은 버그가 아니다.
- 조건: 시총 ≥ $5B. RSI(2) ≤ 10. 실적 ±5 영업일 종목 제외.
- RSI는 Wilder가 아니다. 최근 2개 상승/하락의 단순 평균이다.
- 점수: `(10 - RSI2) * 10`, 상한 100. RSI가 0이면 점수가 100으로 몰린다. 동점이면 3일 낙폭 절대값이 큰 종목이 앞선다.
- 부가 필드: `market_cap_b`, `drop_3d_pct`, `dist_sma200_pct`. 시총 키는 `marketCap`, `market_cap`, `mcap` 순이다. 200일선은 종가 200개의 평균이다. 계산이 안 되면 0.0.
- 카드 목표가: +3%. 카드 손절 표시는 −4%.
- 보유 청산: 손익 ≤ −3%면 `SL`. 종가가 5일선 위로 복귀하면 `SMA5_EXIT`. 영업일 ≥ 4일이면 `TIME_EXIT`.

카드에 찍는 −4% 손절과 가디언의 실제 손절(PEAD −4.5%, 런업 −4%, RSI2 −3%)은 숫자가 다르다. 화면 뱃지는 표시용이고, 보유 사이렌은 `config`의 전략 손절을 본다.

---

## 4. 가격 뱃지

`screener/pricing.py`

```python
def compute_price_badges(row: dict, strategy: str) -> dict:
    # current_price, price, close 순으로 읽고 0 이하면 전부 0.0
    # PEAD 목표 = 컨센서스 또는 현재가*1.10
    # RUNUP 목표 = 컨센서스 또는 현재가*1.08
    # RSI2 목표 = 컨센서스 또는 현재가*1.03
    # 손절 = 현재가 * 0.96
    # upside_pct = max(0, (target-price)/price*100)
```

컨센서스 후보는 `target_consensus`, `consensus_target`, `target_price` 순이다. 이미 계산된 `target_price`를 다시 넣으면 그 값이 컨센서스처럼 쓰여 같은 숫자가 유지된다.

`enrich_candidates`는 스캔 끝과 AI에 넘기기 직전에 호출된다.

---

## 5. 스캔

`engine.build_hunting_pool`은 백그라운드 스레드다. UI를 막지 않는다. PEAD, 런업, RSI2는 각각 `try/except`다. 하나가 죽어도 나머지는 `SHARED.hunting_pool`에 남고, 실패 문장은 `SHARED.scan_errors`에 쌓여 사냥 데스크 익스팬더에 보인다.

순서:

1. PEAD: `pead.scan_pead(limit=7)` 후 뱃지.
2. 런업: `event_driven.fetch_universe()` → `download_daily` → SPY로 `regime.get_market_regime` → `scan_earnings(rows, hist)` 결과 `[:7]`. `scan_earnings`에는 `limit=` 인자가 없다.
3. RSI2: `rsi2.scan_rsi2(limit=7)` 후 뱃지.
4. `finally`에서 큰 일봉 참조를 끊고 `gc.collect()`. 풀 저장은 `safe_set`으로 락을 0.5초만 기다린다. 실패하면 결과 유실을 막기 위해 공개 속성에 직접 쓴다.

`refresh_pool_snapshot`(⚡ 순위 새로고침)은 PEAD, 런업, RSI2 후보 전부를 `fmp.quotes`로 다시 받아 `SHARED.note_quote`에 넣고, `compute_price_badges`로 현재가·목표가·손절·업사이드를 다시 쓴다. 그 다음 런업만 `earn_rank_live(rows, SHARED.quote)`로 다시 줄 세우고 D-7~D-3 밖은 탈락시킨다. PEAD와 RSI2 점수 자체는 일봉 기준이라 시세로 다시 매기지 않는다. 사냥 카드는 1초마다 `SHARED.quote`가 있으면 그 가격으로 뱃지를 다시 그린다.

감시 목록은 PEAD ∪ 런업 ∪ RSI2 ∪ 보유 ∪ `KRW=X`다.

---

## 6. 시세

- 일봉: FMP EOD. `data/yf_download.download_daily`. 시드 `data/seed/eod_seed.pkl.gz`. 분봉 `download_intraday`는 빈 DataFrame만 반환한다. `yfinance` 패키지는 requirements에서 빠졌다.
- 실시간: Yahoo v7이 429면 Nasdaq `primaryData`. Alpaca IEX 웹소켓은 보조. `delayed_sip` REST는 없다.
- 단발 시세: `data/fmp.quotes(["AAPL"]) -> {"AAPL": {"price": ...}}`. `get_quote`라는 함수는 없다.
- 컨센서스: `data/fmp.fetch_fmp_analyst_data(ticker)` → `target_consensus`, `buy_ratio`.
- 프로필: `event_driven._fmp_get("/stable/profile", {"symbol": ...})`.
- 다음 실적일: `event_driven.next_earnings_date(symbol) -> "YYYY-MM-DD"`. 미래만.
- 캘린더: `fetch_calendar(today, horizon_days) -> dict[ticker, iso_date]`. 리스트가 아니다. horizon은 일수다.
- `_hist_stats(df)`는 DataFrame을 받는다. 키는 `price, sma10, sma50, adv20, today_vol, ret_5d`. `sma200`은 없다. 종가가 50개 미만이면 `None`.

---

## 7. 공유 상태

`state.SHARED`는 프로세스 전역 `SharedState`다. 스캔 스레드가 쓰고 UI가 읽는다.

중요 필드:

- `hunting_pool`: `{"pead": [dict], "runup": [dict], "rsi2": [dict]}`. 세션 스테이트가 아니다.
- `hunting_pool_updated_ts`: 마지막 구축/새로고침 시각. 없으면 스캔 전.
- `scan_errors`: `list[str]`.
- `earnings_targets`: 런업 행 사본. 퀵 스캔과 등록 시 실적일 조회가 읽는다.
- `live_quotes[symbol]["price"]`.
- `regime`: SPY 200일선과 25일 분산일. `get_market_regime`.
- `heavy_scan_running`, `scan_running`.

`safe_get` / `safe_set`은 공개 속성 이름(`hunting_pool`)에 쓴다. `_hunting_pool` 같은 숨은 이름에 쓰면 화면이 빈 풀을 본다. 락을 0.5초 안에 못 잡으면 마지막 스냅샷을 돌려주거나 쓰기를 포기한다.

국면(`screener/regime.py`): SPY가 200일선 아래이거나 분산일이 8 이상이면 CORRECTION. 5~7이면 PRESSURE. 그 미만이고 선 위면 NORMAL. 이 값은 헤더와 AI 입력에 쓰인다. RSI2 스캐너가 SPY 200일선을 따로 한 번 더 본다.

---

## 8. 보유와 등록

`portfolio.json` 한 행의 핵심:

```text
ticker, strategy(PEAD|RUNUP|RSI2), entry_price, shares,
stop_price, entry_date, entry_fx, earnings_date,
target_consensus, buy_ratio, peak_price
```

라디오 매핑은 `STRATEGY_LABEL_TO_KEY` 하나다.

```python
{"📈 PEAD": "PEAD", "📅 런업 D-7~D-3": "RUNUP", "📉 RSI2": "RSI2"}
```

등록 폼의 티커 칸은 `st.form` 밖에 있다. 폼 안 위젯은 `on_change`가 안 돈다. 티커가 바뀌면 `fetch_auto_fill_for_registration`이 현재가와 가장 가까운 실적일(과거·미래)을 채운다. 같은 티커면 재조회하지 않는다.

`validate_position_registration`:

- PEAD: 실적일 없어도 `ok=True`.
- RSI2: 실적일을 `None`으로 두고 통과.
- RUNUP: 입력이 있으면 통과. 없으면 자동 조회. 그래도 없으면 `ok=False`.

그 다음 `add_position(ticker, entry_price, shares, strategy=, earnings_date=)`. 전략이 세 개 외면 예외. 런업인데 날짜가 없고 스캔 풀에도 없으면 `런업 종목은 실적 발표일이 필수입니다`. PEAD는 이 문장에 들어가지 않는다. 런업 날짜가 과거면 `이미 지난 실적 발표일입니다`.

손절 저장은 전략 퍼센트다. PEAD 4.5, 런업 4, RSI2 3. 카드 뱃지의 −4%와 다르다.

고점 디스크 쓰기는 20초와 0.3%를 둘 다 넘길 때만이다. 1초마다 파일을 쓰지 않는다. 클라우드 동기화는 기존 `cloud_sync`와 Gist다.

가디언 순위: SL 0, D2_EXIT와 SMA5_EXIT 1, TIME_EXIT 2, NO_QUOTE 3, HOLD와 LEGACY 9. 반환은 문자열이 아니라 dict다. `code`, `badge`, `order`, `rank`, `alert`.

PEAD, RUNUP, RSI2가 아닌 보유는 `LEGACY`다. 자동 매도 문장을 만들지 않는다. 옛 포지션은 월요일에 사람이 지운다.

---

## 9. AI

모델:

- 실무: Groq `openai/gpt-oss-120b`. 타임아웃 8초. `reasoning_effort="low"`. `$`는 `USD`로 바꾼 뒤 마크다운한다. Streamlit이 `$`를 수식으로 먹어 문장이 사라진다.
- 총괄 CIO: DeepSeek `deepseek-chat`, `https://api.deepseek.com/chat/completions`, 타임아웃 15초.
- `_cio_chat`은 DeepSeek을 먼저 부르고, 실패하면 Groq가 같은 프롬프트를 쓴 뒤 맨 위에 대타 사유를 붙인다.
- 키 우선순위: Streamlit secrets → 환경변수 → 앱 `.env`. 필수 키는 FMP와 Alpaca 둘뿐이다. Groq와 DeepSeek은 선택.
- 사이드바에 DeepSeek를 치면 그 세션 `os.environ`에 바로 넣는다. 저장 버튼을 눌러야 디스크에 남는다.
- Groq 429면 한국어로 "일일 토큰 20만을 썼다"고 하고, 같은 파이프라인의 다음 Groq 호출은 멈춘다. 원문 JSON을 화면에 뿌리지 않는다.
- 프롬프트에 넣는 JSON은 `_j`로 약 1500자에서 자른다. 실무 보고서는 `_trim`으로 약 700자다.

### 보유 감리 `run_portfolio_audit`

Groq 3명(펀더멘털, 수급, 리스크) 다음 CIO. 반환:

```python
{
  "full_report": str,
  "position_cards": [
    {"ticker", "verdict_code", "verdict_label", "avg_price",
     "target_price", "stop_price", "deadline", "action_line"}
  ],
  "staff_reports": {"fundamental", "flow", "risk"},
  "errors": [str],
}
```

`verdict_code`는 `HOLD`, `TRIM`, `EXIT`만. 화면은 종목마다 색 카드 하나. 원문과 3인 보고서는 접힌 칸. CIO는 `[POSITION] ... [/POSITION]` 마커를 종목 수만큼 내야 한다. 마커가 없는 보유 티커는 HOLD 폴백 카드다. `_ticker_list()`는 `"NET, AMD"` 문자열이라 여기 쓰지 않는다. `_holding_tickers()`가 `list[str]`이다.

### 티커 심의 `run_ticker_review`

Groq 실무 4명(매크로, 펀더멘털, 테크니컬, 리스크)을 스레드 4개로 동시에 부른다. 한 명이 약 16초를 넘기면 그 칸은 `응답 없음`. 그 다음 DeepSeek CIO.

CIO는 이 마커만 인정한다.

```text
[VERDICT] ENTER_NOW | WAIT_CONDITIONAL | NO_ENTRY
[REASON] 한 줄
[ENTRY] USD a ~ USD b
[TARGET] ...
[STOP] ...
[SIZE] ...
[PNL] 성공 시 ... / 실패 시 ...
[상세]
```

파서 `_parse_cio_sections`가 dict로 확정한다. 마커가 없으면 문장의 🟢/🟡/🔴로 추정하고, 그것도 없으면 `WAIT_CONDITIONAL`이다. 앱은 파싱 실패로 죽지 않는다.

화면 라벨:

- `ENTER_NOW`: 🟢 [당장 진입 추천]
- `WAIT_CONDITIONAL`: 🟡 [조건부 대기 진입]
- `NO_ENTRY`: 🔴 [진입 금지 (패스)]

"승인 권고", "반려", "합의도 %"는 티커 심의 출력 규칙에서 빼 있다. 카드는 HTML이다. AI 문자는 `html.escape` 뒤에 넣는다. `$`는 `USD`.

반환 키: `ticker`, `verdict_code`, `verdict_label`, `reason_line`, `entry_range`, `target_price`, `stop_price`, `position_size`, `expected_pnl`, `staff_4lines`, `order_copy_text`, `cio_report_full`, `staff_reports`, `errors`.

세션:

- `portfolio_audit_result`: 감리 dict. 예외 시에는 아직 문자열일 수 있어 렌더러가 문자열도 받아 적는다.
- `ticker_reviews`: `{티커: review_dict}`. 종목마다 남긴다. 감리 결과와 서로 지우지 않는다.
- `ticker_review_result`: 가장 최근 심의.
- `ai_busy`: 참인 동안 버튼 잠금, 1초 데스크 정지.

수동 티커는 `manual_lookup.fetch_manual_ticker_data`가 시세, 프로필, 50일선, 200일선, 다음 실적일, 컨센서스를 모은 뒤 같은 `run_ticker_review`에 넘긴다. 시세가 없으면 마지막 일봉 종가로 대체한다.

`run_top3_summary_briefing`은 Tab 1의 PEAD·런업·RSI2 후보 전체를 실무 4인(매크로, 펀더멘털, 테크니컬, 리스크)이 병렬로 읽고, DeepSeek CIO(없으면 Groq)가 그 메모로 Top 3를 고른다. 열의 `scanner_rank` 1등을 그대로 1·2·3위로 붙이지 않는다. 한 전략에서 둘을 골라도 된다. 가격은 사냥터 행에서만 가져온다. 실무진이 실패하거나 CIO가 목록 밖 티커만 내면 카드는 비우고 에러만 남긴다. 버튼은 헤더의 AI 통제 안에 있고, 메달 카드와 실무 메모는 Tab 3 본문 맨 위에 `top3_summary_result`로 그린다.

`run_new_hunt`은 파일에 남아 있다. 지금 Tab 3 버튼은 이 함수를 호출하지 않는다. 전략별 1위 카드의 "집중 심의"와 수동 티커가 그 자리를 대신한다.

---

## 10. 런업 점수 식

```python
def _earn_dday_points(d: int) -> float:
    if 3 <= d <= 7:
        return 40.0
    return 0.0

def earn_score_row(...):
    score = dday(최대 40) + rating(최대 30) + sma50(0 또는 15) + upside(최대 15)
```

`scan_earnings(universe, hist, today=None) -> (top, note, calendar)`.
`earn_rank_live(rows, quote_fn, today=None)`는 기존 행에 라이브 가격을 덮고 다시 점수 낸다. 티커 문자열 리스트를 받지 않는다.

---

## 11. 파일 지도

| 파일 | 역할 |
| --- | --- |
| `app.py` | Streamlit. 헤더 프래그먼트, 3 데스크, 등록 폼, 카드 HTML. |
| `engine.py` | 헤비/퀵 스캔, 감시 목록, 실시간 기동. |
| `state.py` | `SHARED`, `safe_get`/`safe_set`. |
| `config.py` | 전략 상수. |
| `env_settings.py` | 키. 필수 FMP+Alpaca. Groq, DeepSeek 선택. |
| `screener/event_driven.py` | 달력, 유니버스, 일봉 통계, 런업 점수와 `scan_earnings`. |
| `screener/pead.py` | PEAD 스캔. |
| `screener/rsi2.py` | RSI2 스캔. |
| `screener/pricing.py` | 카드 가격 뱃지. |
| `screener/manual_lookup.py` | 수동 티커 데이터. |
| `screener/portfolio.py` | 보유, 가디언, 등록 검증. |
| `screener/regime.py` | SPY 국면. |
| `screener/radar.py` | 세션 시계, 09:45 ET 사격 잠금. |
| `ai_advisor.py` | Groq, DeepSeek, 감리, 티커 심의. |
| `data/fmp.py` | 스크리너, `quotes`, EOD, 애널리스트. |
| `data/yf_download.py` | 일봉 캐시와 시드. |
| `data_feed.py` | 1초 시세 데몬. |
| `cloud_sync.py` | Gist 동기화. |

---

## 12. 평가자가 볼 긴장

1. 카드 손절은 전부 −4%로 그리고, 보유 사이렌은 −4.5 / −4 / −3이다. 사람이 어느 숫자를 믿어야 하는지가 갈린다.
2. AI 호출은 아직 Streamlit 스크립트 스레드에서 돈다. 실무 4명이 병렬이어도 CIO까지 붙으면 클릭 한 번이 수십 초 웹소켓을 붙잡을 수 있다. 8초/15초 타임아웃과 데스크 정지는 완화이지 제거가 아니다.
3. 1초 헤더 밖의 버튼은 클릭이 죽는 전례가 있다. 등록 폼은 본문에 있다. 티커 `on_change`가 1초 헤더와 겹치면 조회가 한 박자 늦거나 무시될 수 있다.
4. PEAD가 0건인 날은 서프라이즈+갭+D+1~D+2+$10이 동시에 없는 것이다. 2026-10-10 실측에서 갭 통과 종목 HOVR의 시세와 종가가 둘 다 $1.55라 필터가 빈 리스트를 냈다.
5. RSI2가 0건인 날은 SPY가 200일선 아래이거나, RSI(2)≤10이면서 실적 가드를 통과한 $5B 종목이 없는 것이다.
6. 런업은 D-7~D-3이다. 그 창이 아니면 열이 빈다. 청산은 여전히 D-2 이하다.
7. `run_new_hunt`와 LDPB 상수는 남아 있다. 화면 경로와 어긋난다.
8. `fileWatcherType = none`이라 클라우드 프로세스가 옛 모듈을 메모리에 붙잡고 있으면 배포 뒤에도 옛 함수가 돈다. 증상이 남으면 Streamlit Manage app에서 Reboot.
9. Groq 무료 한도는 요청당 약 8000토큰, 하루 약 20만 토큰이다. 마스터 프롬프트가 길면 잘리거나 429가 난다.
10. 가디언은 주문을 만들지 않는다. 문구만 만든다. 실제 매도는 사람의 토스 조작이다.
