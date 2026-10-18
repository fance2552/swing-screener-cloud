# swing-screener-cloud — Claude Code 시스템 헌법 & 개발 가이드

이 문서는 Claude Code CLI 및 Claude AI가 이 저장소의 코드를 분석하고 수정할 때 반드시 준수해야 하는 최상위 규칙이다.

## 1. 프로젝트 정체성 및 기본 원칙
- **제품 본질:** 미국 주식(NYSE/NASDAQ) 이벤트 드리븐 단기 스윙 트레이더 콕핏 (Streamlit Community Cloud 배포).
- **자동 주문 금지:** 브로커(토스증권) 주문 API 자동 전송은 일체 하지 않는다. 모든 매수/매도는 사령관(트레이더)이 토스 HTS에서 수동 지정가로 집행한다.
- **배포 환경:** `fance2552/swing-screener-cloud` 저장소의 `main` 브랜치에 푸시하면 Streamlit Community Cloud가 자동 재배포한다.
- **보안/인증:** 진입 시 `app.auth_gate()`를 통해 6자리 PIN(`COCKPIT_PIN`) 통과 전에는 화면 렌더링을 차단한다.

## 2. 화면 구조 (2-Tab 체제)
1. **`🎯 사냥 데스크` (`tab_hunt`)**:
   - 3대 검증 셋업(PEAD / 런업 압축 / RSI2) 기반 발굴.
   - 후보는 당일 A급 셋업 1~3개만 엄선 표출 (기준 미달 시 "오늘은 기준을 통과한 A급 셋업이 없습니다" 휴식 표출).
   - 각 후보별 DeepSeek 1-Shot 사격 주문표 발행 버튼.
   - 22:45 KST는 강제 락이 아닌 `💡 권장 진입: 밤 10:45 이후` 안내 뱃지. 24시간 상시 검색 지원.
2. **`🛡️ 수호 데스크` (`tab_guard`)**:
   - `portfolio.json` 기반 보유 카드, 0.2% 수수료 선반영 실수령 손익, 손절선.
   - DeepSeek 포트폴리오 정밀 감리 (`HOLD` / `TRIM` / `EXIT` 판결).
   - 포지션 등록 폼 (티커 입력 시 FMP 실시간 시세 자동 채움).

## 3. 핵심 전략 3대 기둥
- **전략 1 [🔥 PEAD] (120만 원 슬롯):**
  - 실적 서프라이즈(EPS beat ≥ 5%, 매출 신장) + 당일 갭업(≥ +2%) + 주가 ≥ $10, 시총 ≥ $1B 우량주.
  - D+1~D+2 진입, 목표 +10~15%, 손절 -4.5%, 15영업일 시간 청산.
  - 스코어링: 100점 만점 표준 체계 (기본 자격 40점 + EPS 35점 + 갭 25점).
- **전략 2 [📅 런업 압축형] (100만 원 슬롯):**
  - 실적 직전 D-5~D-4 윈도우만 진입, 실적 D-2 전량 강제 청산 (D-12 데드존 폐기).
  - 손절 -4.0%, 목표 +8~12%.
- **전략 3 [⚡ RSI2 과매도 반등] (80만 원 슬롯):**
  - SPY > 200SMA (상승장 한정), 시총 ≥ $5B 대형주, RSI(2) ≤ 10, 실적 ±5영업일 제외.
  - 손절 -3.0%, 익절 +3~4% (5일선 복귀 시 청산).
- ❌ **영구 퇴출된 전략:** 숏스퀴즈(SQUEEZE) 및 D-12 런업은 코드 및 추천에서 전면 제거됨.

## 4. AI 모델 및 통신 규칙
- **Gemini 영구 배제:** `google-generativeai` 또는 Gemini 관련 코드는 일체 사용 금지.
- **DeepSeek 단독 주력:**
  - `DEEPSEEK_API_KEY`(`deepseek-chat`, Base URL: `https://api.deepseek.com`)를 사격 결재판 및 포트폴리오 감리의 최우선 엔진으로 사용.
  - 키 누락 또는 장애 시 Groq(`llama-3.3-70b-versatile`)로 자동 폴백(Fallback).
- **Streamlit 마크다운 파싱 헌법:**
  - Streamlit이 `$` 기호를 LaTeX 수식으로 인식해 뒷문장을 날려버리는 버그를 방지하기 위해, 모든 렌더링 텍스트에서 `$` 기호는 반드시 `USD`로 치환할 것.

## 5. 클라우드 인프라 및 영속성 규칙
- **Gist 영속성 (`cloud_sync.py`):**
  - Streamlit Cloud의 휘발성 디스크 문제를 방지하기 위해 `portfolio.json`, `api_keys.json`, `closed_trades.jsonl`은 GitHub Gist와 동기화한다.
  - API 키 저장 시 Gist에 영구 백업하여 재배포 시에도 키가 증발하지 않도록 유지한다.
- **1초 라이브 격리 프래그먼트:**
  - 헤더는 `@st.fragment(run_every="1s")`로 격리 실행.
  - 1초 주기 루프 내에서 메인 스크립트의 `st.rerun()` 호출 절대 금지 (전체 화면 깜빡임 누수 방지).
  - 고점(`peak_price`) 갱신은 메모리에서 처리하며, 디스크 저장은 20초 간격 & 0.3% 이상 갱신 시에만 디바운스 수행.

## 6. 테스트 및 검증 명령어
코드 수정 후 반드시 터미널에서 아래 테스트를 통과해야 한다:
```bash
python -m py_compile app.py ai_advisor.py cloud_sync.py screener/portfolio.py screener/fmp_client.py
pytest tests/test_v4.py tests/test_cloud_keys.py
```
