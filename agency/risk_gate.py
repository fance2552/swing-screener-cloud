"""리스크 매니저. 순수 함수, LLM 없음."""
from __future__ import annotations

from .config import AgencyCfg, DEC_SEV


def _f(x):
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _holding_risk_usd(pos: dict, price: float | None) -> tuple[float, bool]:
    """(리스크 USD, 시세없음). 본전 스탑이면 0. 시세 없으면 합산에 넣지 않고 하한 플래그."""
    from screener import portfolio as pf

    qty = _f(pos.get("qty")) or 0.0
    if pos.get("tp1_done"):
        return 0.0, False
    if price is None or price <= 0:
        return 0.0, True
    ev = pf.evaluate(pos, price, None)
    stop = _f(ev.get("stop"))
    if stop is None:
        return 0.0, True
    dist = max(0.0, price - stop)
    return dist * qty, False


def evaluate_risk(
    candidates: list,
    holdings: list,
    marks: dict,
    account_usd: float,
    risk_pct: float,
    regime: str,
    today_new_count: int = 0,
    cfg: AgencyCfg | None = None,
    sectors: dict | None = None,
) -> dict:
    """후보별 {decision, checks} + portfolio 요약."""
    cfg = cfg or AgencyCfg()
    sectors = sectors or {}
    account = _f(account_usd) or 0.0
    held = [p for p in (holdings or []) if isinstance(p, dict) and p.get("ticker")]
    held_tickers = {str(p["ticker"]).upper() for p in held}
    held_sectors: dict[str, int] = {}
    for p in held:
        sec = sectors.get(str(p["ticker"]).upper())
        if sec:
            held_sectors[sec] = held_sectors.get(sec, 0) + 1

    open_risk = 0.0
    missing_px = False
    rows = []
    for p in held:
        t = str(p["ticker"]).upper()
        px = (marks.get(t) or {}).get("price") if isinstance(marks, dict) else None
        px = _f(px)
        ru, miss = _holding_risk_usd(p, px)
        open_risk += ru
        missing_px = missing_px or miss
        try:
            from screener import portfolio as pf
            sma = (marks.get(t) or {}).get("sma10") if isinstance(marks, dict) else None
            rows.append(pf.evaluate(p, px, _f(sma)))
        except Exception:
            rows.append({"ticker": t, "signal": "NO_DATA", "price": px})

    open_risk_pct = (open_risk / account * 100.0) if account > 0 else 0.0
    per: dict[str, dict] = {}

    for c in candidates or []:
        ticker = str(c.get("ticker") or "").upper()
        tech = c.get("technical") if isinstance(c.get("technical"), dict) else None
        checks = []
        worst = "GO"

        def add(name: str, ok: bool, detail: str, fail: str = "BLOCK"):
            nonlocal worst
            checks.append({"name": name, "ok": ok, "detail": detail})
            if not ok and DEC_SEV.get(fail, 0) > DEC_SEV.get(worst, 0):
                worst = fail

        reg_u = str(regime or "").upper()
        if cfg.block_on_correction and reg_u == "CORRECTION":
            add("correction_block", False, "국면 CORRECTION — 신규 진입 차단", "BLOCK")
        elif reg_u not in ("NORMAL", "PRESSURE"):
            add("correction_block", False, f"국면 판정 불가({regime or '대기'}) — 확인 전 보수 진입", "CAUTION")
        elif reg_u == "PRESSURE":
            add("correction_block", False, "국면 PRESSURE (분산일 누적) — 엄선 진입", "CAUTION")
        else:
            add("correction_block", True, f"국면 {regime or '대기'}")

        if ticker in held_tickers:
            add("already_held", False, f"{ticker} 이미 보유", "BLOCK")
        else:
            add("already_held", True, f"{ticker} 미보유")

        n_after = len(held) + (0 if ticker in held_tickers else 1)
        if n_after > cfg.max_positions:
            add("max_positions", False,
                f"보유 {len(held)} + 신규 1 = {n_after} / 상한 {cfg.max_positions}", "BLOCK")
        else:
            add("max_positions", True,
                f"보유 {len(held)} + 신규 1 = {n_after} / 상한 {cfg.max_positions}")

        qty = _f((tech or {}).get("qty")) or 0.0
        entry = _f((tech or {}).get("entry_hi")) or _f((tech or {}).get("entry_lo"))
        stop_pct = _f((tech or {}).get("stop_pct"))
        new_risk = 0.0
        if account > 0 and qty > 0 and entry and stop_pct:
            new_risk = qty * entry * stop_pct / 100.0
        elif account > 0 and tech and risk_pct:
            new_risk = account * risk_pct / 100.0
        total_pct = ((open_risk + new_risk) / account * 100.0) if account > 0 else 0.0
        if account <= 0:
            add("open_risk", False, "계좌 금액 없음", "BLOCK")
        elif total_pct > cfg.max_open_risk_pct:
            add("open_risk", False,
                f"오픈 리스크 {total_pct:.2f}% / 상한 {cfg.max_open_risk_pct:.1f}%"
                + (" (시세 없는 보유 있어 하한값)" if missing_px else ""),
                "BLOCK")
        else:
            add("open_risk", True,
                f"오픈 리스크 {total_pct:.2f}% / 상한 {cfg.max_open_risk_pct:.1f}%"
                + (" (시세 없는 보유 있어 하한값)" if missing_px else ""))

        if today_new_count + 1 > cfg.max_new_per_day:
            add("daily_new", False,
                f"오늘 신규 {today_new_count} + 1 / 상한 {cfg.max_new_per_day}", "CAUTION")
        else:
            add("daily_new", True,
                f"오늘 신규 {today_new_count} + 1 / 상한 {cfg.max_new_per_day}")

        if tech and entry and qty and account > 0:
            notional_pct = entry * qty / account * 100.0
            if notional_pct > cfg.max_position_pct:
                cap_qty = account * cfg.max_position_pct / 100.0 / entry
                add("position_size", False,
                    f"포지션 {notional_pct:.1f}% / 상한 {cfg.max_position_pct:.0f}% → 수량 {qty:g} → {cap_qty:.2f} 축소",
                    "CAUTION")
            else:
                add("position_size", True, f"포지션 {notional_pct:.1f}% / 상한 {cfg.max_position_pct:.0f}%")
        else:
            add("position_size", True, "기술 셋업 없어 수량 미산출")

        sec = sectors.get(ticker)
        if sec:
            n_sec = held_sectors.get(sec, 0) + (0 if ticker in held_tickers else 1)
            if n_sec > cfg.max_same_sector:
                add("sector_cap", False, f"섹터 {sec} {n_sec} / 상한 {cfg.max_same_sector}", "CAUTION")
            else:
                add("sector_cap", True, f"섹터 {sec} {n_sec} / 상한 {cfg.max_same_sector}")
        else:
            add("sector_cap", True, "섹터 미확인 — 검사 생략")

        lod = bool((tech or {}).get("lod_cap_exceeded"))
        if lod:
            add("lod_cap", False, "손절이 -3.5%로 조여짐 (lod_cap_exceeded)", "CAUTION")
        else:
            add("lod_cap", True, "손절 한도 조임 없음")

        per[ticker] = {"decision": worst, "checks": checks}

    return {
        "by_ticker": per,
        "portfolio": {
            "positions": len(held),
            "open_risk_pct": round(open_risk_pct, 4),
            "open_risk_is_lower_bound": missing_px,
            "rows": rows,
        },
    }
