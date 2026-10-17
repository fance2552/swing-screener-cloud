"""AI 어드바이저 (v4.0): DeepSeek 우선 → Groq 자동 폴백. Gemini 영구 퇴출.

원칙
  - 가격·손절·목표가는 전부 파이썬(episodic_pivot.make_plan / portfolio.evaluate)이 계산한다.
    LLM 은 승인/보류/반려 판단과 사유만 쓴다 (숫자 환각 차단).
  - 모든 출력은 sanitize_text() 를 거쳐 달러 기호를 USD 로 치환한다 (Streamlit 수식 파싱 에러 방지).
"""
from __future__ import annotations

import json
import os
import re

import requests

PROVIDERS = (
    ("DEEPSEEK", "DEEPSEEK_API_KEY", "https://api.deepseek.com/chat/completions", "deepseek-chat"),
    ("GROQ", "GROQ_API_KEY", "https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile"),
)
_TIMEOUT = 45
_SEV = {"HOLD": 0, "TRIM": 1, "EXIT": 2}


# ----------------------------------------------------------------------------
# 유틸
# ----------------------------------------------------------------------------
def secret(name: str) -> str:
    try:
        import streamlit as st
        v = st.secrets.get(name)
        if v:
            return str(v)
    except Exception:
        pass
    return os.environ.get(name, "") or ""


def sanitize_text(x):
    """문자열/리스트/딕셔너리 전체에서 달러 기호를 USD 로 치환."""
    if isinstance(x, str):
        s = re.sub(r"\$\s*", "USD ", x)
        return re.sub(r"USD\s{2,}", "USD ", s)
    if isinstance(x, list):
        return [sanitize_text(i) for i in x]
    if isinstance(x, dict):
        return {k: sanitize_text(v) for k, v in x.items()}
    return x


def usd(v, nd: int = 2) -> str:
    return "-" if v is None else f"USD {v:,.{nd}f}"


def active_providers():
    return [(n, k, u, m) for (n, k, u, m) in PROVIDERS if secret(k)]


def provider_label() -> str:
    ps = active_providers()
    if not ps:
        return "AI 키 없음 (규칙 기반만 동작)"
    return " → ".join(f"{n}({m})" for n, _, _, m in ps)


def _parse_json(text: str):
    if not text:
        return None
    t = text.strip()
    fence = "`" * 3                                   # 마크다운 코드펜스 제거
    t = re.sub(rf"^{fence}(?:json)?|{fence}$", "", t, flags=re.M).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def chat_json(system: str, user: str, max_tokens: int = 900):
    """(parsed|None, provider_name|None, errors[]).  DeepSeek 실패/키 누락 시 Groq 로 폴백."""
    errors = []
    for name, key_name, url, model in PROVIDERS:
        key = secret(key_name)
        if not key:
            errors.append(f"{name}: 키 없음")
            continue
        body = {"model": model, "temperature": 0.2, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"}}
        for attempt in (0, 1):
            try:
                r = requests.post(url, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=_TIMEOUT)
                if r.status_code == 400 and attempt == 0:
                    body.pop("response_format", None)
                    continue
                r.raise_for_status()
                txt = r.json()["choices"][0]["message"]["content"]
                parsed = _parse_json(txt)
                if parsed is not None:
                    return sanitize_text(parsed), name, errors
                errors.append(f"{name}: JSON 파싱 실패")
                break
            except Exception as e:
                errors.append(f"{name}: {type(e).__name__}")
                break
    return None, None, errors


# ----------------------------------------------------------------------------
# Tab 1: 1-Shot 주문 결재판
# ----------------------------------------------------------------------------
_ORDER_SYS = (
    "너는 미국 주식 1~5일 스윙 전문 리스크 심사역이다. 잡담·역할극 없이 결재 의견만 낸다. "
    "가격 숫자는 입력으로 주어진 값만 사용하고 새 가격을 만들지 마라. "
    "반드시 JSON 한 개만 출력: "
    '{"verdict":"APPROVE|CONDITIONAL|REJECT","summary":"한 줄","reasons":["최대3"],"risks":["최대3"],"conditions":["최대2"]}'
)


def position_size(equity_usd: float, risk_pct: float, entry: float, stop_pct: float) -> float:
    if not (equity_usd and risk_pct and entry and stop_pct):
        return 0.0
    risk_usd = equity_usd * risk_pct / 100.0
    per_share = entry * stop_pct / 100.0
    return max(0.0, round(risk_usd / per_share, 2))


def order_text(c: dict, shares: float, kst_entry: str) -> str:
    p = c["plan"]
    t2 = usd(c.get("sma10")) if c.get("sma10") else "-"
    return sanitize_text(
        f"[매수 주문표] {c['symbol']}  ({c['setup_label']})\n"
        f"1) 진입(지정가): {usd(p['entry_lo'])} ~ {usd(p['entry_hi'])}  / 한국시간 {kst_entry} 이후, 시장가 금지\n"
        f"2) 강제 손절: {usd(p['stop'])}  (-{p['stop_pct']:.1f}%)  / 체결 즉시 스탑 등록\n"
        f"3) 1차 익절: {usd(p['t1'])}  (+{p['t1_pct']:.1f}%)  / 도달 시 50% 매도 후 손절선을 본전으로\n"
        f"4) 2차 완주: 10일선 {t2} 종가 이탈 시 잔량 청산\n"
        f"5) 손익비: {p['rr']:.2f}:1 (수수료 0.2% 반영 {p['rr_net']:.2f}:1)\n"
        f"6) 수량: {shares:g}주 (계좌 리스크 한도 기준)\n"
        f"7) 시간 손절: 3거래일 내 추가 상승 없으면 청산")


def review_order(c: dict, equity_usd: float, risk_pct: float, kst_entry: str) -> dict:
    p = c["plan"]
    facts = {
        "symbol": c["symbol"], "setup": c["setup_label"], "price": round(c["price"], 2),
        "gap_pct": round(c["gap"], 1), "volume_x": round(c["volx"], 1), "volume_projected": c["projected"],
        "catalyst": (c.get("catalyst") or {}).get("text"), "score": c["score"],
        "entry": [round(p["entry_lo"], 2), round(p["entry_hi"], 2)], "stop": round(p["stop"], 2),
        "stop_pct": round(p["stop_pct"], 2), "target1": round(p["t1"], 2), "target1_pct": round(p["t1_pct"], 2),
        "rr_gross": round(p["rr"], 2), "rr_net": round(p["rr_net"], 2),
        "lod_stop_wider_than_cap": p["lod_cap_exceeded"], "warnings": c["warnings"], "extra": c.get("extra", {}),
    }
    user = ("아래 후보의 매수 주문을 결재하라. 승인 기준: 손익비 2:1 이상, 촉매가 실재, 갭이 메워지지 않음. "
            "촉매가 약한 뉴스 헤드라인뿐이거나 장중 추정 거래량이면 CONDITIONAL 로 낮춰라.\n"
            + json.dumps(facts, ensure_ascii=False))
    ai, prov, errs = chat_json(_ORDER_SYS, user)
    shares = position_size(equity_usd, risk_pct, p["entry_hi"], p["stop_pct"])
    verdict = "REJECT" if not p["rr_ok"] else (ai or {}).get("verdict", "RULES_ONLY")
    if verdict not in ("APPROVE", "CONDITIONAL", "REJECT", "RULES_ONLY"):
        verdict = "CONDITIONAL"
    return {"verdict": verdict, "ai": ai, "provider": prov, "errors": errs, "shares": shares,
            "text": order_text(c, shares, kst_entry)}


# ----------------------------------------------------------------------------
# Tab 2: 포트폴리오 감리
# ----------------------------------------------------------------------------
_AUDIT_SYS = (
    "너는 미국 주식 스윙 포트폴리오 감리관이다. 보유 종목마다 HOLD / TRIM / EXIT 중 하나만 고른다. "
    "TRIM=50% 분할 익절, EXIT=전량 청산. 가격선은 입력값만 사용하고 새 숫자를 만들지 마라. "
    "JSON 한 개만 출력: "
    '{"items":[{"ticker":"","action":"HOLD|TRIM|EXIT","reason":"한 줄"}],"overall":"두 줄 이내 총평"}'
)


def audit_portfolio(rows: list) -> dict:
    live = [r for r in rows if r.get("price")]
    payload = [{"ticker": r["ticker"], "price": round(r["price"], 2), "avg": round(r["avg"], 2),
                "gross_pct": round(r["gross_pct"], 2), "stop": round(r["stop"], 2),
                "tp1": round(r["tp1"], 2), "sma10": round(r["sma10"], 2) if r.get("sma10") else None,
                "tp1_done": r["tp1_done"], "rule_signal": r["signal"], "rule_reason": r["reason"]} for r in live]
    ai, prov, errs = (None, None, [])
    if payload:
        ai, prov, errs = chat_json(_AUDIT_SYS, "보유 종목 전수 감리:\n" + json.dumps(payload, ensure_ascii=False), 1200)
    ai_map = {}
    if ai and isinstance(ai.get("items"), list):
        for it in ai["items"]:
            if isinstance(it, dict) and it.get("ticker"):
                ai_map[str(it["ticker"]).upper()] = it
    out = []
    for r in rows:
        rule = r["signal"] if r["signal"] in _SEV else "HOLD"
        it = ai_map.get(r["ticker"], {})
        a = str(it.get("action", "")).upper()
        a = a if a in _SEV else rule
        final = a if _SEV[a] > _SEV[rule] else rule          # AI 는 상향만 가능, 규칙 EXIT/TRIM 은 하향 불가
        reason = it.get("reason") if final == a and it.get("reason") else r["reason"]
        if r["signal"] == "NO_DATA":
            final, reason = "HOLD", "시세 조회 실패 — 수동 확인 필요"
        out.append({"ticker": r["ticker"], "action": final, "reason": sanitize_text(reason),
                    "rule": rule, "ai_action": a if it else None,
                    "stop": r["stop"], "tp1": r["tp1"], "sma10": r.get("sma10"), "price": r.get("price")})
    return {"items": out, "overall": (ai or {}).get("overall", ""), "provider": prov, "errors": errs}
