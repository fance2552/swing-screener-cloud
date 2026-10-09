"""Groq 단일 모델 기반 AI 전술 위원회. 완전 독립 모듈. 스캐너/포트폴리오 파이프라인을 import 하지 않는다.

v4.1: Cerebras 의존성을 완전히 제거하고 Groq 하나로 단일화했다.
  동시에 Groq 무료 한도(요청당 약 8,000토큰) 초과로 인한 413을 막기 위해
  데이터/보고서 트림 길이를 낮추고 Master 프롬프트에서 원본 JSON 재삽입을 없앴다.
  - Agent A (펀더멘털/어닝): Groq.
  - Agent B (수급/차트): Groq.
  - Agent C (리스크/거시): Groq.
  - Master (최종 결정권자): Groq.
"""
from __future__ import annotations

import json
import os
from typing import Any

try:
    from groq import Groq
except ImportError:
    Groq = None  # type: ignore[misc, assignment]

_GROQ_MODEL = "openai/gpt-oss-120b"
_DATA_TRIM_CHARS = 1500
_AGENT_REPORT_TRIM_CHARS = 700


def _load_key(name: str) -> str:
    """st.secrets → os.environ → 앱 폴더 .env. env_settings.get_key 는 없다."""
    try:
        import env_settings
        got = str((env_settings.get_api_keys() or {}).get(name) or "").strip()
        if got:
            return got
    except Exception:  # noqa: BLE001
        pass
    key = os.environ.get(name, "").strip()
    if key:
        return key
    try:
        from pathlib import Path
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _groq_chat(
    system_prompt: str,
    user_prompt: str,
    model: str = _GROQ_MODEL,
    max_tokens: int = 700,
) -> str:
    key = _load_key("GROQ_API_KEY")
    if not Groq:
        raise RuntimeError("groq 패키지가 설치되어 있지 않음 (requirements.txt에 groq 추가 필요)")
    if not key:
        raise RuntimeError("GROQ_API_KEY가 비어있음 (Streamlit Secrets 확인 필요)")
    client = Groq(api_key=key, timeout=20.0)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    # gpt-oss 는 숨은 추론 토큰이 max_tokens 를 먼저 먹고, 본문이 한 줄에서 끊긴다.
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.3,
            max_tokens=max_tokens,
            reasoning_effort="low",
        )
    except Exception as exc:  # noqa: BLE001
        if "reasoning_effort" not in str(exc):
            raise
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.3,
            max_tokens=max_tokens,
        )
    choice = resp.choices[0]
    message = choice.message
    content = (getattr(message, "content", None) or "").strip()
    if not content:
        content = str(
            getattr(message, "reasoning", None)
            or getattr(message, "reasoning_content", None)
            or ""
        ).strip()
    if not content:
        content = "❌ Groq 본문이 비었다."
    if getattr(choice, "finish_reason", None) == "length":
        content += "\n\n(응답이 출력 한도에서 끊겼습니다.)"
    return content


def _j(x: Any, max_chars: int = _DATA_TRIM_CHARS) -> str:
    try:
        text = json.dumps(x, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(x)
    if len(text) > max_chars:
        return text[:max_chars] + "...(생략)"
    return text


def _trim(text: str, max_chars: int = _AGENT_REPORT_TRIM_CHARS) -> str:
    if not text:
        return text
    if len(text) > max_chars:
        return text[:max_chars] + "...(이하 생략)"
    return text


def _agent_a_fundamental(portfolio_data: Any, runup_data: Any) -> str:
    """AI A: 펀더멘털/어닝 분석가 (Groq)."""
    prompt = f"""당신은 펀더멘털/어닝 분석가입니다. 아래 데이터를 보고 보유 종목 및 런업 후보의
실적 D-Day, 컨센서스 목표가, 서프라이즈 가능성을 짧고 날카롭게 분석하십시오. 데이터에 없는 수치는
절대 지어내지 마십시오.

[보유 포트폴리오]: {_j(portfolio_data)}
[실적 런업 Top 10]: {_j(runup_data)}"""
    return _groq_chat(
        system_prompt="당신은 펀더멘털/어닝 분석가입니다. 데이터에 없는 수치는 지어내지 마십시오.",
        user_prompt=prompt,
    )


def _agent_b_flow(squeeze_data: Any) -> str:
    """AI B: 수급/차트 분석가 (Groq)."""
    prompt = f"""당신은 수급/차트 분석가입니다. 아래 숏스퀴즈 후보들의 화력(숏비율/거래량배수),
ATR 변동성 대비 노이즈 수준을 짧고 날카롭게 분석하십시오. 데이터에 없는 수치는 지어내지 마십시오.

[숏스퀴즈 Top 10]: {_j(squeeze_data)}"""
    return _groq_chat(system_prompt="당신은 수급/차트 분석가입니다.", user_prompt=prompt)


def _agent_c_risk(regime_data: Any, portfolio_data: Any) -> str:
    """AI C: 리스크/거시 분석가 (Groq)."""
    prompt = f"""당신은 리스크/거시 분석가입니다. 현재 시장 국면과 보유 포트폴리오를 보고
섹터 악재 및 주말 갭 리스크를 짧고 날카롭게 짚으십시오. 데이터에 없는 수치는 지어내지 마십시오.

[시장 국면]: {_j(regime_data)}
[보유 포트폴리오]: {_j(portfolio_data)}"""
    return _groq_chat(system_prompt="당신은 리스크/거시 분석가입니다.", user_prompt=prompt)


def run_committee_briefing(
    regime_data: Any,
    portfolio_data: Any,
    runup_data: Any,
    squeeze_data: Any,
    recently_closed_today: list | None = None,
) -> str:
    """Groq 3+1 위원회 파이프라인. app.py의 Tab3 실행 버튼이 이 함수 하나만 호출하면 된다."""
    closed_list = recently_closed_today or []
    closed_note = (
        f"오늘 이미 청산된 종목: {', '.join(closed_list)} — 신규 추천(미션 2)에서 반드시 제외할 것."
        if closed_list
        else "오늘 청산된 종목 없음."
    )
    errors: list[str] = []
    try:
        report_a = _agent_a_fundamental(portfolio_data, runup_data)
    except Exception as exc:  # noqa: BLE001
        report_a = "(Agent A 분석 실패)"
        errors.append(f"Agent A 실패: {exc}")
    try:
        report_b = _agent_b_flow(squeeze_data)
    except Exception as exc:  # noqa: BLE001
        report_b = "(Agent B 분석 실패)"
        errors.append(f"Agent B 실패: {exc}")
    try:
        report_c = _agent_c_risk(regime_data, portfolio_data)
    except Exception as exc:  # noqa: BLE001
        report_c = "(Agent C 분석 실패)"
        errors.append(f"Agent C 실패: {exc}")

    def _ticker_list(data: Any) -> str:
        try:
            if isinstance(data, list):
                tickers = [
                    str(row.get("ticker"))
                    for row in data
                    if isinstance(row, dict) and row.get("ticker")
                ]
                return ", ".join(tickers[:10]) if tickers else "없음"
        except Exception:  # noqa: BLE001
            pass
        return "없음"

    master_prompt = f"""당신은 3인의 애널리스트 보고서를 종합하는 마스터 AI(최종 결정권자)입니다.
아래는 원본 데이터가 아니라 이미 분석이 끝난 요약 보고서입니다. 이것만 보고 아래 출력 템플릿 그대로
최종 브리핑을 작성하십시오. 설교나 안전 경고 반복 없이, 군더더기 없는 행동 지침 위주로 작성합니다.

[Agent A - 펀더멘털/어닝 요약]: {_trim(report_a)}

[Agent B - 수급/차트 요약]: {_trim(report_b)}

[Agent C - 리스크/거시 요약]: {_trim(report_c)}

[참고용 티커 목록 (원본 전체는 A/B/C가 이미 분석함)]
- 실적 런업 후보: {_ticker_list(runup_data)}
- 숏스퀴즈 후보: {_ticker_list(squeeze_data)}
- 회전문 체크: {closed_note}

[출력 템플릿 — 반드시 이 구조 그대로]

■ 미션 1: 현재 포트폴리오 전 종목 감리
(토큰 예산이 한정되어 있으니 티커당 반드시 4줄 이내로 간결하게 작성할 것. 장황한 설명 금지.)
보유 종목이 없으면 "보유 종목 없음 — 슬롯 100% 가용"이라고만 쓰십시오.
있다면 티커별로 아래 형식을 반복하십시오:
- [티커]: A/B/C 3자 의견 요약 (1줄) → 최종 결정: HOLD / TRIM / EXIT
  목표가: $XX (+XX%) / 손절·무효화 기준: $XX / 권장 홀딩 기한: (D-Day 기준 구체적 날짜)

■ 미션 2: 신규 1~2픽 타겟 추천
가용 슬롯이 있을 때만 작성. 실적 런업/숏스퀴즈 Top10 중 손익비 최고 1픽을 선정하고
(회전문 체크에 걸리면 차순위로) 아래 4개 항목을 반드시 명시:
목표가(및 %) / 권장 홀딩 기한 / 무효화 조건 / 포지션 배분액

위 요약에 없는 수치는 절대 지어내지 마십시오. 값이 비어 있으면 그 사실을 그대로 인정하십시오."""
    try:
        final_report = _groq_chat(
            system_prompt="당신은 마스터 AI(최종 결정권자)입니다.",
            user_prompt=master_prompt,
            max_tokens=2000,
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Master 종합 실패: {exc}")
        final_report = (
            "⚠️ 마스터 종합 실패 — 개별 Agent 보고서만 표시합니다.\n\n"
            f"[Agent A]\n{report_a}\n\n[Agent B]\n{report_b}\n\n[Agent C]\n{report_c}"
        )
    if errors:
        final_report = "⚠️ " + " / ".join(errors) + "\n\n" + final_report
    return final_report or "❌ Groq 본문이 비었다."
