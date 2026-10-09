"""Groq 기반 AI 전술 위원회 — 2개의 독립 파이프라인.

완전 독립 모듈. 스캐너/포트폴리오 파이프라인을 import 하지 않는다.

v5.0: 단일 파이프라인을 폐기하고 완전히 분리된 2개 파이프라인으로 재설계했다.
  - run_portfolio_audit(...): 보유 종목 생사 판결 전용 (4단계: 펀더멘털/수급/리스크/마스터CIO)
  - run_new_hunt(...)       : 신규 1픽 발굴 전용 (4단계: 런업전문가/스퀴즈전문가/리스크&쿨다운/마스터CIO)
각 파이프라인은 토큰 예산을 서로 침범하지 않으며, 각자 독립적으로 실패/성공한다.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

try:
    from groq import Groq
except ImportError:
    Groq = None  # type: ignore[misc, assignment]

_GROQ_MODEL = "openai/gpt-oss-120b"
_AGENT_TIMEOUT_SEC = 20
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
    if getattr(choice, "finish_reason", None) == "length":
        text += "\n\n(응답이 출력 한도에서 끊겼습니다.)"
    return _sanitize_dollar(text)


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


def _audit_agent1_fundamental(portfolio_data: Any) -> str:
    prompt = f"""당신은 펀더멘털/어닝 분석가입니다. 아래 보유 종목들의 실적 D-Day, 컨센서스 목표가,
서프라이즈 가능성을 티커별로 짧고 날카롭게 분석하십시오. 데이터에 없는 수치는 절대 지어내지 마십시오.

[보유 포트폴리오]: {_j(portfolio_data)}"""
    return _groq_chat(
        system_prompt="당신은 펀더멘털/어닝 분석가입니다. 데이터에 없는 수치는 지어내지 마십시오.",
        user_prompt=prompt,
        max_tokens=800,
    )


def _audit_agent2_flow(portfolio_data: Any) -> str:
    prompt = f"""당신은 수급/차트 분석가입니다. 아래 보유 종목들의 단기 수급, 숏스퀴즈 화력,
ATR 대비 장중 노이즈 및 지지선을 티커별로 짧고 날카롭게 분석하십시오. 데이터에 없는 수치는
절대 지어내지 마십시오.

[보유 포트폴리오]: {_j(portfolio_data)}"""
    return _groq_chat(
        system_prompt="당신은 수급/차트 분석가입니다.",
        user_prompt=prompt,
        max_tokens=800,
    )


def _audit_agent3_risk(regime_data: Any, portfolio_data: Any) -> str:
    prompt = f"""당신은 리스크/거시 분석가입니다. 현재 시장 국면(SPY 분산일 여부 포함)과 보유
포트폴리오를 보고 섹터 악재, 주말 오버위크 갭 리스크를 티커별로 짧고 날카롭게 짚으십시오.
데이터에 없는 수치는 절대 지어내지 마십시오.

[시장 국면]: {_j(regime_data)}
[보유 포트폴리오]: {_j(portfolio_data)}"""
    return _groq_chat(
        system_prompt="당신은 리스크/거시 분석가입니다.",
        user_prompt=prompt,
        max_tokens=800,
    )


def _audit_master(report1: str, report2: str, report3: str, portfolio_data: Any) -> str:
    prompt = f"""당신은 마스터 CIO(최종 결정권자)입니다. 아래 3개 전문가 보고서만 보고,
보유 중인 전 종목에 대해 반드시 아래 포맷 그대로 최종 판결을 내리십시오. 설교 금지, 행동 지침만.

[펀더멘털/어닝 보고서]: {_trim(report1)}
[수급/차트 보고서]: {_trim(report2)}
[리스크/거시 보고서]: {_trim(report3)}
[참고용 보유 티커]: {_ticker_list(portfolio_data)}

[출력 포맷 — 티커별로 반드시 이 형식 반복]
보유 종목이 없으면 "보유 종목 없음 — 슬롯 100% 가용"이라고만 쓰십시오.
- [티커]: 최종 판결: HOLD / TRIM / EXIT
  목표가: USD XX (+XX%) / 손절·무효화 기준: USD XX / 권장 홀딩 기한: (D-Day 기준 구체적 날짜)
  (판결 근거 1줄)

위 보고서에 없는 수치는 절대 지어내지 마십시오. 값이 비어 있으면 그 사실을 그대로 인정하십시오."""
    return _groq_chat(
        system_prompt="당신은 마스터 CIO(최종 결정권자)입니다.",
        user_prompt=prompt,
        max_tokens=1500,
    )


def run_portfolio_audit(
    regime_data: Any,
    portfolio_data: Any,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """파이프라인 A: 보유 종목 생사 판결 전용."""

    def _p(msg: str) -> None:
        if on_progress:
            try:
                on_progress(msg)
            except Exception:  # noqa: BLE001
                pass

    errors: list[str] = []
    _p("1/4 — 펀더멘털/어닝 분석 중...")
    try:
        r1 = _audit_agent1_fundamental(portfolio_data)
        _p("1/4 — 펀더멘털/어닝 완료 ✅")
    except Exception as exc:  # noqa: BLE001
        r1 = "(1/4 실패)"
        errors.append(f"1/4 실패: {exc}")
        _p(f"1/4 — 실패 ❌ ({exc})")
    _p("2/4 — 수급/차트 분석 중...")
    try:
        r2 = _audit_agent2_flow(portfolio_data)
        _p("2/4 — 수급/차트 완료 ✅")
    except Exception as exc:  # noqa: BLE001
        r2 = "(2/4 실패)"
        errors.append(f"2/4 실패: {exc}")
        _p(f"2/4 — 실패 ❌ ({exc})")
    _p("3/4 — 리스크/거시 분석 중...")
    try:
        r3 = _audit_agent3_risk(regime_data, portfolio_data)
        _p("3/4 — 리스크/거시 완료 ✅")
    except Exception as exc:  # noqa: BLE001
        r3 = "(3/4 실패)"
        errors.append(f"3/4 실패: {exc}")
        _p(f"3/4 — 실패 ❌ ({exc})")
    _p("4/4 — 마스터 CIO 최종 판결 작성 중...")
    try:
        final_report = _audit_master(r1, r2, r3, portfolio_data)
        _p("4/4 — 완료 ✅")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"4/4 실패: {exc}")
        _p(f"4/4 — 실패 ❌ ({exc})")
        final_report = (
            "⚠️ 마스터 판결 실패 — 개별 보고서만 표시합니다.\n\n"
            f"[1/4]\n{r1}\n\n[2/4]\n{r2}\n\n[3/4]\n{r3}"
        )
    if errors:
        final_report = "⚠️ " + " / ".join(errors) + "\n\n" + final_report
    return _sanitize_dollar(final_report)


def _hunt_agent1_runup(runup_data: Any) -> str:
    prompt = f"""당신은 실적 런업 전문가입니다. 아래 Top 10 후보 중 D-12~D-7 골든 윈도우,
SMA50 상회, 월가 목표가 상승여력을 종합해 최고 후보 1종목을 선정하고 핵심 수치를 추출하십시오.
데이터에 없는 수치는 절대 지어내지 마십시오.

[실적 런업 Top 10]: {_j(runup_data)}"""
    return _groq_chat(
        system_prompt="당신은 실적 런업 전문가입니다.",
        user_prompt=prompt,
        max_tokens=800,
    )


def _hunt_agent2_squeeze(squeeze_data: Any) -> str:
    prompt = f"""당신은 숏스퀴즈 전문가입니다. 아래 Top 10 후보 중 공매도비율 15%+, 거래량 급증,
상승여력 5%+를 종합해 "단순 1위의 함정"을 지적하고 실제 손익비가 가장 좋은 1종목을 선정하십시오.
데이터에 없는 수치는 절대 지어내지 마십시오.

[숏스퀴즈 Top 10]: {_j(squeeze_data)}"""
    return _groq_chat(
        system_prompt="당신은 숏스퀴즈 전문가입니다.",
        user_prompt=prompt,
        max_tokens=800,
    )


def _hunt_agent3_risk_cooldown(
    regime_data: Any,
    recently_closed_today: list | None,
    portfolio_data: Any,
) -> str:
    closed_list = recently_closed_today or []
    closed_note = (
        f"오늘 이미 청산된 종목: {', '.join(str(x) for x in closed_list)} — 이 종목들은 신규 추천에서 강제 배제할 것."
        if closed_list
        else "오늘 청산된 종목 없음."
    )
    prompt = f"""당신은 리스크 & 쿨다운 심사관입니다. 현재 시장 국면(NORMAL/PRESSURE/CORRECTION)과
회전문 방지 체크를 반영해, 신규 진입이 가능한 가용 슬롯(자금 한도) 상태를 점검하십시오.

[시장 국면]: {_j(regime_data)}
[회전문 체크]: {closed_note}
[현재 보유 포트폴리오 (슬롯 점검용)]: {_j(portfolio_data)}"""
    return _groq_chat(
        system_prompt="당신은 리스크 & 쿨다운 심사관입니다.",
        user_prompt=prompt,
        max_tokens=800,
    )


def _hunt_master(report1: str, report2: str, report3: str) -> str:
    prompt = f"""당신은 마스터 CIO(최종 결정권자)입니다. 아래 3개 전문가 보고서만 보고,
런업 대장주 vs 스퀴즈 대장주 중 오늘 밤 사격할 최종 1픽(가용 슬롯이 여러 개면 슬롯별 각 1픽)을
확정하십시오. 설교 금지, 행동 지침만.

[실적 런업 전문가 보고서]: {_trim(report1)}
[숏스퀴즈 전문가 보고서]: {_trim(report2)}
[리스크 & 쿨다운 보고서]: {_trim(report3)}

[출력 포맷 — 반드시 이 형식 그대로, 선정된 종목마다 반복]
- 선정 티커 및 전략 유형: (실적 런업 120만원 슬롯 / 숏스퀴즈 60만원 하프슬롯)
  목표가: USD XX (+XX% 기대수익률)
  권장 홀딩 기한: (구체적 날짜)
  22:45 이후 눌림목 진입가 및 무효화(손절) 조건: USD XX

쿨다운 체크에서 배제 대상이면 반드시 제외하고 차순위를 선정하십시오. 가용 슬롯이 전혀 없으면
"신규 진입 불가 — 슬롯 소진"이라고만 쓰십시오. 위 보고서에 없는 수치는 절대 지어내지 마십시오."""
    return _groq_chat(
        system_prompt="당신은 마스터 CIO(최종 결정권자)입니다.",
        user_prompt=prompt,
        max_tokens=1500,
    )


def run_new_hunt(
    regime_data: Any,
    portfolio_data: Any,
    runup_data: Any,
    squeeze_data: Any,
    recently_closed_today: list | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """파이프라인 B: 신규 1픽 발굴 전용."""

    def _p(msg: str) -> None:
        if on_progress:
            try:
                on_progress(msg)
            except Exception:  # noqa: BLE001
                pass

    errors: list[str] = []
    _p("1/4 — 실적 런업 전문가 분석 중...")
    try:
        r1 = _hunt_agent1_runup(runup_data)
        _p("1/4 — 실적 런업 완료 ✅")
    except Exception as exc:  # noqa: BLE001
        r1 = "(1/4 실패)"
        errors.append(f"1/4 실패: {exc}")
        _p(f"1/4 — 실패 ❌ ({exc})")
    _p("2/4 — 숏스퀴즈 전문가 분석 중...")
    try:
        r2 = _hunt_agent2_squeeze(squeeze_data)
        _p("2/4 — 숏스퀴즈 완료 ✅")
    except Exception as exc:  # noqa: BLE001
        r2 = "(2/4 실패)"
        errors.append(f"2/4 실패: {exc}")
        _p(f"2/4 — 실패 ❌ ({exc})")
    _p("3/4 — 리스크 & 쿨다운 심사 중...")
    try:
        r3 = _hunt_agent3_risk_cooldown(regime_data, recently_closed_today, portfolio_data)
        _p("3/4 — 리스크 & 쿨다운 완료 ✅")
    except Exception as exc:  # noqa: BLE001
        r3 = "(3/4 실패)"
        errors.append(f"3/4 실패: {exc}")
        _p(f"3/4 — 실패 ❌ ({exc})")
    _p("4/4 — 마스터 CIO 최종 1픽 확정 중...")
    try:
        final_report = _hunt_master(r1, r2, r3)
        _p("4/4 — 완료 ✅")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"4/4 실패: {exc}")
        _p(f"4/4 — 실패 ❌ ({exc})")
        final_report = (
            "⚠️ 마스터 확정 실패 — 개별 보고서만 표시합니다.\n\n"
            f"[1/4]\n{r1}\n\n[2/4]\n{r2}\n\n[3/4]\n{r3}"
        )
    if errors:
        final_report = "⚠️ " + " / ".join(errors) + "\n\n" + final_report
    return _sanitize_dollar(final_report)
