"""Groq 기반 AI 전술 위원회 — 2개의 독립 파이프라인.

완전 독립 모듈. 스캐너/포트폴리오 파이프라인을 import 하지 않는다.

v5.0: 단일 파이프라인을 폐기하고 완전히 분리된 2개 파이프라인으로 재설계했다.
  - run_portfolio_audit(...): 보유 종목 생사 판결 전용 (4단계: 펀더멘털/수급/리스크/마스터CIO)
  - run_new_hunt(...)       : 신규 Top3 발굴 (5단계: PEAD/런업/RSI2/리스크&쿨다운/마스터CIO)
각 파이프라인은 토큰 예산을 서로 침범하지 않으며, 각자 독립적으로 실패/성공한다.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

import requests

try:
    from groq import Groq
except ImportError:
    Groq = None  # type: ignore[misc, assignment]

_GROQ_MODEL = "openai/gpt-oss-120b"
_DEEPSEEK_MODEL = "deepseek-chat"
_DEEPSEEK_BASE_URL = "https://api.deepseek.com/chat/completions"
_DEEPSEEK_TIMEOUT_SEC = 15
_AGENT_TIMEOUT_SEC = 8  # Groq는 통상 1초 내 응답. 길게 잡으면 웹소켓 핑이 끊긴다.
VERDICT_ENTER_NOW = "ENTER_NOW"
VERDICT_WAIT = "WAIT_CONDITIONAL"
VERDICT_NO_ENTRY = "NO_ENTRY"
VERDICT_LABEL = {
    VERDICT_ENTER_NOW: "🟢 [당장 진입 추천]",
    VERDICT_WAIT: "🟡 [조건부 대기 진입]",
    VERDICT_NO_ENTRY: "🔴 [진입 금지 (패스)]",
}
_VERDICT_TOKEN_MAP = {
    "ENTER_NOW": VERDICT_ENTER_NOW,
    "WAIT_CONDITIONAL": VERDICT_WAIT,
    "WAIT": VERDICT_WAIT,
    "NO_ENTRY": VERDICT_NO_ENTRY,
    "PASS": VERDICT_NO_ENTRY,
}
AUDIT_VERDICT_HOLD = "HOLD"
AUDIT_VERDICT_TRIM = "TRIM"
AUDIT_VERDICT_EXIT = "EXIT"
AUDIT_VERDICT_LABEL = {
    AUDIT_VERDICT_HOLD: "🟢 [HOLD 홀딩 유지]",
    AUDIT_VERDICT_TRIM: "🟡 [TRIM 부분 익절]",
    AUDIT_VERDICT_EXIT: "🔴 [EXIT 즉시 매도]",
}
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
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _sanitize_dollar(text: str) -> str:
    """'$17.96' 를 'USD 17.96' 으로 바꿔 Streamlit LaTeX 오인을 막는다."""
    if not text:
        return text
    return re.sub(r"\$\s*(\d)", r"USD \1", text)


def _clean_model_text(text: str) -> str:
    """모델이 넣은 <br> 과 달러 기호를 읽히는 문장으로 바꾼다."""
    if not text:
        return text
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", text)
    cleaned = re.sub(r"</?[a-zA-Z][^>]*>", "", cleaned)
    return _sanitize_dollar(cleaned)


def _rate_limit_note(exc: Exception) -> str:
    raw = str(exc)
    if "429" not in raw and "rate_limit" not in raw:
        return raw[:400]
    wait = re.search(r"try again in ([0-9hms.]+)", raw)
    when = f" {wait.group(1)} 뒤에 다시 누르면 된다." if wait else ""
    return f"Groq 일일 토큰 20만을 전부 썼다.{when} 그 전에는 감리도 발굴도 호출하지 마라."


def _groq_chat(
    system_prompt: str,
    user_prompt: str,
    model: str = _GROQ_MODEL,
    max_tokens: int = 700,
    reasoning_effort: str = "low",
) -> str:
    key = _load_key("GROQ_API_KEY")
    if not Groq:
        raise RuntimeError("groq 패키지가 설치되어 있지 않음 (requirements.txt에 groq 확인)")
    if not key:
        raise RuntimeError("GROQ_API_KEY가 비어있음 (Streamlit Secrets 확인 필요)")
    client = Groq(api_key=key, timeout=float(_AGENT_TIMEOUT_SEC))
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": user_prompt
                + "\n\n(출력 시 달러 기호 '$' 대신 'USD'를 사용할 것 — 서식 파싱 오류 방지)",
            },
        ],
        temperature=0.3,
        max_tokens=max_tokens,
    )
    try:
        resp = client.chat.completions.create(reasoning_effort=reasoning_effort, **kwargs)
    except Exception as exc:  # noqa: BLE001
        if "reasoning_effort" not in str(exc):
            raise
        resp = client.chat.completions.create(**kwargs)
    choice = resp.choices[0]
    message = choice.message
    text = (getattr(message, "content", None) or "").strip()
    if not text:
        text = str(
            getattr(message, "reasoning", None)
            or getattr(message, "reasoning_content", None)
            or ""
        ).strip()
    if not text:
        text = "❌ Groq 본문이 비었다."
    return _clean_model_text(text)


def _deepseek_chat(system_prompt: str, user_prompt: str, max_tokens: int = 1200) -> str:
    key = _load_key("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY 없음")
    resp = requests.post(
        _DEEPSEEK_BASE_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": _DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt + "\n\n(출력 시 '$' 대신 'USD'를 사용할 것)"},
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        },
        timeout=_DEEPSEEK_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    data = resp.json()
    text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    if not text:
        raise RuntimeError("DeepSeek 본문이 비었다")
    return _clean_model_text(text)


def _cio_chat(system_prompt: str, user_prompt: str, max_tokens: int = 1200) -> str:
    """총괄 CIO. DeepSeek이 없으면 Groq가 같은 결재를 쓴다."""
    try:
        return _deepseek_chat(system_prompt, user_prompt, max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001
        reason = _rate_limit_note(exc)[:180]
        return _clean_model_text(
            f"(DeepSeek 대신 Groq가 CIO 결재를 썼다. 사유: {reason})\n\n"
            + _groq_chat(system_prompt, user_prompt, max_tokens=max_tokens)
        )


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


def _holding_tickers(data: Any, limit: int = 10) -> list[str]:
    """_ticker_list는 'A, B' 문자열이다. 감리 카드는 리스트가 필요하다."""
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for row in data:
        if isinstance(row, dict) and row.get("ticker"):
            out.append(str(row["ticker"]).upper())
        if len(out) >= limit:
            break
    return out


def _ticker_list(data: Any, limit: int = 10) -> str:
    try:
        if isinstance(data, list):
            tickers = [
                str(row.get("ticker"))
                for row in data
                if isinstance(row, dict) and row.get("ticker")
            ]
            return ", ".join(tickers[:limit]) if tickers else "없음"
    except Exception:  # noqa: BLE001
        pass
    return "없음"


def _holding_brief(data: Any, limit: int = 10) -> str:
    """티커와 전략 필드를 같이 넘긴다. 전략 분기에 티커만 주면 마스터가 전략을 지어낸다."""
    try:
        if isinstance(data, list):
            bits = []
            for row in data:
                if not isinstance(row, dict) or not row.get("ticker"):
                    continue
                strat = str(row.get("strategy") or "미상")
                bits.append(f"{row.get('ticker')}({strat})")
            return ", ".join(bits[:limit]) if bits else "없음"
    except Exception:  # noqa: BLE001
        pass
    return "없음"


_AUDIT_SHAPE = """
티커마다 마크다운 표 한 줄로 쓰고, 표 아래에 근거를 문장으로 끝내라.
HTML 태그(<br> 포함)는 쓰지 마라. 문장 중간에서 끊지 마라. 길면 문장을 줄여 완결하라.
데이터에 없는 수치는 만들지 마라.
"""


def _audit_agent1_fundamental(portfolio_data: Any) -> str:
    prompt = f"""당신은 펀더멘털/어닝 분석가입니다. 보유 종목마다 실적 D-Day, 컨센서스 목표가,
현재가 대비 괴리, 서프라이즈 가능성을 표로 보여라.
{_AUDIT_SHAPE}
[보유 포트폴리오]: {_j(portfolio_data)}"""
    return _groq_chat(
        system_prompt="당신은 펀더멘털/어닝 분석가입니다. 데이터에 없는 수치는 지어내지 마십시오.",
        user_prompt=prompt,
        max_tokens=1400,
    )


def _audit_agent2_flow(portfolio_data: Any) -> str:
    prompt = f"""당신은 수급/차트 분석가입니다. 보유 종목마다 단기 수급, 손절선, 지지, 변동성 노이즈를
표로 보여라.
{_AUDIT_SHAPE}
[보유 포트폴리오]: {_j(portfolio_data)}"""
    return _groq_chat(
        system_prompt="당신은 수급/차트 분석가입니다.",
        user_prompt=prompt,
        max_tokens=1400,
    )


def _audit_agent3_risk(regime_data: Any, portfolio_data: Any) -> str:
    prompt = f"""당신은 리스크/거시 분석가입니다. 국면을 먼저 한 단락으로 요약하고,
보유 종목마다 핵심 리스크와 주말 갭 위험을 표로 보여라.
{_AUDIT_SHAPE}
[시장 국면]: {_j(regime_data)}
[보유 포트폴리오]: {_j(portfolio_data)}"""
    return _groq_chat(
        system_prompt="당신은 리스크/거시 분석가입니다.",
        user_prompt=prompt,
        max_tokens=1400,
    )


def _parse_audit_positions(raw_text: str, tickers: list[str]) -> list[dict[str, str]]:
    """[POSITION] 블록을 카드로 바꾼다. 마커가 없어도 보유 티커는 HOLD 카드로 남는다."""
    text = raw_text or ""
    blocks = re.findall(r"\[POSITION\]\s*(.*?)\[/POSITION\]", text, re.DOTALL)
    cards: list[dict[str, str]] = []
    found: set[str] = set()

    def _grab(block: str, pattern: str, default: str = "정보 없음") -> str:
        found_row = re.search(pattern, block, re.DOTALL)
        return found_row.group(1).strip() if found_row else default

    for block in blocks:
        ticker = _grab(block, r"\[TICKER\]\s*(.+?)(?:\n\[|\Z)", "").strip().upper()
        if not ticker:
            continue
        found.add(ticker)
        verdict_token = _grab(block, r"\[VERDICT\]\s*([A-Z]+)", "").strip()
        verdict_code = verdict_token if verdict_token in AUDIT_VERDICT_LABEL else AUDIT_VERDICT_HOLD
        cards.append({
            "ticker": ticker,
            "verdict_code": verdict_code,
            "verdict_label": AUDIT_VERDICT_LABEL.get(verdict_code, AUDIT_VERDICT_LABEL[AUDIT_VERDICT_HOLD]),
            "avg_price": _grab(block, r"\[AVG\]\s*(.+?)(?:\n\[|\Z)"),
            "target_price": _grab(block, r"\[TARGET\]\s*(.+?)(?:\n\[|\Z)"),
            "stop_price": _grab(block, r"\[STOP\]\s*(.+?)(?:\n\[|\Z)"),
            "deadline": _grab(block, r"\[DEADLINE\]\s*(.+?)(?:\n\[|\Z)"),
            "action_line": _grab(block, r"\[ACTION\]\s*(.+?)(?:\n\[|\Z)", "행동 지침 파싱 실패 — 상세 리포트 참고"),
        })
    for ticker in tickers:
        name = str(ticker or "").upper().strip()
        if name and name not in found:
            cards.append({
                "ticker": name,
                "verdict_code": AUDIT_VERDICT_HOLD,
                "verdict_label": AUDIT_VERDICT_LABEL[AUDIT_VERDICT_HOLD],
                "avg_price": "정보 없음",
                "target_price": "정보 없음",
                "stop_price": "정보 없음",
                "deadline": "정보 없음",
                "action_line": "CIO가 이 종목에 대한 판정을 생성하지 못했다. 상세 리포트를 보라.",
            })
    return cards


def _audit_master(report1: str, report2: str, report3: str, portfolio_data: Any) -> dict[str, Any]:
    """보유 종목별 HOLD/TRIM/EXIT 카드와 상세 원문을 같이 돌려준다."""
    tickers = _holding_tickers(portfolio_data)
    ticker_str = ", ".join(tickers) if tickers else "보유 종목 없음"
    master_prompt = f"""당신은 총괄 팀장(CIO)입니다. 아래 3인 실무진 보고서를 종합해
현재 보유 중인 종목({ticker_str}) 각각에 대해 행동 판정을 내리십시오.

전략 기준:
- PEAD: 드리프트가 살아 있으면 HOLD. 식었으면 TRIM 또는 EXIT.
- RUNUP: D-2/D-1이면 수익률과 관계없이 EXIT. 여유 있으면 HOLD.
- RSI2: +2~4% 또는 5일선 재돌파 또는 4영업일 임박을 본다. 반등이 없으면 TRIM 또는 EXIT.
- 그 외 전략은 표기를 바꾸지 말고 HOLD로 두되 행동 줄에 LEGACY라고 쓴다.

[실무진 보고서]
- 펀더멘털: {_trim(report1, _AGENT_REPORT_TRIM_CHARS)}
- 수급: {_trim(report2, _AGENT_REPORT_TRIM_CHARS)}
- 리스크: {_trim(report3, _AGENT_REPORT_TRIM_CHARS)}
[보유]: {_holding_brief(portfolio_data)}

[절대 규칙]
1. 공백 채우기 문구를 쓰지 마라. 근거가 약하면 HOLD를 내리고 사유에 "정보 제한적"이라고만 써라.
2. 각 종목마다 아래 블록을 종목 수만큼 반복하라. 보유가 없으면 블록 없이 "보유 종목 없음"만 써라.
3. VERDICT는 HOLD, TRIM, EXIT 중 하나만 써라.
4. 없는 숫자는 "정보 없음"이라고 써라.
5. ACTION은 무엇을 할지만 한 줄로 써라.

[출력 포맷 — 종목당 1개 블록, 마커 그대로 유지]
[POSITION]
[TICKER] (종목코드)
[VERDICT] HOLD 또는 TRIM 또는 EXIT 중 하나만
[AVG] (평단가, 예: USD 123.45)
[TARGET] (목표 익절가, 예: USD 140.00 (+13.4%))
[STOP] (손절선, 예: USD 118.50 (-4.0%))
[DEADLINE] (홀딩 기한, 예: 2026-10-20 (D-3) 또는 "기한 없음")
[ACTION] (행동 지침 1줄)
[/POSITION]
[상세]
(종목 사이 교차와 포트폴리오 총평. 상세 칸에만 나온다.)
"""
    try:
        full_report = _cio_chat(
            system_prompt="당신은 엄격하고 단호한 CIO입니다. 공백 문구로 칸을 채우지 않습니다.",
            user_prompt=master_prompt,
            max_tokens=1600,
        )
    except Exception as exc:  # noqa: BLE001
        full_report = f"CIO 호출 실패: {_rate_limit_note(exc)}"
    full_report = _sanitize_dollar(full_report)
    return {"full_report": full_report, "position_cards": _parse_audit_positions(full_report, tickers)}


def run_portfolio_audit(
    regime_data: Any,
    portfolio_data: Any,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """파이프라인 A: 보유 종목별 HOLD/TRIM/EXIT 카드."""

    def _p(msg: str) -> None:
        if on_progress:
            try:
                on_progress(msg)
            except Exception:  # noqa: BLE001
                pass

    errors: list[str] = []
    halted = False

    def _stop(exc: Exception) -> bool:
        nonlocal halted
        if "429" in str(exc) or "rate_limit" in str(exc):
            halted = True
        return halted

    _p("1/4 — 펀더멘털/어닝 분석 중...")
    try:
        r1 = _audit_agent1_fundamental(portfolio_data)
        _p("1/4 — 펀더멘털/어닝 완료 ✅")
    except Exception as exc:  # noqa: BLE001
        r1 = "(1/4 실패) " + _rate_limit_note(exc)
        errors.append(r1)
        _stop(exc)
        _p(f"1/4 — 실패 ❌ ({exc})")
    _p("2/4 — 수급/차트 분석 중...")
    if halted:
        r2 = "(2/4 생략) 일일 한도로 이후 호출을 멈췄다."
    else:
        try:
            r2 = _audit_agent2_flow(portfolio_data)
            _p("2/4 — 수급/차트 완료 ✅")
        except Exception as exc:  # noqa: BLE001
            r2 = "(2/4 실패) " + _rate_limit_note(exc)
            errors.append(r2)
            _stop(exc)
            _p(f"2/4 — 실패 ❌ ({exc})")
    _p("3/4 — 리스크/거시 분석 중...")
    if halted:
        r3 = "(3/4 생략) 일일 한도로 이후 호출을 멈췄다."
    else:
        try:
            r3 = _audit_agent3_risk(regime_data, portfolio_data)
            _p("3/4 — 리스크/거시 완료 ✅")
        except Exception as exc:  # noqa: BLE001
            r3 = "(3/4 실패) " + _rate_limit_note(exc)
            errors.append(r3)
            _stop(exc)
            _p(f"3/4 — 실패 ❌ ({exc})")
    _p("4/4 — 마스터 CIO 최종 판결 작성 중...")
    master_result = {
        "full_report": " / ".join(errors) or "판결 없음",
        "position_cards": _parse_audit_positions("", _holding_tickers(portfolio_data)),
    }
    if halted:
        _p("4/4 — 생략")
    else:
        try:
            master_result = _audit_master(r1, r2, r3, portfolio_data)
            _p("4/4 — 완료 ✅")
        except Exception as exc:  # noqa: BLE001
            errors.append(_rate_limit_note(exc))
            _p(f"4/4 — 실패 ❌ ({_rate_limit_note(exc)})")
            master_result = {
                "full_report": _rate_limit_note(exc),
                "position_cards": _parse_audit_positions("", _holding_tickers(portfolio_data)),
            }
    return {
        "full_report": str(master_result.get("full_report") or ""),
        "position_cards": list(master_result.get("position_cards") or []),
        "staff_reports": {"fundamental": r1, "flow": r2, "risk": r3},
        "errors": errors,
    }


def _hunt_agent_pead(pead_data: Any) -> str:
    prompt = f"""당신은 PEAD(실적 서프라이즈 드리프트) 전문가입니다. 아래 후보 중 EPS 서프라이즈
폭, 반응일 갭업 크기, 진입 타이밍(D+1~D+2)을 종합해 최고 후보 1종목을 선정하고 핵심 수치를
추출하십시오. 후보가 비어 있으면 "PEAD 후보 없음"이라고만 쓰십시오.
current_price, target_price, stop_price, upside_pct가 있으면 보고서 첫 줄에 그 숫자를 그대로 적으십시오.
그 숫자가 없으면 그 항목만 "확인 필요"라고 쓰십시오. 데이터에 없는 수치는 절대 지어내지 마십시오.

[PEAD Top 후보]: {_j(pead_data)}"""
    return _groq_chat(system_prompt="당신은 PEAD 전문가입니다.", user_prompt=prompt, max_tokens=700)


def _hunt_agent_runup(runup_data: Any) -> str:
    prompt = f"""당신은 실적 런업(D-7~D-3) 전문가입니다. 아래 후보 중 점수, 애널리스트
매수비율, 목표가 상승여력을 종합해 최고 후보 1종목을 선정하십시오. 진입 후 D-2/D-1에 반드시
탈출해야 하는 초단기 전략임을 감안해 선정하십시오. 후보가 비어 있으면 "런업 후보 없음"이라고만 쓰십시오.
current_price, target_price, stop_price, upside_pct가 있으면 보고서 첫 줄에 그 숫자를 그대로 적으십시오.
그 숫자가 없으면 그 항목만 "확인 필요"라고 쓰십시오. 데이터에 없는 수치는 절대 지어내지 마십시오.

[런업 Top 후보]: {_j(runup_data)}"""
    return _groq_chat(system_prompt="당신은 실적 런업 전문가입니다.", user_prompt=prompt, max_tokens=700)


def _hunt_agent_rsi2(rsi2_data: Any) -> str:
    prompt = f"""당신은 RSI(2) 과매도 반등 전문가입니다. 아래 후보 중 RSI2 수치가 가장 극단적이고
(낮을수록 좋음) 반등 신뢰도가 높은 1종목을 선정하십시오. 데이터에 없는 수치는 절대 지어내지 마십시오.
아래 목록에 종목이 1개 이상 있다면 반드시 그중에서 1종목을 선정해야 합니다 — "후보 없음"이라고
답하는 것은 목록이 완전히 비어있을 때만 허용됩니다.
current_price, target_price, stop_price, upside_pct, market_cap_b, drop_3d_pct, dist_sma200_pct가
있으면 보고서 첫 줄에 그 숫자를 그대로 적으십시오.

[RSI2 Top 후보]: {_j(rsi2_data)}"""
    return _groq_chat(system_prompt="당신은 RSI2 전문가입니다.", user_prompt=prompt, max_tokens=700)


def _hunt_agent_risk_cooldown(
    regime_data: Any,
    recently_closed_today: list | None,
    portfolio_data: Any,
) -> str:
    closed_list = [str(item) for item in (recently_closed_today or []) if item]
    closed_note = (
        f"오늘 이미 청산된 종목: {', '.join(closed_list)} — 이 종목들은 신규 추천에서 강제 배제할 것."
        if closed_list
        else "오늘 청산된 종목 없음."
    )
    prompt = f"""당신은 리스크 & 회전문(쿨다운) 심사관입니다. 당신의 유일한 임무는:
1. 현재 시장 국면을 한 줄로 요약하고 오늘 밤 공격적/보수적 기조를 제시한다.
2. 회전문 체크에 걸린 종목이 있으면 그 티커만 명시해 "제외 대상"이라고 알린다.
이 단계는 신규 추천을 막을 권한이 없습니다. "진입 불가"나 "슬롯 소진" 같은 판단은 절대
내리지 마십시오.

[시장 국면]: {_j(regime_data)}
[회전문 체크]: {closed_note}
[현재 보유 포트폴리오 (참고용)]: {_j(portfolio_data)}"""
    return _groq_chat(
        system_prompt="당신은 리스크 & 회전문 심사관입니다. 추천을 막을 권한이 없습니다.",
        user_prompt=prompt,
        max_tokens=700,
    )


def _hunt_master(report_pead: str, report_runup: str, report_rsi2: str, report_risk: str) -> str:
    prompt = f"""당신은 마스터 CIO(최종 결정권자)입니다. 아래 3개 전략 전문가 보고서와 리스크
보고서를 바탕으로, 오늘 밤 사격할 **Top 3 순위**를 확정하십시오.

중요 규칙 (절대 위반 금지):
- "신규 진입 불가", "슬롯 소진", "추천 보류" 문구는 절대 쓰지 마십시오.
- PEAD/런업/RSI2 전문가가 각각 제시한 후보 중에서 기대값이 높은 순서로 반드시 3개를
  순위 매기십시오. 회전문 체크에서 제외 대상으로 지목된 티커만 건너뛰고 차순위로 대체하십시오.
- 아래에 주어지는 각 전문가 보고서는 이미 실제 후보 데이터를 기반으로 작성된 것입니다.
  "후보가 없다", "데이터가 부족하다", "가격 데이터 없음" 같은 말을 쓰는 것은 금지되어
  있습니다. 보고서에 특정 수치가 안 보이면 그 항목만 "확인 필요"라고 쓰고, 선정 자체는
  반드시 수행하십시오.

[PEAD 전문가 보고서]: {_trim(report_pead)}

[런업 전문가 보고서]: {_trim(report_runup)}

[RSI2 전문가 보고서]: {_trim(report_rsi2)}

[리스크 & 회전문 보고서]: {_trim(report_risk)}

각 후보의 current_price/target_price/stop_price/upside_pct는 파이썬이 미리 계산해
전문가 보고서에 들어 있다. 그 숫자를 그대로 인용하라. 새로 계산하지 마라.
보고서에 그 숫자가 없으면 그 항목만 "확인 필요"라고 써라.

[출력 포맷 — 반드시 1위~3위 전부 작성]
1위: [티커] (전략: PEAD/RUNUP/RSI2) — 선정사유 1줄
   목표가: USD XX (+XX%) / 권장 홀딩 기한 / 22:45 이후 진입 타점 및 손절가

2위: [티커] (전략: ...) — 선정사유 1줄
   목표가: USD XX (+XX%) / 권장 홀딩 기한 / 22:45 이후 진입 타점 및 손절가

3위: [티커] (전략: ...) — 선정사유 1줄
   목표가: USD XX (+XX%) / 권장 홀딩 기한 / 22:45 이후 진입 타점 및 손절가

위 보고서에 없는 수치는 절대 지어내지 마십시오. 특정 전략에 후보가 아예 없으면(예: RSI2가
시장 국면상 휴지 상태) 그 사실만 인정하고 나머지 전략들로 3개를 채우십시오. 후보 풀 자체가
3개 미만이면 있는 만큼만 순위를 매기고, 빈 자리는 "후보 부족"이라고 쓰십시오."""
    return _groq_chat(
        system_prompt="당신은 마스터 CIO입니다. 반드시 Top3를 순위와 함께 제시합니다.",
        user_prompt=prompt,
        max_tokens=1800,
    )


def run_new_hunt(
    regime_data: Any,
    portfolio_data: Any,
    pead_data: Any,
    runup_data: Any,
    rsi2_data: Any,
    recently_closed_today: list | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """파이프라인 B: 3대 전략 Top3 발굴. 5단계(전문가 3 + 리스크 1 + 마스터 1)."""

    def _p(msg: str) -> None:
        if on_progress:
            try:
                on_progress(msg)
            except Exception:  # noqa: BLE001
                pass

    errors: list[str] = []
    halted = False

    def _stop(exc: Exception) -> None:
        nonlocal halted
        if "429" in str(exc) or "rate_limit" in str(exc):
            halted = True

    def _fail(step: str, exc: Exception) -> str:
        note = _rate_limit_note(exc)
        _stop(exc)
        errors.append(f"({step} 실패) {note}")
        _p(f"{step} — 실패 ❌ ({note})")
        return f"({step} 실패) {note}"

    _p("1/5 — PEAD 전문가 분석 중...")
    try:
        r_pead = _hunt_agent_pead(pead_data)
        _p("1/5 — PEAD 완료 ✅")
    except Exception as exc:  # noqa: BLE001
        r_pead = _fail("1/5", exc)
    _p("2/5 — 런업 전문가 분석 중...")
    if halted:
        r_runup = "(2/5 생략) 일일 토큰을 전부 써서 이후 호출을 멈췄다."
    else:
        try:
            r_runup = _hunt_agent_runup(runup_data)
            _p("2/5 — 런업 완료 ✅")
        except Exception as exc:  # noqa: BLE001
            r_runup = _fail("2/5", exc)
    _p("3/5 — RSI2 전문가 분석 중...")
    if halted:
        r_rsi2 = "(3/5 생략) 일일 토큰을 전부 써서 이후 호출을 멈췄다."
    else:
        try:
            r_rsi2 = _hunt_agent_rsi2(rsi2_data)
            _p("3/5 — RSI2 완료 ✅")
        except Exception as exc:  # noqa: BLE001
            r_rsi2 = _fail("3/5", exc)
    _p("4/5 — 리스크 & 쿨다운 심사 중...")
    if halted:
        r_risk = "(4/5 생략) 일일 토큰을 전부 써서 이후 호출을 멈췄다."
    else:
        try:
            r_risk = _hunt_agent_risk_cooldown(regime_data, recently_closed_today, portfolio_data)
            _p("4/5 — 리스크 & 쿨다운 완료 ✅")
        except Exception as exc:  # noqa: BLE001
            r_risk = _fail("4/5", exc)
    _p("5/5 — 마스터 CIO Top3 확정 중...")
    if halted:
        _p("5/5 — 생략")
        final_report = "Groq 일일 토큰 20만을 전부 썼다. 발굴 나머지 단계를 호출하지 않는다."
    else:
        try:
            final_report = _hunt_master(r_pead, r_runup, r_rsi2, r_risk)
            _p("5/5 — 완료 ✅")
        except Exception as exc:  # noqa: BLE001
            _fail("5/5", exc)
            final_report = (
                "⚠️ 마스터 확정 실패 — 개별 보고서만 표시합니다.\n\n"
                f"[PEAD]\n{r_pead}\n\n[런업]\n{r_runup}\n\n[RSI2]\n{r_rsi2}\n\n[리스크]\n{r_risk}"
            )
    if halted:
        return _clean_model_text(errors[0] if errors else final_report)
    if errors:
        final_report = "⚠️ " + " / ".join(errors) + "\n\n" + final_report
    return _clean_model_text(final_report)


def _staff_macro(ticker_data: dict, regime_data: Any) -> str:
    prompt = f"""당신은 매크로 실무관입니다. SPY 시장 국면과 섹터 흐름을 종합해 이 종목의
거시 환경을 짧게 평가하십시오. 데이터에 없는 수치는 지어내지 마십시오.

[시장 국면]: {_j(regime_data)}
[티커 데이터]: {_j(ticker_data)}"""
    return _groq_chat(system_prompt="당신은 매크로 실무관입니다.", user_prompt=prompt, max_tokens=500)


def _staff_fundamental(ticker_data: dict) -> str:
    prompt = f"""당신은 펀더멘털 실무관입니다. 실적 D-Day, 서프라이즈 이력, 월가 컨센서스
목표가를 짧게 평가하십시오. 데이터에 없는 수치는 지어내지 마십시오.

[티커 데이터]: {_j(ticker_data)}"""
    return _groq_chat(system_prompt="당신은 펀더멘털 실무관입니다.", user_prompt=prompt, max_tokens=500)


def _staff_technical(ticker_data: dict) -> str:
    prompt = f"""당신은 테크니컬 실무관입니다. 22:45 KST 이후 진입 지지선, 200일선 이격도, 변동성을
짧게 평가하십시오. 데이터에 없는 수치는 지어내지 마십시오.

[티커 데이터]: {_j(ticker_data)}"""
    return _groq_chat(system_prompt="당신은 테크니컬 실무관입니다.", user_prompt=prompt, max_tokens=500)


def _staff_risk(ticker_data: dict) -> str:
    prompt = f"""당신은 리스크 매니저(악마의 대변인)입니다. 이 종목에 투자하면 안 되는
이유를 최대한 찾으십시오. 거래량 부족, 갭다운, 하방 손실을 짧게 경고하십시오.
데이터에 없는 수치는 지어내지 마십시오.

[티커 데이터]: {_j(ticker_data)}"""
    return _groq_chat(system_prompt="당신은 리스크 매니저(악마의 대변인)입니다.", user_prompt=prompt, max_tokens=500)


def _parse_cio_sections(raw_text: str) -> dict[str, str]:
    """마커가 빠져도 기본값으로 돌아온다. 화면은 이 함수 때문에 죽지 않는다."""
    text = raw_text or ""

    def _grab(pattern: str, default: str = "") -> str:
        found = re.search(pattern, text, re.DOTALL)
        return found.group(1).strip() if found else default

    verdict_token = _grab(r"\[VERDICT\]\s*([A-Z_]+)", "").strip()
    verdict_code = _VERDICT_TOKEN_MAP.get(verdict_token, "")
    if not verdict_code:
        if "당장 진입" in text or "🟢" in text:
            verdict_code = VERDICT_ENTER_NOW
        elif "조건부 대기" in text or "🟡" in text:
            verdict_code = VERDICT_WAIT
        elif "진입 금지" in text or "패스" in text or "🔴" in text:
            verdict_code = VERDICT_NO_ENTRY
        else:
            verdict_code = VERDICT_WAIT
    return {
        "verdict_code": verdict_code,
        "verdict_label": VERDICT_LABEL.get(verdict_code, VERDICT_LABEL[VERDICT_WAIT]),
        "reason_line": _grab(r"\[REASON\]\s*(.+?)(?:\n\[|\Z)", "사유 파싱 실패 — 하단 상세 리포트 참고"),
        "entry_range": _grab(r"\[ENTRY\]\s*(.+?)(?:\n\[|\Z)", "USD 정보 없음"),
        "target_price": _grab(r"\[TARGET\]\s*(.+?)(?:\n\[|\Z)", "USD 정보 없음"),
        "stop_price": _grab(r"\[STOP\]\s*(.+?)(?:\n\[|\Z)", "USD 정보 없음"),
        "position_size": _grab(r"\[SIZE\]\s*(.+?)(?:\n\[|\Z)", "정보 없음"),
        "expected_pnl": _grab(r"\[PNL\]\s*(.+?)(?:\n\[|\Z)", "정보 없음"),
    }


def run_ticker_review(
    ticker_data: dict,
    regime_data: Any,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """단일 티커. 상단 판정 필드와 하단 전문을 나눠 돌려준다."""
    ticker = str((ticker_data or {}).get("ticker") or "UNKNOWN")
    errors: list[str] = []

    def _progress(msg: str) -> None:
        if on_progress:
            try:
                on_progress(msg)
            except Exception:  # noqa: BLE001
                pass

    staff_reports: dict[str, str] = {}
    staff_fns = {
        "macro": lambda: _staff_macro(ticker_data, regime_data),
        "fundamental": lambda: _staff_fundamental(ticker_data),
        "technical": lambda: _staff_technical(ticker_data),
        "risk": lambda: _staff_risk(ticker_data),
    }
    _progress(f"1/5 — {ticker} 4인 실무진 소집 중...")
    futures: dict[Any, str] = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fn): name for name, fn in staff_fns.items()}
            try:
                for fut in concurrent.futures.as_completed(futures, timeout=_AGENT_TIMEOUT_SEC * 2):
                    name = futures[fut]
                    try:
                        staff_reports[name] = fut.result(timeout=_AGENT_TIMEOUT_SEC)
                    except Exception as exc:  # noqa: BLE001
                        note = _rate_limit_note(exc)
                        staff_reports[name] = f"({name} 분석 실패: {note})"
                        errors.append(f"{name} 단계 실패: {note}")
            except concurrent.futures.TimeoutError:
                errors.append("실무진 응답 시간 초과")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"실무진 소집 실패: {_rate_limit_note(exc)}")
    for name in staff_fns:
        staff_reports.setdefault(name, "(응답 없음)")
    _progress("2/5 — 실무진 보고서 취합 완료, CIO 심의 준비 중...")

    macro_r = _trim(staff_reports.get("macro", ""), _AGENT_REPORT_TRIM_CHARS)
    fund_r = _trim(staff_reports.get("fundamental", ""), _AGENT_REPORT_TRIM_CHARS)
    tech_r = _trim(staff_reports.get("technical", ""), _AGENT_REPORT_TRIM_CHARS)
    risk_r = _trim(staff_reports.get("risk", ""), _AGENT_REPORT_TRIM_CHARS)

    _progress(f"3/5 — CIO({_DEEPSEEK_MODEL}) 최종 심의 중...")
    cio_prompt = f"""당신은 총괄 팀장(CIO)입니다. 아래 4인 실무진 보고서를 종합해
"{ticker}" 티커에 대한 최종 행동 판정을 내리십시오.

[실무진 보고서]
- 매크로: {macro_r}
- 펀더멘털: {fund_r}
- 테크니컬: {tech_r}
- 리스크: {risk_r}

[절대 규칙 — 반드시 준수]
1. 최종 판결은 반드시 다음 3개 코드 토큰 중 정확히 하나만 사용하십시오. 다른 단어는 쓰지 마십시오.
   - ENTER_NOW : 지금 당장(22:45 KST 이후 타점 도달 시) 지정가로 진입
   - WAIT_CONDITIONAL : 지지선이나 수급을 더 보고 분할 매수만 허용
   - NO_ENTRY : 오늘 밤은 사지 않음
2. 데이터에 없는 숫자는 절대 지어내지 마십시오. 모르면 "정보 없음"이라고 쓰십시오.
3. 사유는 한 줄로, 왜 지금 사는지 또는 왜 기다리거나 넘기는지를 단호하게 쓰십시오.
4. 아래 마커는 한 줄에 하나씩, 순서를 유지하십시오.

[출력 포맷 — 반드시 이 구조 그대로, 대괄호 마커 유지]
[VERDICT] ENTER_NOW 또는 WAIT_CONDITIONAL 또는 NO_ENTRY 중 하나만
[REASON] (판결 사유 단 1줄, 단호하게)
[ENTRY] (22:45 KST 이후 지정가 진입 타점 범위, 예: USD 24.10 ~ USD 24.50)
[TARGET] (목표 익절가, 예: USD 27.30 (+12.5%))
[STOP] (강제 손절가, 예: USD 23.00 (-4.5%))
[SIZE] (권장 투입 비중, 예: 120만원 / PEAD 슬롯 전액)
[PNL] (예상 손익, 예: 성공 시 +15만원 / 실패 시 -5만원)

[상세]
(여기부터는 4인 실무진을 교차한 근거를 쓴다. 이 부분은 상세 아코디언에만 나온다.)
"""
    try:
        cio_report_raw = _cio_chat(
            system_prompt="당신은 엄격하고 단호한 CIO입니다. 모호한 보고서체를 쓰지 않습니다.",
            user_prompt=cio_prompt,
            max_tokens=1200,
        )
    except Exception as exc:  # noqa: BLE001
        note = _rate_limit_note(exc)
        cio_report_raw = (
            "[VERDICT] WAIT_CONDITIONAL\n"
            f"[REASON] CIO 호출 실패로 자동 대기 판정: {note}\n"
            "[ENTRY] 정보 없음\n[TARGET] 정보 없음\n[STOP] 정보 없음\n"
            "[SIZE] 정보 없음\n[PNL] 정보 없음\n\n[상세]\nCIO 호출이 실패했다."
        )
        errors.append(f"CIO 호출 실패: {note}")

    cio_report_raw = _sanitize_dollar(cio_report_raw)
    _progress("4/5 — CIO 판결 파싱 중...")
    parsed = _parse_cio_sections(cio_report_raw)
    staff_4lines = {
        "macro": _trim(staff_reports.get("macro", "정보 없음"), 120).replace("\n", " "),
        "fundamental": _trim(staff_reports.get("fundamental", "정보 없음"), 120).replace("\n", " "),
        "technical": _trim(staff_reports.get("technical", "정보 없음"), 120).replace("\n", " "),
        "risk": _trim(staff_reports.get("risk", "정보 없음"), 120).replace("\n", " "),
    }
    order_copy_text = (
        f"[{ticker}] {parsed['verdict_label']}\n"
        f"진입 타점: {parsed['entry_range']}\n"
        f"목표가: {parsed['target_price']}\n"
        f"손절가: {parsed['stop_price']}\n"
        f"투입 비중: {parsed['position_size']}\n"
        f"예상 손익: {parsed['expected_pnl']}\n"
        f"사유: {parsed['reason_line']}"
    )
    _progress("5/5 — 심의 완료")
    return {
        "ticker": ticker,
        "verdict_code": parsed["verdict_code"],
        "verdict_label": parsed["verdict_label"],
        "reason_line": parsed["reason_line"],
        "entry_range": parsed["entry_range"],
        "target_price": parsed["target_price"],
        "stop_price": parsed["stop_price"],
        "position_size": parsed["position_size"],
        "expected_pnl": parsed["expected_pnl"],
        "staff_4lines": staff_4lines,
        "order_copy_text": order_copy_text,
        "cio_report_full": cio_report_raw,
        "staff_reports": staff_reports,
        "errors": errors,
    }
