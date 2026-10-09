"""Groq + Cerebras 기반 AI 전술 위원회. 완전 독립 모듈. 스캐너/포트폴리오 파이프라인을 import 하지 않는다.

v4.0: Gemini 의존성을 완전히 제거했다.
  - Agent A (펀더멘털/어닝): Cerebras 1차 시도, 실패 시 Groq가 즉시 대타.
  - Agent B (수급/차트): Groq.
  - Agent C (리스크/거시): Groq.
  - Master (최종 결정권자): Groq.
"""
from __future__ import annotations

import json
import os
import concurrent.futures
from typing import Any

try:
    from groq import Groq
except ImportError:
    Groq = None  # type: ignore[misc, assignment]

try:
    from cerebras.cloud.sdk import Cerebras
except ImportError:
    Cerebras = None  # type: ignore[misc, assignment]

_GROQ_MODEL = "openai/gpt-oss-120b"
# 3초면 Cerebras가 끝나기 전에 끊기고 Groq로만 몰린다. 그러면 분당 8,000토큰을 다시 넘는다.
_CEREBRAS_TIMEOUT_SEC = 25
_CEREBRAS_MODEL = "llama-3.3-70b"

_EARN_KEYS = (
    "rank", "ticker", "name", "score", "signal", "price",
    "earnings_date", "d_day", "upside", "buy_ratio", "target_consensus",
)
_SQZ_KEYS = (
    "rank", "ticker", "name", "score", "signal", "price",
    "short_float_pct", "rvol", "upside", "days_to_cover", "target_consensus",
)
_PF_KEYS = (
    "ticker", "strategy", "entry_price", "shares", "stop_price",
    "entry_date", "earnings_date", "exit_code", "pct", "r_mult",
)
_REGIME_KEYS = ("state", "color", "label", "detail", "distribution_days")


def _load_key(name: str) -> str:
    """st.secrets → os.environ → 앱 폴더 .env. env_settings.get_key 는 없다."""
    try:
        import env_settings
        got = str((env_settings.get_api_keys() or {}).get(name) or "").strip()
        if got:
            return got
    except Exception:  # noqa: BLE001
        pass
    return str(os.environ.get(name) or "").strip()


def _groq_chat(system_prompt: str, user_prompt: str, model: str = _GROQ_MODEL) -> str:
    key = _load_key("GROQ_API_KEY")
    if Groq is None or not key:
        raise RuntimeError("Groq 클라이언트 또는 GROQ_API_KEY 없음")
    client = Groq(api_key=key)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content or ""


def _cerebras_chat(
    system_prompt: str,
    user_prompt: str,
    model: str = _CEREBRAS_MODEL,
    timeout_sec: int = _CEREBRAS_TIMEOUT_SEC,
) -> str:
    key = _load_key("CEREBRAS_API_KEY")
    if Cerebras is None or not key:
        raise RuntimeError("Cerebras 클라이언트 또는 CEREBRAS_API_KEY 없음")
    client = Cerebras(api_key=key)

    def _call() -> str:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )
        return resp.choices[0].message.content or ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_call).result(timeout=timeout_sec)


def _clip(text: str, limit: int) -> str:
    raw = (text or "").strip()
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "…"


def _pick(row: Any, keys: tuple[str, ...] | None) -> Any:
    if not isinstance(row, dict) or not keys:
        return row
    return {key: row[key] for key in keys if key in row}


def _j(x: Any) -> str:
    try:
        return json.dumps(x, ensure_ascii=False, default=str)[:4000]
    except Exception:  # noqa: BLE001
        return str(x)[:4000]


def _groq_blob(payload: Any, keys: tuple[str, ...] | None = None, rows: int = 5) -> str:
    """Groq 무료 한도는 요청당 8,000토큰. 원문 4,000자×3은 413이 난다."""
    if isinstance(payload, list):
        data: Any = [_pick(row, keys) for row in payload[:rows]]
    elif isinstance(payload, dict):
        data = _pick(payload, keys)
    else:
        data = payload
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(data)
    return _clip(text, 650)


def _agent_a_fundamental(portfolio_data: Any, runup_data: Any) -> str:
    """AI A: 펀더멘털/어닝. Cerebras 1차, 실패 시 Groq 대타. Groq 대타 분량은 한도 안쪽."""
    prompt = f"""당신은 펀더멘털/어닝 분석가입니다. 아래 데이터를 보고 보유 종목 및 런업 후보의
실적 D-Day, 컨센서스 목표가, 서프라이즈 가능성을 짧고 날카롭게 분석하십시오. 데이터에 없는 수치는
절대 지어내지 마십시오. 600자 안으로 쓰십시오.

[보유 포트폴리오]: {_j(portfolio_data)}
[실적 런업 Top 10]: {_j(runup_data)}"""
    try:
        return _cerebras_chat(
            system_prompt="당신은 펀더멘털/어닝 분석가입니다. 데이터에 없는 수치는 지어내지 마십시오.",
            user_prompt=prompt,
        )
    except Exception as exc:  # noqa: BLE001
        slim = f"""펀더멘털/어닝. 없는 수치는 만들지 마십시오. 400자 안.

[보유]: {_groq_blob(portfolio_data, _PF_KEYS)}
[런업]: {_groq_blob(runup_data, _EARN_KEYS)}"""
        note = f"(Cerebras 호출 실패로 Groq가 대타 수행 — 사유: {exc})\n\n"
        return note + _groq_chat(
            system_prompt="당신은 펀더멘털/어닝 분석가입니다. 데이터에 없는 수치는 지어내지 마십시오.",
            user_prompt=slim,
        )


def _agent_b_flow(squeeze_data: Any) -> str:
    """AI B: 수급/차트 분석가 (Groq)."""
    prompt = f"""수급/차트. 숏비율, 거래량배수, 상승여력만. 400자 안. 없는 수치는 만들지 마십시오.

[숏스퀴즈]: {_groq_blob(squeeze_data, _SQZ_KEYS)}"""
    return _groq_chat(system_prompt="당신은 수급/차트 분석가입니다.", user_prompt=prompt)


def _agent_c_risk(regime_data: Any, portfolio_data: Any) -> str:
    """AI C: 리스크/거시 분석가 (Groq)."""
    prompt = f"""리스크/거시. 국면과 보유 종목의 주말 갭만. 400자 안. 없는 수치는 만들지 마십시오.

[국면]: {_groq_blob(regime_data, _REGIME_KEYS, rows=1)}
[보유]: {_groq_blob(portfolio_data, _PF_KEYS)}"""
    return _groq_chat(system_prompt="당신은 리스크/거시 분석가입니다.", user_prompt=prompt)


def run_committee_briefing(
    regime_data: Any,
    portfolio_data: Any,
    runup_data: Any,
    squeeze_data: Any,
    recently_closed_today: list | None = None,
) -> str:
    """Cerebras+Groq 3+1 위원회. app.py 실행 버튼이 이 함수만 호출한다."""
    closed_list = [str(item) for item in (recently_closed_today or []) if item]
    closed_note = (
        f"오늘 이미 청산된 종목: {', '.join(closed_list)} — 미션 2에서 제외."
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

    master_prompt = f"""3인 보고서를 합쳐 아래 템플릿만 작성하십시오. 없는 수치는 만들지 마십시오.

[펀더멘털]: {_clip(report_a, 500)}
[수급]: {_clip(report_b, 400)}
[리스크]: {_clip(report_c, 400)}
[보유]: {_groq_blob(portfolio_data, _PF_KEYS)}
[런업]: {_groq_blob(runup_data, _EARN_KEYS)}
[스퀴즈]: {_groq_blob(squeeze_data, _SQZ_KEYS)}
[회전문]: {closed_note}

■ 미션 1: 보유 종목 감리
없으면 "보유 종목 없음 — 슬롯 100% 가용".
있으면 티커별: 요약 → HOLD / TRIM / EXIT, 목표가, 손절, 홀딩 기한.

■ 미션 2: 신규 1~2픽
회전문 종목은 제외. 목표가(%) / 홀딩 기한 / 무효화 / 배분액."""
    try:
        final_report = _groq_chat(
            system_prompt="당신은 마스터 AI(최종 결정권자)입니다.",
            user_prompt=master_prompt,
        )
    except Exception as exc:  # noqa: BLE001
        body = f"## Cerebras/Groq 전술1\n\n{report_a}\n\n❌ Groq 마스터 실패: {exc}"
        if errors:
            body = "⚠️ " + " / ".join(errors) + "\n\n" + body
        return body
    if errors:
        final_report = "⚠️ " + " / ".join(errors) + "\n\n" + final_report
    return final_report
