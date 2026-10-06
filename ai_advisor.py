"""Gemini 기반 AI 전술 참모. 완전 독립 모듈. 스캐너/포트폴리오 파이프라인을 import 하지 않는다.

v3.4: 프롬프트 조립과 API 호출 분리. JSON 은 행을 줄여서 자르지, 문자열 한가운데를 자르지 않는다.
  - build_prompt_text(...)        : 데이터를 받아 편집 가능한 프롬프트 '텍스트'만 돌려준다. API 호출 없음.
  - run_briefing_from_text(text)  : 완성된(혹은 사용자가 수정한) 프롬프트 텍스트를 그대로 Gemini 에 전달한다.
  - generate_tactical_briefing(...) : 하위 호환용 래퍼. 위 두 함수를 순서대로 호출할 뿐이다.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# 주의: gemini-1.5-flash 는 2025-09-29 에 완전히 shutdown 되어 더는 호출할 수 없다.
# google-generativeai 패키지 자체도 deprecated (google-genai 로 대체됨). 반드시 google-genai 사용.
try:
    from google import genai
except ImportError:
    genai = None

# 최신순 폴백 후보. Google 이 몇 달 단위로 모델을 shutdown 하므로 하나만 하드코딩하지 않는다.
# 최신 상태는 https://ai.google.dev/gemini-api/docs/deprecations 에서 확인할 것.
# 2026-09 기준: 3.8 > 3.7 > 3.6 > 3.5-flash-lite/3.5-flash(2027-05-19까지 보장) > 3-flash-preview
# > 2.5 세대 순. 신규 발급 키는 2.5 세대가 막혀 있을 수 있어 최신 세대를 먼저 시도한다.
_MODEL_CANDIDATES = [
    os.environ.get("GEMINI_MODEL", "").strip() or None,
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]
_MODEL_CANDIDATES = [m for m in _MODEL_CANDIDATES if m]

_JSON_BUDGET = 12000
_ET = ZoneInfo("America/New_York")
_KST = ZoneInfo("Asia/Seoul")

_EARN_KEYS = (
    "rank", "ticker", "name", "score", "signal", "price",
    "earnings_date", "d_day", "upside", "buy_ratio", "target_consensus", "sma50",
)
_SQZ_KEYS = (
    "rank", "ticker", "name", "score", "signal", "price",
    "short_float_pct", "rvol", "upside", "days_to_cover", "target_consensus",
)
_PF_KEYS = (
    "ticker", "strategy", "entry_price", "shares", "stop_price",
    "entry_date", "earnings_date", "target_consensus", "buy_ratio", "entry_fx",
    "exit_code", "exit_badge", "exit_order", "pct", "r_mult",
)
_REGIME_KEYS = ("state", "color", "label", "detail", "distribution_days")


def _load_gemini_key() -> str:
    """env_settings 를 우선 쓰되, 없거나 구조가 다르면 .env/환경변수를 직접 읽는다."""
    try:
        import env_settings

        keys = env_settings.get_api_keys()
        if isinstance(keys, dict) and keys.get("GEMINI_API_KEY"):
            return str(keys["GEMINI_API_KEY"]).strip()
    except Exception:  # noqa: BLE001
        pass
    env_val = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_val:
        return env_val
    env_path = Path.home() / "Desktop" / "swing-screener" / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _list_live_models(client: "genai.Client") -> list[str]:
    """최후 폴백: 이 키가 실제로 쓸 수 있는 모델을 직접 조회한다.
    curated 후보가 전부 실패했을 때만 호출한다(평소엔 API 호출 1번 아끼려고 안 부른다)."""
    try:
        names: list[str] = []
        for m in client.models.list():
            name = str(getattr(m, "name", "") or "").split("/")[-1]
            if not name or "flash" not in name:
                continue
            if any(bad in name for bad in ("image", "audio", "live", "tts", "embedding", "robotics")):
                continue
            actions = getattr(m, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            names.append(name)
        return names
    except Exception:  # noqa: BLE001
        return []


def _pick(row: Any, keys: tuple[str, ...]) -> Any:
    if not isinstance(row, dict):
        return row
    out: dict[str, Any] = {}
    for key in keys:
        if key in row:
            out[key] = row[key]
    return out


def dumps_valid_json(payload: Any, keys: tuple[str, ...] | None = None, budget: int = _JSON_BUDGET) -> str:
    """Always return parseable JSON. If over budget, drop the last list rows. Never slice the string."""
    data: Any
    if isinstance(payload, list):
        data = [_pick(row, keys) if keys else row for row in payload]
    elif isinstance(payload, dict) and keys:
        data = _pick(payload, keys)
    else:
        data = payload
    note = ""
    while True:
        try:
            text = json.dumps(data, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            text = json.dumps({"error": "serialize_failed", "repr": str(payload)[:500]}, ensure_ascii=False)
            break
        if len(text) <= budget:
            if note:
                if isinstance(data, list):
                    return json.dumps({"rows": data, "note": note}, ensure_ascii=False, default=str)
            return text
        if isinstance(data, list) and len(data) > 1:
            data = data[:-1]
            note = f"token budget {budget}: lowest ranks dropped, {len(data)} rows kept"
            continue
        if isinstance(data, dict) and data:
            data = {"truncated": True, "keys": list(data.keys())[:12]}
            continue
        return json.dumps({"error": "payload_too_large", "budget": budget}, ensure_ascii=False)


def _kst_session_times(now: datetime | None = None) -> tuple[str, str]:
    """다음 정규장 개장(09:30 ET)과 사격 해제(09:45 ET)를 KST "HH:MM"으로.
    미국 서머타임 중 22:30/22:45, 해제 후 23:30/23:45. 다음 평일 기준이라 DST 전환 주말에도 맞다."""
    ts = (now or datetime.now(_ET)).astimezone(_ET)
    day = ts.date()
    if ts.hour * 60 + ts.minute >= 9 * 60 + 45:
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)

    def _kst(hour: int, minute: int) -> str:
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_ET).astimezone(_KST).strftime("%H:%M")

    return _kst(9, 30), _kst(9, 45)


def build_prompt_text(
    regime_data: Any,
    portfolio_data: Any,
    runup_data: Any,
    squeeze_data: Any,
    extra_questions: str = "",
    session_meta: Any = None,
) -> str:
    """프롬프트 텍스트만 조립. API 호출 없음. 호출자가 실행 시점 데이터를 넣어야 한다.

    session_meta 는 호출부 호환용으로만 받는다. 본문에 넣으면 장전/인터락 훈계를 유도하므로 넣지 않는다.
    """

    extra = (extra_questions or "").strip()
    extra_block = extra if extra else "(추가 질문 없음)"
    open_kst, unlock_kst = _kst_session_times()
    regime_json = dumps_valid_json(regime_data, _REGIME_KEYS, 2000)
    held_json = dumps_valid_json(portfolio_data, _PF_KEYS)
    earn_json = dumps_valid_json(runup_data, _EARN_KEYS)
    sqz_json = dumps_valid_json(squeeze_data, _SQZ_KEYS)
    return f"""당신은 월가 1티어 프랍 트레이딩 펌의 수석 퀀트 전략 참모입니다.
트레이더는 미국 정규장 개장({open_kst} KST) 직후 이 브리핑을 읽고, {unlock_kst} KST(정규장 15분 후 수급/눌림목 지지 확인 시점)에 사격할 타겟을 결정합니다.

[절대 작성 수칙 — 위반 금지]
1. 시간/인터락 훈계 금지: "지금은 장전이니 사지 마라", "현금 100% 보유하라" 같은 시간 잔소리는 일체 하지 마라. 트레이더는 이미 {unlock_kst} 사격 규율을 완벽히 숙지하고 있다.
2. 단순 1위 앵무새 복사 금지: 표의 1등 종목을 무조건 최고라고 칭찬하지 마라.
   - 스퀴즈 후보군에서 1위 종목의 한계(예: 점수는 높으나 목표가 상승여력이 낮음 등)를 냉정히 짚어라.
   - 숏비율(Short Float %), 거래량 폭증 배수, 컨센서스 상승여력을 종합 비교하여 가장 비대칭적 손익비(Risk-Reward)를 가진 진짜 대장주(1위 또는 2~5위 중 역제안)를 선정하라.
3. 데이터 조작 금지: 주어진 JSON에 없는 수치를 새로 지어내지 말고, 오직 제공된 실제 수치(현재가, 점수, 숏비율, 여력 등)만 인용하라.
   값이 0 이거나 비어 있으면(예: target_consensus 0.0) 그 수치를 인용하지 말고 "데이터 없음"이라고 써라.

[현재 시스템 입력 데이터]
- 시장 국면: {regime_json}
- 트레이더 현재 포트폴리오: {held_json}
- 실적 런업 Top 10 후보: {earn_json}
- 숏스퀴즈 Top 10 후보: {sqz_json}

[당신의 임무: 5대 정밀 전술 명령]
구구절절한 잡설을 빼고, 군대식으로 차갑고 명확하게 아래 5개 번호를 매겨 작성할 것.

1. [시황 판독 & 공격 기조]
   - 현재 시장 국면({regime_json})을 바탕으로 오늘 밤 공격적 사격(비중 확대)을 할지, 보수적 방어(런업 위주/비중 축소)를 할지 딱 1줄로 지시하라.

2. [내 계좌 생사 판결]
   - 보유 종목이 있다면 데이터에 포함된 청산 헌법 배지(SL/TP/D-3)를 기반으로 기계적 즉시 처분을 지시하라. (보유 종목이 0건이면 "현재 보유 없음. 3개 슬롯 전액 신규 가용 가능"이라고 명시)

3. [실적 런업 최우선 타격 1픽 (안정형 120만 원 슬롯)]
   - 런업 Top 10 중 가장 점수와 상승여력 밸런스가 좋은 최적 1종목을 선정하라.
   - 티커, 점수, 현재가, 컨센서스 목표가, D-Day를 명시하고 {unlock_kst} 이후 눌림목 지지 확인 진입 포인트를 지시하라.

4. [숏스퀴즈 최우선 타격 1픽 (고위험 60만 원 하프 슬롯)]
   - 스퀴즈 Top 10 중 가장 폭발력 있는 1종목을 선정하라.
   - 단순 1위의 맹점(약점)과 비교 분석하여, 왜 이 종목의 숏비율과 상승여력이 오늘 밤 가장 매력적인지 구체적 수치로 증명하라. (1위가 진짜 최선이면 1위를, 2~5위가 더 우월하면 2~5위를 선정할 것)

5. [오늘 밤 {unlock_kst} 최종 자금 집행 명령]
   - 트레이더의 총 운용 자금은 300만 원(런업 120만 / 스퀴즈 60만 / 현금 버퍼 120만)이다.
   - 위에서 선정한 종목들을 {unlock_kst} 본장 수급 확인 후 어떻게 배치하고 진입할지 최종 실행 명령을 3줄 이내로 요약하라.

[추가 질문/지시사항]
{extra_block}"""


# 구버전 호출부 호환용 별칭
_build_prompt = build_prompt_text


def run_briefing_from_text(prompt_text: str) -> str:
    """완성된(혹은 사용자가 수정한) 프롬프트 텍스트를 그대로 Gemini 에 전달한다.
    데이터 조립은 전혀 하지 않는다 — 호출자가 무엇을 보냈든 그대로 보낸다."""
    if genai is None:
        return "❌ google-genai 패키지가 설치되어 있지 않습니다. `pip install google-genai` 후 다시 시도하십시오."

    text = (prompt_text or "").strip()
    if not text:
        return "❌ 프롬프트가 비어 있습니다. 분석할 내용을 입력한 뒤 다시 시도하십시오."

    api_key = _load_gemini_key()
    if not api_key:
        return "❌ GEMINI_API_KEY가 설정되지 않았습니다. 사이드바나 .env를 확인하십시오."

    try:
        client = genai.Client(api_key=api_key)
    except Exception as exc:  # noqa: BLE001
        return f"❌ Gemini 클라이언트 생성 실패: {exc}"

    tried: list[str] = []
    errors: list[str] = []

    def _attempt(model_name: str) -> str | None:
        if model_name in tried:
            return None
        tried.append(model_name)
        try:
            response = client.models.generate_content(model=model_name, contents=text)
            out = getattr(response, "text", None)
            if out:
                return out
            errors.append(f"{model_name}: 응답이 비어 있음")
        except Exception as exc:  # noqa: BLE001
            # 404 = 모델명이 틀렸거나 이 키에 없음 / 403 = 권한(요금제) 문제 / 429 = 쿼터 초과.
            errors.append(f"{model_name}: {exc}")
        return None

    for model_name in _MODEL_CANDIDATES:
        result = _attempt(model_name)
        if result:
            return result

    live_models = [m for m in _list_live_models(client) if m not in tried]
    for model_name in live_models[:5]:  # 과도한 재시도 방지, 상위 5개만
        result = _attempt(model_name)
        if result:
            return result

    detail = "\n".join(f"  - {e}" for e in errors) or "(오류 상세 없음)"
    return (
        "❌ AI 분석 중 오류가 발생했습니다: 시도한 모델이 모두 실패했습니다.\n"
        f"{detail}\n"
        "https://ai.google.dev/gemini-api/docs/deprecations 에서 현재 사용 가능한 모델명을 "
        "확인해 GEMINI_MODEL 환경변수로 지정하거나, 위 에러가 403/권한 관련이면 "
        "Google AI Studio에서 이 API 키의 요금제/모델 접근 권한을 확인하십시오."
    )


def generate_tactical_briefing(regime_data: Any, portfolio_data: Any, runup_data: Any, squeeze_data: Any) -> str:
    """하위 호환용 래퍼. build_prompt_text() + run_briefing_from_text() 를 순서대로 호출한다."""
    prompt = build_prompt_text(regime_data, portfolio_data, runup_data, squeeze_data)
    return run_briefing_from_text(prompt)
