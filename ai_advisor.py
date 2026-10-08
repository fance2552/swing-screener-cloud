"""Groq 기반 AI 전술 참모. 완전 독립 모듈. 스캐너/포트폴리오 파이프라인을 import 하지 않는다.

v3.5: 프롬프트 조립과 API 호출 분리. 뉴스는 fmp_news 가 모아 넣고, Groq 는 그 텍스트를 읽기만 한다.
  - build_prompt_text(...)        : 데이터를 받아 편집 가능한 프롬프트 '텍스트'만 돌려준다. API 호출 없음.
  - run_briefing_from_text(text)  : 완성된(혹은 사용자가 수정한) 프롬프트 텍스트를 그대로 Groq 에 전달한다.
  - generate_tactical_briefing(...) : 하위 호환용 래퍼. 위 두 함수를 순서대로 호출할 뿐이다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

try:
    from groq import Groq
except ImportError:
    Groq = None  # type: ignore[misc, assignment]

# llama-3.3-70b-versatile 는 이 키에서 404. 2026-10 기준 이 계정이 쓰는 텍스트 모델.
_MODEL = "openai/gpt-oss-120b"

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
    news_digest: dict[str, str] | None = None,
    recently_closed_today: list[str] | None = None,
) -> str:
    """프롬프트 텍스트만 조립. API 호출 없음. 호출자가 실행 시점 데이터를 넣어야 한다.

    session_meta 는 호출부 호환용으로만 받는다. 본문에 넣으면 장전/인터락 훈계를 유도하므로 넣지 않는다.
    news_digest 는 fmp_news 가 모은 종목별 헤드라인. Groq 는 검색하지 않는다.
    """

    extra = (extra_questions or "").strip()
    extra_block = extra if extra else "(추가 질문 없음)"
    news_block = "\n\n".join(
        f"### {sym}\n{text if text else '최근 특이 뉴스 없음'}"
        for sym, text in (news_digest or {}).items()
    ) or "없음"
    closed_list = [str(x) for x in (recently_closed_today or []) if x]
    closed_note = (
        f"오늘 이미 청산된 종목: {', '.join(closed_list)} — 이 종목들은 당일 재추천 1순위에서 제외하고 차순위를 추천할 것."
        if closed_list
        else "오늘 청산된 종목 없음."
    )
    open_kst, unlock_kst = _kst_session_times()
    regime_json = dumps_valid_json(regime_data, _REGIME_KEYS, 2000)
    held_json = dumps_valid_json(portfolio_data, _PF_KEYS)
    earn_json = dumps_valid_json(runup_data, _EARN_KEYS)
    sqz_json = dumps_valid_json(squeeze_data, _SQZ_KEYS)
    _ = session_meta  # 호출부 호환. 본문에 넣으면 장전 훈계를 유도하므로 넣지 않는다.
    return f"""당신은 월가 1티어 프랍 트레이딩 펌의 수석 퀀트 참모입니다.
트레이더는 이미 '{unlock_kst} KST 본장 수급 확인 후 진입'이라는 시간 원칙을 숙지하고 있습니다.
당신의 임무는 설교가 아니라, 아래 데이터 중에서 오늘 밤 사격할 가장 기대값 높은 후보를
날카롭게 분석하고 지정하는 것입니다. '사지 마라', '대기하라', '안전 수칙을 이행하라' 같은
진입 금지/설교성 문구는 절대 쓰지 마십시오.

[현재 시스템 데이터]
- 시장 국면: {regime_json}
- 트레이더의 현재 포트폴리오 (보유 종목): {held_json}
- 실적 런업 Top 10 후보: {earn_json}
- 숏스퀴즈 Top 10 후보 (상승여력 5% 미만은 이미 제외됨): {sqz_json}
- 회전문 방지 체크: {closed_note}

[최근 뉴스/보도자료 — Top 후보 및 보유종목]
{news_block}

뉴스가 있는 종목은 그 내용을 근거로 추천 사유를 설명하고, "최근 특이 뉴스 없음"으로 표시된 종목은 추측하지 말고 숫자 데이터만으로 판단하라.

[당신의 임무: 5대 전술 명령 작성]
군대식으로 차갑고 명확하게, 행동(Action) 위주로 아래 5개 번호를 매겨 작성할 것.
구구절절한 설명이나 안전 경고 반복은 금지한다.

1. [시황 판독]: SPY 국면을 요약하고, 오늘 밤 공격적/보수적 기조 중 하나를 딱 1줄로 지시하라.

2. [내 계좌 생사 판결]: 현재 포트폴리오의 종목별 헌법(SL, D-3, 트레일링 등) 판정을 요약하라.
   보유 종목이 없으면 "슬롯 100% 가용"이라고 명시하라.

3. [실적 런업 최우선 타겟]: 런업 Top 10 중 가장 점수와 상승여력이 좋은 단 1종목을 선정하라
   (단, 회전문 체크에서 제외 대상이면 차순위로). 현재가, 컨센서스 목표가, D-Day,
   {unlock_kst} 이후 눌림목 진입 전략을 명시하라. (120만 원 슬롯)

4. [숏스퀴즈 최우선 타겟]: 숏스퀴즈 Top 10 중 가장 폭발력 있는 단 1종목을 선정하라
   (단, 회전문 체크에서 제외 대상이면 차순위로). 숏비율, 거래량 배수, 상승여력과
   변동성 주의점을 명시하라. (60만 원 하프 슬롯)

5. [오늘 밤 최종 사격 지침]: 위 2개 종목을 오늘 밤 {unlock_kst} 본장 수급 확인 후 어떻게
   분할/배치하여 주문을 넣을지, 총 300만 원 한도 내에서 최종 요약하라.

[데이터 조작 금지]
위 JSON 데이터에 없는 수치를 지어내지 마라. 값이 비어 있거나 0건이면 그 사실을 그대로
인정하고 억지로 추천을 만들어내지 마라.

[추가 질문/지시사항]
{extra_block}"""


# 구버전 호출부 호환용 별칭
_build_prompt = build_prompt_text


def run_briefing_from_text(prompt_text: str) -> str:
    """완성된(혹은 사용자가 수정한) 프롬프트 텍스트를 그대로 Groq 에 전달한다.
    데이터 조립은 전혀 하지 않는다 — 호출자가 무엇을 보냈든 그대로 보낸다."""
    text = (prompt_text or "").strip()
    if not text:
        return "❌ 프롬프트가 비어 있습니다. 분석할 내용을 입력한 뒤 다시 시도하십시오."
    if Groq is None:
        return "❌ groq 패키지가 설치되어 있지 않습니다. `pip install groq` 후 다시 시도하십시오."

    try:
        import env_settings

        api_key = str((env_settings.get_api_keys() or {}).get("GROQ_API_KEY") or "").strip()
    except Exception:  # noqa: BLE001
        api_key = ""
    if not api_key:
        return "❌ GROQ_API_KEY가 설정되지 않았습니다. secrets.toml 또는 .env를 확인하십시오."

    try:
        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model=_MODEL,
            messages=[{"role": "user", "content": text}],
            temperature=0.3,
        )
        content = resp.choices[0].message.content
        return content or "❌ AI 응답이 비어 있습니다."
    except Exception as e:  # noqa: BLE001
        return f"❌ AI 분석 중 오류가 발생했습니다: {str(e)}"


def generate_tactical_briefing(
    regime_data: Any,
    portfolio_data: Any,
    runup_data: Any,
    squeeze_data: Any,
    recently_closed_today: list | None = None,
    extra_questions: str = "",
    session_meta: Any = None,
    news_digest: dict[str, str] | None = None,
) -> str:
    """하위 호환용 래퍼. build_prompt_text() + run_briefing_from_text() 를 순서대로 호출한다."""
    prompt = build_prompt_text(
        regime_data,
        portfolio_data,
        runup_data,
        squeeze_data,
        extra_questions=extra_questions,
        session_meta=session_meta,
        news_digest=news_digest,
        recently_closed_today=recently_closed_today,
    )
    return run_briefing_from_text(prompt)
