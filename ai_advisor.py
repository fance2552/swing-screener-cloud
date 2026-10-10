"""Groq 기반 AI 전술 위원회 — 2개의 독립 파이프라인.

완전 독립 모듈. 스캐너/포트폴리오 파이프라인을 import 하지 않는다.

v5.0: 단일 파이프라인을 폐기하고 완전히 분리된 2개 파이프라인으로 재설계했다.
  - run_portfolio_audit(...): 보유 종목 생사 판결 전용 (4단계: 펀더멘털/수급/리스크/마스터CIO)
  - run_new_hunt(...)       : 신규 Top3 발굴 (5단계: PEAD/런업/RSI2/리스크&쿨다운/마스터CIO)
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


def _audit_master(report1: str, report2: str, report3: str, portfolio_data: Any) -> str:
    prompt = f"""당신은 마스터 CIO(최종 결정권자)입니다. 아래 3개 전문가 보고서만 보고,
보유 중인 전 종목에 대해 반드시 아래 포맷 그대로 최종 판결을 내리십시오. 설교 금지,
행동 지침만. 종목마다 전략이 다르므로 판단 기준도 전략별로 다르게 적용하십시오:

- **PEAD 보유분**: 어닝 서프라이즈 이후의 드리프트(상승 추세)가 아직 살아있는가(거래량/추세 유지)
  vs 모멘텀이 식었는가를 기준으로 판단. 목표 +10~15%까지는 드리프트가 살아있으면 HOLD,
  식었으면 목표 미달이어도 TRIM/EXIT 권고.
- **RUNUP 보유분**: 실적 발표(D-Day)까지 남은 영업일을 최우선 고려. D-2/D-1 데드라인이
  임박했으면 수익률과 무관하게 EXIT 권고. 아직 여유 있으면 HOLD.
- **RSI2 보유분**: 반등 목표(+2~4%) 도달 여부 또는 5일선 재돌파 여부, 그리고 보유기한
  (최대 4영업일) 임박 여부를 기준으로 판단. 반등이 안 나오고 시간만 흐르면 TRIM/EXIT.
- 전략 필드가 PEAD/RUNUP/RSI2가 아니면 그 표기를 그대로 쓰고 LEGACY로 판결하라. 전략을 바꾸지 마라.

[펀더멘털/어닝 보고서]: {_trim(report1)}

[수급/차트 보고서]: {_trim(report2)}

[리스크/거시 보고서]: {_trim(report3)}

[참고용 보유 티커]: {_holding_brief(portfolio_data)}

[출력 포맷 — 티커별로 반드시 이 형식 반복]
보유 종목이 없으면 "보유 종목 없음 — 슬롯 100% 가용"이라고만 쓰십시오.
- [티커] (전략: PEAD/RUNUP/RSI2 중 하나): 최종 판결: HOLD / TRIM / EXIT
  목표가: USD XX (+XX%) / 손절·무효화 기준: USD XX / 권장 홀딩 기한: (구체적 날짜 또는 "D-2까지")
  (판결 근거 1줄 — 위 전략별 기준에 따라 작성)

위 보고서에 없는 수치는 절대 지어내지 마십시오. 값이 비어 있으면 그 사실을 그대로 인정하십시오."""
    return _groq_chat(
        system_prompt="당신은 마스터 CIO(최종 결정권자)입니다. 전략별로 다른 판단 기준을 적용합니다.",
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
    final_report = ""
    if halted:
        _p("4/4 — 생략")
    else:
        try:
            final_report = _audit_master(r1, r2, r3, portfolio_data)
            _p("4/4 — 완료 ✅")
        except Exception as exc:  # noqa: BLE001
            errors.append(_rate_limit_note(exc))
            _p(f"4/4 — 실패 ❌ ({exc})")
    detail = (
        "## 펀더멘털\n\n" + r1 + "\n\n"
        "## 수급\n\n" + r2 + "\n\n"
        "## 리스크\n\n" + r3
    )
    if final_report:
        head = f"## 판결\n\n{final_report}"
    else:
        head = "## 판결\n\n" + (" / ".join(errors) or "판결 없음")
    return _clean_model_text(head + "\n\n" + detail)


def _hunt_agent_pead(pead_data: Any) -> str:
    prompt = f"""당신은 PEAD(실적 서프라이즈 드리프트) 전문가입니다. 아래 후보 중 EPS 서프라이즈
폭, 반응일 갭업 크기, 진입 타이밍(D+1~D+2)을 종합해 최고 후보 1종목을 선정하고 핵심 수치를
추출하십시오. 후보가 비어 있으면 "PEAD 후보 없음"이라고만 쓰십시오. 데이터에 없는 수치는 절대 지어내지 마십시오.

[PEAD Top 후보]: {_j(pead_data)}"""
    return _groq_chat(system_prompt="당신은 PEAD 전문가입니다.", user_prompt=prompt, max_tokens=700)


def _hunt_agent_runup(runup_data: Any) -> str:
    prompt = f"""당신은 실적 런업(D-5~D-4 압축형) 전문가입니다. 아래 후보 중 점수, 애널리스트
매수비율, 목표가 상승여력을 종합해 최고 후보 1종목을 선정하십시오. 진입 후 D-2/D-1에 반드시
탈출해야 하는 초단기 전략임을 감안해 선정하십시오. 후보가 비어 있으면 "런업 후보 없음"이라고만 쓰십시오.
데이터에 없는 수치는 절대 지어내지 마십시오.

[런업 Top 후보]: {_j(runup_data)}"""
    return _groq_chat(system_prompt="당신은 실적 런업 전문가입니다.", user_prompt=prompt, max_tokens=700)


def _hunt_agent_rsi2(rsi2_data: Any) -> str:
    prompt = f"""당신은 RSI(2) 과매도 반등 전문가입니다. 아래 후보 중 RSI2 수치가 가장 극단적이고
(낮을수록 좋음) 반등 신뢰도가 높은 1종목을 선정하십시오. 후보가 비어 있으면 "RSI2 후보 없음 — 전략 휴지"
라고만 쓰십시오. 데이터에 없는 수치는 절대 지어내지 마십시오.

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

중요 규칙:
- "신규 진입 불가", "슬롯 소진", "추천 보류" 문구는 절대 쓰지 마십시오.
- PEAD/런업/RSI2 전문가가 각각 제시한 후보 중에서 기대값(목표수익률 × 성공확률 - 리스크)이
  높은 순서로 반드시 3개를 순위 매기십시오. 회전문 체크에서 제외 대상으로 지목된 티커만
  건너뛰고 차순위로 대체하십시오.

[PEAD 전문가 보고서]: {_trim(report_pead)}

[런업 전문가 보고서]: {_trim(report_runup)}

[RSI2 전문가 보고서]: {_trim(report_rsi2)}

[리스크 & 회전문 보고서]: {_trim(report_risk)}

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
