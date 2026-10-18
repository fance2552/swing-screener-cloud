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
_SEV = {"HOLD": 0, "TRIM": 1, "EXIT": 2, "TIME_EXIT": 3}


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


def _provider_order(prefer: str | None) -> tuple:
    picked = str(prefer or "DEEPSEEK").upper()
    if picked == "GROQ":
        return tuple(p for p in PROVIDERS if p[0] == "GROQ") + tuple(p for p in PROVIDERS if p[0] != "GROQ")
    return PROVIDERS


def chat_text(system: str, messages: list, prefer: str | None = None, max_tokens: int = 900):
    """평문 답. 고른 모델이 없거나 실패하면 다른 쪽으로 넘긴다. (text, provider, errors)."""
    errors = []
    payload = [{"role": "system", "content": system}]
    for msg in (messages or [])[-12:]:
        role = str(msg.get("role") or "user")
        if role not in ("user", "assistant"):
            role = "user"
        content = str(msg.get("content") or "").strip()
        if content:
            payload.append({"role": role, "content": content})
    if len(payload) == 1:
        payload.append({"role": "user", "content": "상태만 짧게 브리핑하라."})
    for name, key_name, url, model in _provider_order(prefer):
        key = secret(key_name)
        if not key:
            errors.append(f"{name}: 키 없음")
            continue
        body = {"model": model, "temperature": 0.3, "max_tokens": max_tokens, "messages": payload}
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=_TIMEOUT)
            r.raise_for_status()
            txt = str(r.json()["choices"][0]["message"]["content"] or "").strip()
            if txt:
                return sanitize_text(txt), name, errors
            errors.append(f"{name}: 본문 없음")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
    return "응답을 받지 못했다. " + " / ".join(errors), None, errors


def _closed_today() -> list[str]:
    from datetime import datetime
    from pathlib import Path
    from zoneinfo import ZoneInfo

    path = Path("closed_trades.jsonl")
    if not path.exists():
        try:
            import cloud_sync
            cloud_sync.pull_closed_trades_from_cloud(path)
        except Exception:
            return []
    if not path.exists():
        return []
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    found: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        day = str(row.get("closed_at_et_date") or row.get("exit_date") or "")[:10]
        symbol = str(row.get("ticker") or "").upper().strip()
        if day == today and symbol and symbol not in found:
            found.append(symbol)
    return found


def _legacy_hunt() -> dict:
    try:
        from state import SHARED
        raw = SHARED.safe_get("hunting_pool") or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def build_system_context(
    regime: str = "",
    hunt: dict | None = None,
    holdings: list | None = None,
    closed: list | None = None,
) -> str:
    """질문마다 붙이는 사실. 없는 가격은 비워 둔다."""
    if holdings is None:
        try:
            from screener import portfolio as pf
            holdings, _msg = pf.load_positions()
        except Exception:
            holdings = []
    held_brief = []
    for row in holdings or []:
        if not isinstance(row, dict):
            continue
        held_brief.append({
            "ticker": row.get("ticker"),
            "qty": row.get("qty"),
            "avg": row.get("avg"),
            "stop_pct": row.get("stop_pct"),
            "opened": row.get("opened"),
            "strategy": row.get("strategy"),
            "tp1_done": row.get("tp1_done"),
        })
    pool = hunt if isinstance(hunt, dict) else {}
    top = pool.get("top") if isinstance(pool.get("top"), list) else []
    live = []
    for row in top[:10]:
        if not isinstance(row, dict):
            continue
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        live.append({
            "symbol": row.get("symbol"),
            "setup": row.get("setup"),
            "score": row.get("score"),
            "price": row.get("price"),
            "gap_pct": row.get("gap"),
            "volume_x": row.get("volx"),
            "dry_mean": extra.get("dry_mean"),
        })
    if closed is None:
        closed = _closed_today()
    legacy = _legacy_hunt()
    legacy_trim = {}
    if isinstance(legacy, dict):
        for key, value in legacy.items():
            legacy_trim[key] = value[:5] if isinstance(value, list) else value
    else:
        legacy_trim = legacy
    return (
        "너는 Cloud Cockpit 전략 비서다. 아래 JSON만 사실로 사용하라. "
        "없는 숫자와 없는 종목은 만들지 마라. 주문은 보내지 마라. 달러 기호 대신 USD 를 써라.\n"
        f"사령관 보유 종목: {json.dumps(held_brief, ensure_ascii=False)}\n"
        f"오늘자 사냥터 후보(런업/RSI2/PEAD): {json.dumps(legacy_trim, ensure_ascii=False, default=str)}\n"
        f"오늘자 사냥터 후보(EP/눌림목): {json.dumps(live, ensure_ascii=False)}\n"
        f"시장 국면: {json.dumps(regime or '대기', ensure_ascii=False)}\n"
        f"오늘 이미 청산한 티커: {json.dumps(list(closed or []), ensure_ascii=False)}"
    )


_CIO_PERSONA = (
    "너의 이름은 사령관 직속 CIO 딥시크다. 다른 AI(Claude, Gemini 등)라고 사칭하지 마라. "
    "만약 스스로를 그렇게 소개한다면 즉시 사과하고 정체성을 바로잡아라."
)
_BADGE = {
    "DEEPSEEK": "DeepSeek 총괄 CIO",
    "deepseek-chat": "DeepSeek 총괄 CIO",
    "GROQ": "Groq 실무 참모",
    "llama-3.3-70b-versatile": "Groq 실무 참모",
}


def secretary_badge(prefer: str | None) -> str:
    return _BADGE.get(str(prefer or "").strip(), "DeepSeek 총괄 CIO")


def ask_secretary(history: list, prefer: str | None, context: str) -> tuple[str, str | None, list]:
    """대화 배열과 최신 컨텍스트로 한 턴을 받는다."""
    system = _CIO_PERSONA + "\n" + (context or "")
    return chat_text(system, history, prefer=prefer, max_tokens=900)


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
    "너는 v4 사냥 데스크의 리스크 심사역이다. 전략은 둘뿐이다. "
    "셋업 A EP(Episodic Pivot)는 당일 갭과 거래량 폭증을 본다. "
    "셋업 B 눌림목(Pullback)은 EP 이후 거래량이 말랐는지를 본다. "
    "런업, 스퀴즈, PEAD, RSI2 규칙은 쓰지 마라. "
    "가격 숫자는 입력값만 사용하고 새 가격을 만들지 마라. "
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
        f"[수수료 반영 실질 손익비: {p['rr_net']:.1f}]\n"
        f"[매수 주문표] {c['symbol']}  ({c['setup_label']})\n"
        f"1) 진입(지정가): {usd(p['entry_lo'])} ~ {usd(p['entry_hi'])}  / 한국시간 {kst_entry} 이후, 시장가 금지\n"
        f"2) 강제 손절: {usd(p['stop'])}  (-{p['stop_pct']:.1f}%)  / 체결 즉시 스탑 등록\n"
        f"3) 1차 익절: {usd(p['t1'])}  (+{p['t1_pct']:.1f}%)  / 도달 시 50% 매도 후 손절선을 본전으로\n"
        f"4) 2차 완주: 10일선 {t2} 종가 이탈 시 잔량 청산\n"
        f"5) 손익비: {p['rr']:.2f}:1 (수수료 0.2% 반영 {p['rr_net']:.2f}:1)\n"
        f"6) 수량: {shares:g}주 (계좌 리스크 한도 기준)\n"
        f"7) 시간 손절: 3거래일 내 추가 상승 없으면 청산")


def _brief_candidate(row: dict) -> dict:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    catalyst = row.get("catalyst") if isinstance(row.get("catalyst"), dict) else {}
    return {
        "symbol": row.get("symbol"),
        "setup": row.get("setup"),
        "gap_pct": row.get("gap"),
        "volume_x": row.get("volx"),
        "volume_projected": row.get("projected"),
        "dry_mean": extra.get("dry_mean"),
        "depth_pct": extra.get("depth"),
        "days_since_ep": extra.get("days_since"),
        "catalyst": catalyst.get("type"),
        "score": row.get("score"),
    }


def _split_candidates(rows: list | None) -> tuple[list, list]:
    ep_rows, pull_rows = [], []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        brief = _brief_candidate(row)
        if row.get("setup") == "B":
            pull_rows.append(brief)
        else:
            ep_rows.append(brief)
    return ep_rows, pull_rows


def review_order(
    c: dict,
    equity_usd: float,
    risk_pct: float,
    kst_entry: str,
    ep_candidates: list | None = None,
    pullback_candidates: list | None = None,
) -> dict:
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
    ep_board, pull_board = _split_candidates((ep_candidates or []) + (pullback_candidates or []))
    if c.get("setup") == "B":
        pull_board = [_brief_candidate(c)] + [row for row in pull_board if row.get("symbol") != c.get("symbol")]
    else:
        ep_board = [_brief_candidate(c)] + [row for row in ep_board if row.get("symbol") != c.get("symbol")]
    user = (
        "오늘 뜬 EP 종목은 갭과 거래량 폭증을 분석하라. "
        "셋업 B 눌림목은 거래량 감소(dry_mean)를 검증하라. "
        "승인 기준: 촉매가 실재, 갭이 메워지지 않음, 거래량 확증. 손익비는 참고 정보이며 반려 사유가 아니다. "
        "촉매가 약한 뉴스이거나 장중 추정 거래량이면 CONDITIONAL 로 낮춰라.\n"
        + json.dumps(
            {"focus": facts, "EP_candidates": ep_board[:3], "Pullback_candidates": pull_board[:3]},
            ensure_ascii=False,
        )
    )
    ai, prov, errs = chat_json(_ORDER_SYS, user)
    shares = position_size(equity_usd, risk_pct, p["entry_hi"], p["stop_pct"])
    net_rr = float(p["rr_net"])
    # 손익비는 표시만 한다. 판정은 종목 선별(촉매·갭·거래량)과 AI 결재로 한다.
    verdict = (ai or {}).get("verdict", "RULES_ONLY")
    if verdict not in ("APPROVE", "CONDITIONAL", "REJECT", "RULES_ONLY"):
        verdict = "CONDITIONAL"
    return {"verdict": verdict, "ai": ai, "provider": prov, "errors": errs, "shares": shares,
            "net_rr": net_rr, "text": order_text(c, shares, kst_entry)}


# ----------------------------------------------------------------------------
# Tab 2: 포트폴리오 감리
# ----------------------------------------------------------------------------
_AUDIT_SYS = (
    "너는 v4 수호 데스크 감리관이다. 보유 종목마다 HOLD / TRIM / EXIT 중 하나만 고른다. "
    "헌법: 현재가가 입력된 손절가 이하면 EXIT. 손절 한도는 평단 대비 -2.5%에서 -3.5%다. "
    "입력 tp1(+5%)에 닿았고 아직 분할 전하면 TRIM(50% 매도). "
    "1차 익절 후 10일선 아래면 EXIT. "
    "EP 후보는 갭과 거래량 폭증, 눌림목 후보는 거래량 감소만 총평에 한 줄로 적어라. "
    "런업과 스퀴즈 규칙은 쓰지 마라. 가격은 입력값만 써라. "
    "규칙 signal이 EXIT 또는 TRIM이면 그보다 약한 판정으로 내리지 마라. "
    "JSON 한 개만 출력: "
    '{"items":[{"ticker":"","action":"HOLD|TRIM|EXIT","reason":"한 줄"}],"overall":"두 줄 이내 총평"}'
)


def audit_portfolio(rows: list, ep_candidates: list | None = None, pullback_candidates: list | None = None) -> dict:
    live = [r for r in rows if r.get("price")]
    payload = [{"ticker": r["ticker"], "price": round(r["price"], 2), "avg": round(r["avg"], 2),
                "gross_pct": round(r["gross_pct"], 2), "stop": round(r["stop"], 2),
                "tp1": round(r["tp1"], 2), "sma10": round(r["sma10"], 2) if r.get("sma10") else None,
                "tp1_done": r["tp1_done"], "rule_signal": r["signal"], "rule_reason": r["reason"]} for r in live]
    ai, prov, errs = (None, None, [])
    ep_board, pull_board = _split_candidates((ep_candidates or []) + (pullback_candidates or []))
    if payload or ep_board or pull_board:
        ai, prov, errs = chat_json(
            _AUDIT_SYS,
            "보유 종목을 현재가 기준 -2.5%~-3.5% 손절과 +5% 분할 익절로 판정하라. "
            "EP_candidates의 갭과 거래량 폭증, Pullback_candidates의 거래량 감소를 총평에 검증하라.\n"
            + json.dumps(
                {"holdings": payload, "EP_candidates": ep_board[:3], "Pullback_candidates": pull_board[:3]},
                ensure_ascii=False,
            ),
            1200,
        )
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
