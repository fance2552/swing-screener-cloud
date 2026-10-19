"""에이전시 보고서 Streamlit 렌더. 커스텀 CSS / run_every 없음."""
from __future__ import annotations

import json
from pathlib import Path

from .config import AgencyCfg, LAST_REPORT_PATH

DEC_ICON = {"GO": "🟢", "CAUTION": "🟡", "BLOCK": "🔴", "PASS": "🟢",
            "HOLD": "🟡", "REJECT": "🔴", "NO_DATA": "⚪", "NO_SCAN": "⚪", "OK": "🟢"}
HEADLINE_LABEL = {"GO": ("🟢", "진입 후보"), "CAUTION": ("🟡", "주의"),
                  "BLOCK": ("⏳", "대기"), "NO_SCAN": ("⚪", "스캔 필요")}
DISCLAIMER = (
    "후보를 줄이고 규칙을 고정하는 도구이며 주문을 보내지 않습니다. "
    "승률을 보장하지 않으며 투자 자문이 아닙니다."
)


def _icon(v) -> str:
    return DEC_ICON.get(str(v or ""), "⚪")


def _usd(v) -> str:
    try:
        if v is None:
            return "-"
        return f"USD {float(v):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _pct(v, signed=True) -> str:
    try:
        if v is None:
            return "-"
        return f"{float(v):+.1f}%" if signed else f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return "-"


def load_last_report(path: Path | None = None) -> dict | None:
    p = path or LAST_REPORT_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def save_last_report(report: dict, path: Path | None = None) -> None:
    p = path or LAST_REPORT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding="utf-8")


def report_to_markdown(report: dict) -> str:
    hl = report.get("headline") or {}
    meta = report.get("meta") or {}
    lines = [
        f"# 에이전시 보고서 {meta.get('generated_kst', '')}",
        f"**{HEADLINE_LABEL.get(str(hl.get('decision') or ''), ('', hl.get('decision', '')))[1]}** — {hl.get('text', '')}",
        f"세션 {meta.get('session_date')} · 국면 {meta.get('regime')} · "
        f"계좌 {_usd(meta.get('account_usd'))} · 리스크 {meta.get('risk_pct')}%",
        "",
    ]
    if meta.get("pool_note"):
        lines.append(f"> {meta['pool_note']}")
        lines.append("")
    for c in report.get("candidates") or []:
        fin = c.get("final") or {}
        lines.append(f"## {_icon(fin.get('decision'))} {c.get('ticker')} · {c.get('name') or ''} · {fin.get('decision')}")
        fund = c.get("fundamental") or {}
        facts = fund.get("facts") or {}
        tech = c.get("technical") or {}
        lines.append(f"- 재무 {fund.get('verdict')} / 기술 {c.get('setup') or '미평가'} / 리스크 {(c.get('risk') or {}).get('decision')}")
        lines.append(f"- 매출 YoY {_pct(facts.get('revenue_yoy_pct'))} · 영업이익률 {_pct(facts.get('operating_margin_pct'), False)} · "
                     f"D/E {facts.get('debt_to_equity') if facts.get('debt_to_equity') is not None else '-'} · "
                     f"FCF {_usd(facts.get('fcf'))}")
        if tech:
            lines.append(f"- 진입 {_usd(tech.get('entry_lo'))}~{_usd(tech.get('entry_hi'))} · "
                         f"손절 {_usd(tech.get('stop'))} · 1차 {_usd(tech.get('tp1'))} · 수량 {tech.get('qty')}")
        for r in fin.get("reasons") or []:
            lines.append(f"- 사유: {r}")
        lines.append("")
    if report.get("cio_note"):
        lines.append("## 참고용 AI 해석")
        lines.append(str(report["cio_note"]))
        lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def render_report(report: dict, today_session: str = "") -> None:
    import streamlit as st
    from ai_advisor import sanitize_text, usd

    meta = report.get("meta") or {}
    hl = report.get("headline") or {}
    stale = bool(today_session and meta.get("session_date") and meta.get("session_date") != today_session)
    cap = f"마지막 생성 {meta.get('generated_kst', '-')} KST · 세션 {meta.get('session_date', '-')} · AI 호출 {meta.get('llm_calls', 0)}"
    if stale:
        cap += " · 이전 세션 보고서"
    if meta.get("pool_note"):
        cap += f" · {meta['pool_note']}"
    st.caption(cap)

    # 헤드라인 BLOCK 은 '진입 금지'가 아니라 '오늘 통과 후보 없음/보류' → 노란 대기 표시.
    box = {"GO": st.success, "CAUTION": st.warning, "BLOCK": st.warning, "NO_SCAN": st.info}.get(
        hl.get("decision"), st.info)
    icon, label = HEADLINE_LABEL.get(str(hl.get("decision") or ""), (_icon(hl.get("decision")), hl.get("decision", "")))
    box(f"{icon} {label} — {hl.get('text', '')}")
    chips = st.columns(4)
    chips[0].metric("세션일", str(meta.get("session_date") or "-"))
    chips[1].metric("국면", str(meta.get("regime") or "-"))
    chips[2].metric("계좌", usd(meta.get("account_usd")))
    chips[3].metric("1회 리스크", f"{meta.get('risk_pct', 0)}%")

    rows = []
    for c in report.get("candidates") or []:
        rows.append({
            "종목": c.get("ticker"),
            "셋업": c.get("setup") or "-",
            "점수": "-" if c.get("score") is None else c.get("score"),
            "기술": f"{_icon('CAUTION' if not c.get('technical') else ('CAUTION' if (c.get('technical') or {}).get('warnings') else 'GO'))} "
                   + ("미평가" if not c.get("technical") else ("경고" if (c.get("technical") or {}).get("warnings") else "통과")),
            "재무": f"{_icon((c.get('fundamental') or {}).get('verdict'))} {(c.get('fundamental') or {}).get('verdict')}",
            "리스크": f"{_icon((c.get('risk') or {}).get('decision'))} {(c.get('risk') or {}).get('decision')}",
            "최종": f"{_icon((c.get('final') or {}).get('decision'))} {(c.get('final') or {}).get('decision')}",
        })
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    else:
        st.info("후보 없음.")

    for i, c in enumerate(report.get("candidates") or []):
        fin = c.get("final") or {}
        title = f"{_icon(fin.get('decision'))} {c.get('ticker')} · {sanitize_text(c.get('name') or '-')} · {fin.get('decision')}"
        with st.expander(title, expanded=(i == 0)):
            tech = c.get("technical") or {}
            a, b = st.columns(2)
            with a:
                with st.container(border=True):
                    st.markdown("**📈 주식 분석가**")
                    cat = tech.get("catalyst") or {}
                    st.markdown(f"- 촉매: {sanitize_text(cat.get('text') or '자료 없음')}")
                    st.markdown(f"- 갭: {_pct(tech.get('gap')) if tech else '-'} · 거래량 배수: "
                                f"{tech.get('volx') if tech and tech.get('volx') is not None else '-'}"
                                + (" (추정)" if tech.get("projected") else ""))
                    st.markdown(f"- 점수: {c.get('score') if c.get('score') is not None else '-'} · 출처: {c.get('source')}")
            with b:
                with st.container(border=True):
                    st.markdown("**🏦 재무 분석가**")
                    fund = c.get("fundamental") or {}
                    facts = fund.get("facts") or {}
                    st.markdown(f"판정 {_icon(fund.get('verdict'))} **{fund.get('verdict')}** · 플로어 {fund.get('floor')} · {fund.get('status')}")
                    m = st.columns(4)
                    m[0].metric("매출 YoY", _pct(facts.get("revenue_yoy_pct")))
                    m[1].metric("영업이익률", _pct(facts.get("operating_margin_pct"), False))
                    de = facts.get("debt_to_equity")
                    m[2].metric("부채비율", "-" if de is None else f"{float(de):.2f}")
                    m[3].metric("FCF", _usd(facts.get("fcf")))
                    for s in fund.get("strengths") or []:
                        st.markdown(f"- 강점: {sanitize_text(s)}")
                    for s in fund.get("concerns") or []:
                        st.markdown(f"- 우려: {sanitize_text(s)}")
                    st.caption(f"일회성 이익 의심: {sanitize_text(fund.get('one_off_risk') or '자료 없음')}")
                    st.caption(f"신뢰도 {fund.get('confidence')} · red_flags {', '.join(facts.get('red_flags') or []) or '없음'}")
                    if fund.get("headline"):
                        st.markdown(f"> AI 해석: {sanitize_text(fund.get('headline'))}")
            c1, c2 = st.columns(2)
            with c1:
                with st.container(border=True):
                    st.markdown("**🧭 기술적 분석가**")
                    if not tech:
                        st.caption("기술 셋업 미평가")
                    else:
                        st.markdown(
                            f"- 진입: **{_usd(tech.get('entry_lo'))} ~ {_usd(tech.get('entry_hi'))}**\n"
                            f"- 손절: **{_usd(tech.get('stop'))}** ({_pct(-(tech.get('stop_pct') or 0), False)})\n"
                            f"- 1차 목표: **{_usd(tech.get('tp1'))}**\n"
                            f"- 손익비: {tech.get('rr') if tech.get('rr') is not None else '-'} · 수량 {tech.get('qty')}\n"
                            f"- 권장 진입 시각(KST): {tech.get('entry_kst') or '-'}"
                        )
            with c2:
                with st.container(border=True):
                    st.markdown("**🛡️ 리스크 매니저**")
                    for chk in (c.get("risk") or {}).get("checks") or []:
                        mark = "✅" if chk.get("ok") else "❌"
                        st.markdown(f"{mark} {chk.get('name')} — {chk.get('detail')}")
            st.markdown("**최종 사유**")
            for r in fin.get("reasons") or []:
                st.markdown(f"- {sanitize_text(r)}")

    st.markdown("#### 포트폴리오 리스크")
    port = report.get("portfolio") or {}
    p = st.columns(3)
    p[0].metric("보유 수", f"{port.get('positions', 0)}개")
    p[1].metric("총 오픈 리스크", f"{float(port.get('open_risk_pct') or 0):.2f}%")
    cap_r = AgencyCfg().max_open_risk_pct
    p[2].metric("상한 대비", f"{float(port.get('open_risk_pct') or 0):.2f}% / {cap_r:.1f}%")
    if port.get("open_risk_is_lower_bound"):
        st.warning("시세 없는 보유가 있어 총 오픈 리스크는 하한값입니다.")
    for row in port.get("rows") or []:
        if not isinstance(row, dict):
            continue
        st.caption(
            f"{row.get('ticker')} · {row.get('signal')} · 현재가 {_usd(row.get('price'))} · "
            f"{sanitize_text(row.get('reason') or '')}"
        )

    st.markdown("#### 참고용 AI 해석")
    cio_staff = next((s for s in (report.get("staff_log") or []) if s.get("role") == "기록·총괄"), None)
    if report.get("cio_note"):
        st.markdown(f"> {sanitize_text(report['cio_note'])}")
    elif cio_staff and cio_staff.get("status") == "SKIP":
        st.caption(f"총평 없음 ({cio_staff.get('reason') or '후보 0명 — 호출 생략'}). 판정은 코드만 사용.")
    else:
        st.caption("총평 없음 (AI 미응답). 판정은 코드만 사용.")

    with st.expander("직원 근무 일지"):
        log_rows = []
        for s in report.get("staff_log") or []:
            log_rows.append({
                "직원": s.get("role"), "상태": s.get("status"),
                "소요 초": s.get("seconds"), "AI 호출": s.get("llm_calls"),
                "FMP 호출 수": s.get("fmp_calls", 0),
                "실패 수": s.get("fmp_fails", 0),
                "마지막 HTTP 코드": s.get("last_http") if s.get("last_http") is not None else "",
                "사유": s.get("reason") or "",
                "오류": s.get("error") or "",
            })
        if log_rows:
            st.dataframe(log_rows, width="stretch", hide_index=True)

    with st.expander("📊 누적 성과"):
        st.caption("기록 준비 중")

    st.download_button(
        "⬇ 보고서 다운로드 (.md)",
        data=report_to_markdown(report),
        file_name=f"agency_{meta.get('session_date') or 'report'}.md",
        mime="text/markdown",
    )
    st.caption(DISCLAIMER)
