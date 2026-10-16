"""이벤트 드리븐 스윙 콕핏 v3.0. 풀와이드 4탭: 🎯 듀얼 사냥 데스크 / 🛡️ 수호 & 포트폴리오 / 🧠 AI / ⚡ 22:30 지휘소."""
from __future__ import annotations

import fd_limit  # noqa: F401

import hmac
import html
import importlib
import os
import re
import threading
import time
import warnings

warnings.filterwarnings("ignore")

import pandas as pd
import streamlit as st

import ai_advisor
importlib.reload(ai_advisor)
import config
import engine
import env_settings
from screener import event_driven as ed
from screener import portfolio, radar
from screener.portfolio import (
    STRATEGY_LABEL_TO_KEY,
    fetch_auto_fill_for_registration,
    validate_position_registration,
)
from state import SHARED


def _bind_shared_flags() -> None:
    """fileWatcherType=none 이라 state.py 가 메모리에 남는다.
    재배포 직후 옛 SHARED 객체에는 신규 필드가 없다. 없으면 붙여서 헤더가 죽지 않게 한다.
    """
    defaults: dict[str, object] = {
        "heavy_scan_running": False,
        "heavy_scan_progress": 0.0,
        "heavy_scan_done_ts": None,
        "radar_on": True,
        "last_quick_scan_ts": None,
        "hunting_pool": {"pead": [], "runup": [], "rsi2": []},
        "hunting_pool_updated_ts": None,
        "scan_errors": [],
        "scan_progress": 0.0,
        "scan_status_text": "",
        "warroom_last_result": None,
    }
    for key, val in defaults.items():
        if not hasattr(SHARED, key):
            setattr(SHARED, key, list(val) if isinstance(val, list) else val)


_bind_shared_flags()

st.set_page_config(
    page_title=config.APP_TITLE,
    layout="wide",
    initial_sidebar_state="expanded",
)

env_settings.load()
_keys_ready = env_settings.api_keys_ready()


def _get_secret(name: str) -> str:
    try:
        val = st.secrets.get(name)
        if val:
            return str(val).strip()
    except Exception:
        pass
    return (os.environ.get(name) or "").strip()


@st.cache_resource
def _pin_guard() -> dict:
    # 세션별 카운터는 새 탭을 열면 0으로 돌아가므로, 실패 지연은 프로세스 전체에서 공유한다.
    return {"lock": threading.Lock(), "fails": 0, "next_ok": 0.0}


def _require_pin() -> None:
    if st.session_state.get("authenticated"):
        return
    env_settings.load()
    correct_pin = _get_secret("COCKPIT_PIN")
    if not correct_pin:
        st.error("❌ COCKPIT_PIN이 설정되지 않았습니다. secrets.toml 또는 .env를 확인하십시오.")
        st.stop()

    st.markdown("## 🔒 사령관 전용 콕핏")
    entered = st.text_input("PIN", type="password", key="pin_input")
    if st.button("입장"):
        guard = _pin_guard()
        with guard["lock"]:
            wait = guard["next_ok"] - time.time()
            if wait > 0:
                st.error(f"연속 실패로 잠김. {wait:.0f}초 후 다시 시도하십시오.")
                st.stop()
            if hmac.compare_digest(str(entered).strip().encode("utf-8"), str(correct_pin).encode("utf-8")):
                guard["fails"] = 0
                guard["next_ok"] = 0.0
                ok = True
            else:
                guard["fails"] += 1
                guard["next_ok"] = time.time() + min(2 ** guard["fails"], 60)
                ok = False
        if ok:
            st.session_state.authenticated = True
            st.rerun()
        st.error("PIN이 틀렸습니다.")
    st.stop()


_require_pin()

if "is_scanning" not in st.session_state:
    st.session_state.is_scanning = False

EXIT_REASON_LABEL = {
    "SL": "손절선 터치",
    "TIME_EXIT": "보유기한 만기 청산",
    "D2_EXIT": "런업 D-2 강제청산 (실적 직전)",
    "SMA5_EXIT": "RSI2 5일선 재돌파 익절",
}
_EXIT_CODES = frozenset(EXIT_REASON_LABEL)

CSS = """
<style>
#MainMenu, footer, [data-testid="stToolbarActions"], [data-testid="stStatusWidget"], [data-testid="stDecoration"] { display: none !important; }
[data-testid="stHeader"], [data-testid="stToolbar"] {
  background: transparent !important; height: 0 !important; min-height: 0 !important; overflow: visible !important;
}
[data-testid="stExpandSidebarButton"] {
  pointer-events: auto !important; position: fixed !important; top: 8px !important; left: 8px !important;
  z-index: 1000002 !important; display: flex !important; width: 34px !important; height: 34px !important;
  background: #151C24 !important; border: 1px solid #3DDC97 !important; border-radius: 8px !important;
}
.block-container { padding: 0.5rem 0.9rem 0.4rem 0.9rem !important; max-width: 100% !important; }
.badge {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 700;
  border: 1px solid #2A3544; background: #151C24; color: #E8EEF4;
}
.reg-GREEN { background: #10291d; border-color: #3DDC97; color: #3DDC97; }
.reg-YELLOW { background: #2a230d; border-color: #F5A623; color: #F5A623; }
.reg-RED { background: #2a1016; border-color: #FF5C7A; color: #FF5C7A; }
.sess-PRE { background: #2a230d; border-color: #F5A623; color: #F5A623; font-size: 13px; }
.sess-RTH { background: #10291d; border-color: #3DDC97; color: #3DDC97; font-size: 13px; }
.sess-AH { background: #1a1630; border-color: #C084FC; color: #C084FC; font-size: 13px; }
.sess-CLOSED { background: #151C24; border-color: #8A97A8; color: #8A97A8; font-size: 13px; }
.muted { color: #8A97A8; font-size: 12px; }
.banner { font-size: 14px; font-weight: 700; color: #3DDC97; padding: 4px 0; }
.banner-fatal { font-size: 14px; font-weight: 800; color: #FF5C7A; padding: 4px 0; }
.telem-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.guide-go, .guide-wait, .guide-chase { border-radius: 8px; padding: 6px 10px; margin: 2px 0 6px 0; }
.guide-go { background: #10291d; border: 1px solid #3DDC97; }
.guide-wait { background: #2a230d; border: 1px solid #F5A623; }
.guide-chase { background: #2a1016; border: 1px solid #FF5C7A; }
.guide-title { font-size: 15px; font-weight: 800; letter-spacing: -0.02em; margin: 0 0 3px 0; }
.guide-go .guide-title { color: #3DDC97; }
.guide-wait .guide-title { color: #F5A623; }
.guide-chase .guide-title { color: #FF5C7A; }
.guide-line { font-size: 13px; font-weight: 600; line-height: 1.3; color: #E8EEF4; margin: 0; }
.pos-card {
  background: #151C24; border: 1px solid #2A3544; border-radius: 10px;
  padding: 12px 14px; margin: 0 0 10px 0; height: 100%;
}
.pos-head { font-size: 15px; font-weight: 800; letter-spacing: -0.02em; margin: 0 0 5px 0; color: #E8EEF4; }
.pos-metrics { font-size: 13px; font-weight: 600; color: #E8EEF4; margin: 2px 0; }
.pos-wall { font-size: 12px; font-weight: 700; color: #C084FC; margin: 4px 0 0 0; }
.diag { font-size: 14px; font-weight: 800; margin: 5px 0 2px 0; color: #E8EEF4; }
.diag-order { font-size: 13px; font-weight: 650; color: #E8EEF4; margin: 3px 0 0 0; }
.summary-box {
  background: #151C24; border: 1px solid #2A3544; border-radius: 10px;
  padding: 12px 16px; margin: 0 0 10px 0; display: flex; gap: 28px; flex-wrap: wrap;
}
.summary-item .label { font-size: 12px; color: #8A97A8; }
.summary-item .value { font-size: 20px; font-weight: 800; letter-spacing: -0.02em; }
.up { color: #3DDC97; }
.dn { color: #FF5C7A; }
div[data-testid="stAlert"] { padding: 0.3rem 0.55rem !important; margin: 0.15rem 0 0.35rem 0 !important; }
.st-key-header_live { position: relative; z-index: 1; }
.st-key-desk_switch { position: relative; z-index: 40; background: #0e1117; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# 하얀 화면 = 브라우저 쪽 페이지가 통째로 죽은 상태. 서버·스캔은 살아 있지만 사이렌·음성은
# 이 탭에서만 울리므로, 죽으면 직전 오류를 localStorage에 남기고 스스로 새로고침한다.
# 새로고침 뒤에는 Chrome이 클릭 전까지 소리를 막으므로 복구 배너 클릭으로 다시 연다.
_WATCHDOG_JS = """
<script>
(function () {
  if (window.__ldpbWatchdog) return;
  window.__ldpbWatchdog = true;
  var CRASH_KEY = "ldpb_last_crash";
  var RELOAD_KEY = "ldpb_last_reload_at";
  var errs = [];
  function stamp() { return new Date().toLocaleTimeString("ko-KR", { hour12: false }); }
  function keep(msg) {
    errs.push(stamp() + " " + String(msg).slice(0, 300));
    if (errs.length > 5) errs.shift();
  }
  window.addEventListener("error", function (e) { keep(e.message || e); });
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    keep((r && (r.message || r)) || "unhandled rejection");
  });
  var origError = console.error;
  console.error = function () {
    try { keep(Array.prototype.map.call(arguments, String).join(" ")); } catch (x) {}
    return origError.apply(console, arguments);
  };

  function unlockAudio() {
    try {
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      ctx.resume();
      setTimeout(function () { try { ctx.close(); } catch (x) {} }, 300);
    } catch (x) {}
  }

  function showRecovered(prev) {
    var box = document.createElement("div");
    box.id = "ldpb-recovered";
    box.style.cssText = "position:fixed;right:14px;bottom:14px;z-index:2147483647;max-width:560px;" +
      "background:#2a1016;border:2px solid #FF5C7A;border-radius:10px;padding:10px 14px;" +
      "color:#E8EEF4;font:600 13px/1.4 sans-serif;cursor:pointer;box-shadow:0 4px 18px rgba(0,0,0,.5)";
    var title = document.createElement("div");
    title.style.cssText = "font-weight:800;font-size:14px;color:#FF5C7A;margin-bottom:4px";
    title.textContent = "♻️ 화면 자동 복구됨 (" + (prev.at || "") + ") — 경보음 다시 켜기: 여기를 한 번 클릭";
    box.appendChild(title);
    var list = prev.errors && prev.errors.length ? prev.errors : ["오류 메시지 없음"];
    list.slice(-3).forEach(function (line) {
      var row = document.createElement("div");
      row.style.cssText = "font:500 12px/1.35 monospace;color:#F5A623;word-break:break-all";
      row.textContent = line;
      box.appendChild(row);
    });
    box.addEventListener("click", function () { unlockAudio(); box.remove(); });
    document.body.appendChild(box);
  }

  try {
    var prev = JSON.parse(localStorage.getItem(CRASH_KEY) || "null");
    if (prev) {
      localStorage.removeItem(CRASH_KEY);
      console.warn("[LDPB] 하얀 화면 자동 복구", prev);
      showRecovered(prev);
    }
  } catch (x) {}

  var armed = false;
  var misses = 0;
  setInterval(function () {
    var app = document.querySelector('[data-testid="stApp"]');
    var alive = !!app && (document.body.innerText || "").trim().length > 0;
    if (alive) { armed = true; misses = 0; return; }
    if (!armed) return;
    misses += 1;
    if (misses < 2) return;
    var last = Number(localStorage.getItem(RELOAD_KEY) || 0);
    if (Date.now() - last < 15000) return;
    try {
      localStorage.setItem(CRASH_KEY, JSON.stringify({ at: stamp(), errors: errs }));
      localStorage.setItem(RELOAD_KEY, String(Date.now()));
    } catch (x) {}
    location.reload();
  }, 2000);
})();
</script>
"""

if "boot_health" not in st.session_state:
    st.session_state.boot_health = True
    if _keys_ready:
        engine.healthcheck()
        engine.ensure_heartbeat()

_saved_keys = env_settings.get_api_keys()
if "api_form_seeded" not in st.session_state:
    for _k, _v in _saved_keys.items():
        st.session_state[f"api_input_{_k}"] = _v
    st.session_state.api_form_seeded = True

with st.sidebar:
    st.markdown("### 이벤트 드리븐 API")
    st.caption("키는 `~/Desktop/swing-screener/.env` 에 저장. 값은 로그하지 않는다.")
    with st.expander(
        "🔑 FMP / Alpaca / Groq",
        expanded=(
            (not env_settings.api_keys_ready())
            or not (_saved_keys.get("GROQ_API_KEY") or "").strip()
        ),
    ):
        fmp_in = st.text_input("FMP_API_KEY", key="api_input_FMP_API_KEY")
        alpaca_key_in = st.text_input("ALPACA_API_KEY", key="api_input_ALPACA_API_KEY")
        alpaca_secret_in = st.text_input("ALPACA_SECRET_KEY", key="api_input_ALPACA_SECRET_KEY", type="password")
        groq_in = st.text_input(
            "Groq API Key",
            key="api_input_GROQ_API_KEY",
            type="password",
            help="실무 4인과 DeepSeek가 없을 때의 CIO 대타. 스캔 자체는 이 키 없이 돈다.",
        )
        if "api_input_DEEPSEEK_API_KEY" not in st.session_state:
            st.session_state["api_input_DEEPSEEK_API_KEY"] = (_saved_keys.get("DEEPSEEK_API_KEY") or "")
        deepseek_in = st.text_input(
            "DeepSeek API Key (선택 — 없으면 Groq가 CIO를 대타)",
            key="api_input_DEEPSEEK_API_KEY",
            type="password",
        )
        if (deepseek_in or "").strip():
            os.environ["DEEPSEEK_API_KEY"] = deepseek_in.strip()
        if st.button("💾 키 저장", type="primary", width="stretch"):
            missing = env_settings.save_api_keys_and_apply({
                "FMP_API_KEY": fmp_in,
                "ALPACA_API_KEY": alpaca_key_in,
                "ALPACA_SECRET_KEY": alpaca_secret_in,
                "GROQ_API_KEY": groq_in,
                "DEEPSEEK_API_KEY": deepseek_in,
            })
            if missing:
                st.error("저장됨 · 누락: " + ", ".join(missing))
            else:
                engine.healthcheck()
                st.success("저장 완료.")
            st.rerun()
        if st.button("📂 기존 봇 .env에서 가져오기", width="stretch"):
            n = env_settings.import_from_legacy_bot()
            st.success(f"{n}개 키 복사.") if n else st.warning("복사할 키 없음.")
            st.rerun()
    st.html(_WATCHDOG_JS, width="content", unsafe_allow_javascript=True)


def _kick_scan() -> None:
    if not env_settings.api_keys_ready():
        st.session_state["need_keys"] = True
        return
    started = engine.start_scan_async()
    st.session_state.is_scanning = True if started else bool(SHARED.scan_running)


_SHOT_JS = """
<script>
(function() {
    try {
        window.speechSynthesis.cancel();
        const msg = new SpeechSynthesisUtterance("지금 사격");
        msg.lang = "ko-KR"; msg.rate = 1.1; msg.pitch = 1.0;
        window.speechSynthesis.speak(msg);
    } catch (e) {}
})();
</script>
"""

_OPEN_JS = """
<script>
(function() {
    try {
        window.speechSynthesis.cancel();
        const msg = new SpeechSynthesisUtterance("사령관님, 미국 장이 시작되었습니다.");
        msg.lang = "ko-KR"; msg.rate = 1.0; msg.pitch = 1.0;
        window.speechSynthesis.speak(msg);
    } catch (e) {}
})();
</script>
"""

_EXIT_VOICE_JS = """
<script>
(function() {
    try {
        window.speechSynthesis.cancel();
        const msg = new SpeechSynthesisUtterance("즉시 청산");
        msg.lang = "ko-KR"; msg.rate = 1.15; msg.pitch = 0.85;
        window.speechSynthesis.speak(msg);
    } catch (e) {}
})();
</script>
"""

_CHIME_JS = """
<script>
(function() {{
    try {{
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(880, ctx.currentTime);
        osc.frequency.setValueAtTime(1046.5, ctx.currentTime + 0.12);
        gain.gain.setValueAtTime(0.001, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.3, ctx.currentTime + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
        osc.connect(gain); gain.connect(ctx.destination);
        osc.start(); osc.stop(ctx.currentTime + 0.4);
        setTimeout(function() {{ try {{ ctx.close(); }} catch (e) {{}} }}, 1200);
    }} catch (e) {{}}
}})();
</script>
<!-- chime {token} -->
"""

_SIREN_JS = """
<script>
(function() {{
    try {{
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sawtooth";
        osc.frequency.setValueAtTime(420, ctx.currentTime);
        osc.frequency.linearRampToValueAtTime(780, ctx.currentTime + 0.22);
        osc.frequency.linearRampToValueAtTime(420, ctx.currentTime + 0.44);
        osc.frequency.linearRampToValueAtTime(780, ctx.currentTime + 0.66);
        osc.frequency.linearRampToValueAtTime(420, ctx.currentTime + 0.88);
        gain.gain.setValueAtTime(0.001, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.28, ctx.currentTime + 0.03);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 1.05);
        osc.connect(gain); gain.connect(ctx.destination);
        osc.start(); osc.stop(ctx.currentTime + 1.1);
        setTimeout(function() {{ try {{ ctx.close(); }} catch (e) {{}} }}, 1800);
    }} catch (e) {{}}
}})();
</script>
<!-- siren {token} -->
"""


def _usd_krw() -> float:
    px = SHARED.quote("KRW=X")
    return px if px > 500 else 1360.0


def _session_gate_rows(rows: list[dict]) -> list[dict]:
    """Strip 🎯 before 09:45 ET so the desk cannot display a live shot."""
    if radar.session_allows_fire_alert():
        return rows
    hold = f"🌙 장전 대기 ({radar.fire_unlock_time_label()})"
    out: list[dict] = []
    for raw in rows:
        row = dict(raw)
        if str(row.get("signal") or "").startswith("🎯"):
            row["signal"] = hold
        out.append(row)
    return out


def _session_meta() -> dict:
    sess = radar.us_session()
    return {
        **sess,
        "fire_alert_allowed": radar.session_allows_fire_alert(),
        "rth_elapsed_min": round(radar.rth_elapsed_minutes(), 2),
    }


def _live_hunt_payload() -> tuple[dict, list, list, list]:
    snap = SHARED.snapshot()
    earn = _session_gate_rows(ed.earn_rank_live(snap.get("earnings_targets") or [], SHARED.quote))
    held_raw = portfolio.load_portfolio()
    held_enriched = []
    for p in held_raw:
        m = portfolio.metrics(p)
        g = portfolio.guardian(m)
        held_enriched.append({
            **p,
            "exit_badge": g["badge"],
            "exit_order": g["order"],
            "exit_code": g["code"],
            "pct": round(float(m["pct"]), 2),
            "r_mult": round(float(m["r_mult"]), 2),
        })
    return snap, earn, held_enriched, portfolio.get_recently_closed_today()


def _assemble_ai_prompt(extra: str) -> str:
    """미리보기 전용. 위원회 함수는 호출하지 않는다."""
    _snap, earn, held, recently_closed = _live_hunt_payload()
    pool = _strategy_pool()
    earn_names = ", ".join(str(row.get("ticker") or "") for row in earn[:10]) or "없음"
    pead_names = ", ".join(str(row.get("ticker") or "") for row in pool["pead"][:7]) or "없음"
    rsi_names = ", ".join(str(row.get("ticker") or "") for row in pool["rsi2"][:7]) or "없음"
    held_names = ", ".join(str(row.get("ticker") or "") for row in held) or "없음"
    closed = ", ".join(recently_closed) or "없음"
    extra_line = (extra or "").strip() or "(추가 질문 없음)"
    return (
        "실행 버튼은 Groq 위원회(펀더멘털·수급·리스크·종합)를 호출한다.\n"
        f"보유: {held_names}\n"
        f"PEAD: {pead_names}\n"
        f"런업: {earn_names}\n"
        f"RSI2: {rsi_names}\n"
        f"오늘 청산: {closed}\n"
        f"추가 질문: {extra_line}"
    )


def _ring_new_shooters(table: pd.DataFrame, sig_col: str, hit_text: str, state_key: str) -> None:
    """Chime + '지금 사격' 음성, 티커당 ET 하루 1회. 09:45 ET 이전에는 절대 울리지 않는다."""
    if not radar.session_allows_fire_alert():
        return
    if state_key not in st.session_state:
        st.session_state[state_key] = set()
    current: set[str] = set()
    if table is not None and not table.empty and sig_col in table.columns:
        hits = table.loc[table[sig_col] == hit_text, "티커"]
        current = {str(t).upper().strip() for t in hits if str(t).strip()}
    active = {str(t).upper().strip() for t in st.session_state[state_key]}
    new_shooters = current - active

    day_key = f"{state_key}_day"
    today_s = ed.today_et().isoformat()
    seen_day = st.session_state.get(day_key)
    if not isinstance(seen_day, dict) or seen_day.get("date") != today_s:
        seen_day = {"date": today_s, "seen": set()}
    fresh = new_shooters - seen_day["seen"]
    seen_day["seen"] |= fresh
    st.session_state[day_key] = seen_day

    if fresh:
        token = f"{time.time():.3f}:{','.join(sorted(fresh))}"
        st.html(_CHIME_JS.format(token=token) + _SHOT_JS, width="content", unsafe_allow_javascript=True)
    st.session_state[state_key] = current


def _ring_keyed(hits: dict[str, str], state_key: str) -> None:
    """사이렌 + '즉시 청산'. 키(티커:코드)당 ET 하루 1회."""
    if not hits:
        return
    day_key = f"{state_key}_day"
    today_s = ed.today_et().isoformat()
    seen_day = st.session_state.get(day_key)
    if not isinstance(seen_day, dict) or seen_day.get("date") != today_s:
        seen_day = {"date": today_s, "seen": set()}
    current = {f"{str(tk).upper().strip()}:{code}" for tk, code in hits.items()}
    fresh = current - seen_day["seen"]
    seen_day["seen"] |= fresh
    st.session_state[day_key] = seen_day
    if fresh:
        token = f"{time.time():.3f}:{','.join(sorted(fresh))}"
        st.html(_SIREN_JS.format(token=token) + _EXIT_VOICE_JS, width="content", unsafe_allow_javascript=True)


def _ring_exit_sirens(rows: list[dict]) -> None:
    hits: dict[str, str] = {}
    for row in rows:
        g = portfolio.guardian(row)
        code = str(g.get("code") or "")
        if code in _EXIT_CODES:
            hits[str(g.get("ticker") or row.get("ticker") or "")] = code
    _ring_keyed(hits, "exit_siren")


def _speak_market_open(_now, session_key: str) -> None:
    if session_key != "RTH" or not radar.is_rth_open_chime_window():
        return
    today_str = radar.now_et().date().isoformat()
    if st.session_state.get("market_open_alert_day") == today_str:
        return
    st.session_state.market_open_alert_day = today_str
    st.html(_OPEN_JS + f"<!-- open {today_str} -->", width="content", unsafe_allow_javascript=True)


def _earn_table(ranked: list[dict]) -> pd.DataFrame:
    tp_label = f"목표가(+{config.EXIT_TP_PCT:g}%)"
    recs = [{
        "순위": int(r["rank"]), "티커": r["ticker"], "종목명": str(r.get("name") or "")[:18],
        "시총": ed.fmt_cap(r.get("market_cap")), "런업점수": round(float(r["score"]), 1),
        "실적발표일": str(r.get("earnings_date") or ""), "D-Day": f"D-{int(r['d_day'])}",
        "현재가": round(float(r.get("price") or 0.0), 2),
        tp_label: round(float(r.get("price") or 0.0) * (1 + config.EXIT_TP_PCT / 100.0), 2),
        "사격신호": r["signal"],
    } for r in ranked]
    return pd.DataFrame(recs)


def _render_earn_deck() -> None:
    snap = SHARED.snapshot()
    ranked = _session_gate_rows(ed.earn_rank_live(snap.get("earnings_targets") or [], SHARED.quote))
    note = str(snap.get("earnings_note") or "")
    st.markdown("**📅 실적 런업 Top 10**")
    if not ranked:
        _ring_new_shooters(pd.DataFrame(), "사격신호", ed.EARN_FIRE, "earn_shoot")
        st.caption(note or "스캔 전 IDLE. 상단 🚀 사냥터 구축.")
        return
    options = ["🎯 1위 자동추적"] + sorted(r["ticker"] for r in ranked)
    if st.session_state.get("earn_pick") not in options:
        st.session_state["earn_pick"] = options[0]
    pick = st.selectbox("브리핑 종목", options, key="earn_pick", label_visibility="collapsed")
    row_map = {r["ticker"]: r for r in ranked}
    brief = row_map.get(pick) or ranked[0]
    _fire_badge_markdown(brief)
    guide = ed.earnings_guide(brief, _usd_krw(), config.EARN_BUDGET_KRW)
    cls = {"go": "guide-go", "wait": "guide-wait", "chase": "guide-chase"}.get(guide["kind"], "guide-wait")
    st.markdown(f'<div class="{cls}">{guide["html"]}</div>', unsafe_allow_html=True)
    table = _earn_table(ranked)
    _ring_new_shooters(table, "사격신호", ed.EARN_FIRE, "earn_shoot")
    st.dataframe(table, hide_index=True, width="stretch", height=380)
    if note:
        st.caption(note)


def _fire_badge_markdown(row: dict) -> None:
    ticker = str(row.get("ticker") or "")
    d_day = row.get("d_day")
    d_txt = f"D-{int(d_day)}" if d_day is not None else ""
    sig = str(row.get("signal") or "")
    entry_window_open = radar.session_allows_fire_alert()
    entry_condition_met = sig.startswith("🎯")
    unlock = radar.fire_unlock_time_label()
    if entry_window_open and entry_condition_met:
        badge = f"🟢 [🎯 지금 즉시 사격! 토스 매수] — {sig}"
        color = "green"
    else:
        badge = f"🔴 [사격 대기] ({unlock} 해제)"
        color = "red"
    st.markdown(f":{color}[**{ticker}**  {d_txt}  {badge}]")


def _cockpit_busy() -> bool:
    return bool(
        getattr(SHARED, "heavy_scan_running", False)
        or getattr(SHARED, "scan_running", False)
        or st.session_state.get("ai_busy", False)
    )


def _strategy_pool() -> dict[str, list]:
    getter = getattr(SHARED, "safe_get", None)
    raw = getter("hunting_pool", None) if callable(getter) else getattr(SHARED, "hunting_pool", None)
    if not isinstance(raw, dict):
        return {"pead": [], "runup": [], "rsi2": []}
    return {
        "pead": [row for row in (raw.get("pead") or []) if isinstance(row, dict)],
        "runup": [row for row in (raw.get("runup") or []) if isinstance(row, dict)],
        "rsi2": [row for row in (raw.get("rsi2") or []) if isinstance(row, dict)],
    }


def _num_txt(value: object, digits: int = 1) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _render_price_badges(c: dict, strategy: str = "RUNUP") -> None:
    from screener.pricing import compute_price_badges

    def _f(value: object) -> float:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    row = dict(c)
    live = SHARED.quote(str(row.get("ticker") or ""))
    if live > 0:
        row["price"] = live
        row["current_price"] = live
        row.update(compute_price_badges(row, strategy))
    cur = _f(row.get("current_price") or row.get("price") or row.get("close") or 0.0)
    c = row
    tgt = _f(c.get("target_price") or (cur * 1.08 if cur else 0.0))
    stp = _f(c.get("stop_price") or (cur * 0.96 if cur else 0.0))
    up = c.get("upside_pct")
    if up is None and cur:
        up = max(0.0, (tgt - cur) / cur * 100.0)
    up = _f(up or 0.0)
    if cur <= 0:
        st.caption("💵 가격 데이터 수집 중 (다음 사냥터 구축 시 반영)")
        return
    st.markdown(
        f"💵 현재가: USD {cur:.2f}  |  🎯 목표가: USD {tgt:.2f} (+{up:.1f}%)  |  "
        f"🛑 손절선: USD {stp:.2f} (-4.0%)"
    )


def _render_fire_badge() -> None:
    if radar.session_allows_fire_alert():
        st.markdown(":green[**🟢 🎯 [지금 사격!]**]")
        return
    unlock = radar.fire_unlock_time_label()
    st.markdown(f":orange[**🌙 장전 대기 ({unlock} 해제)**]")


def _render_strategy_column(title: str, candidates: list[dict], extra_fields_fn=None, strategy: str = "RUNUP") -> None:
    st.markdown(f"#### {title}")
    if not candidates:
        scanned = getattr(SHARED, "hunting_pool_updated_ts", None)
        if scanned:
            st.info("💤 조건 충족 우량주 없음 (스캔 완료 · 주가 $10+ & 시총 우량주 필터 통과 0건)")
        else:
            st.info("스캔 전 IDLE. 상단 [🚀 사냥터 구축] 실행 필요")
        return
    for idx, row in enumerate(candidates):
        is_top = idx == 0
        with st.container(border=True):
            if is_top:
                st.markdown('<span class="hunt-top"></span>', unsafe_allow_html=True)
            header_cols = st.columns([3, 2])
            with header_cols[0]:
                prefix = "👑 " if is_top else ""
                st.markdown(f"**{prefix}{row.get('ticker')}**  점수 {_num_txt(row.get('score'))}")
            with header_cols[1]:
                _render_fire_badge()
            _render_price_badges(row, strategy)
            if extra_fields_fn:
                extra_fields_fn(row)


@st.fragment(run_every="1s")
def _hunt_deck_live() -> None:
    if not bool(getattr(SHARED, "radar_on", True)):
        st.caption("레이더 OFF — 시세 폴링 정지. 마지막 스냅샷만 표시.")
        return
    if _cockpit_busy():
        st.caption("⏸️ 스캔/AI 분석 진행 중 — 실시간 레이더 일시 정지")
        return
    st.markdown(
        """
        <style>
        div[data-testid="stVerticalBlockBorderWrapper"]:has(.hunt-top) {
            border: 2px solid gold !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    pool = _strategy_pool()
    updated_ts = getattr(SHARED, "hunting_pool_updated_ts", None)
    scan_errors = list(getattr(SHARED, "scan_errors", []) or [])
    st.subheader("🎯 사냥 데스크 — 3대 전략 콕핏")
    if updated_ts:
        st.caption(f"마지막 사냥터 구축: {time.strftime('%H:%M:%S', time.localtime(float(updated_ts)))}")
    if scan_errors:
        with st.expander(f"⚠️ 스캔 실패 {len(scan_errors)}건 — 클릭해서 확인"):
            for err in scan_errors:
                st.error(str(err))
    col_pead, col_runup, col_rsi2 = st.columns(3)
    with col_pead:
        _render_strategy_column(
            "🔥 PEAD (메인 1선발 · 120만원)",
            pool.get("pead", []),
            strategy="PEAD",
            extra_fields_fn=lambda row: st.caption(
                f"EPS 서프라이즈 +{_num_txt(row.get('eps_beat_pct'))}% / "
                f"갭업 +{_num_txt(row.get('gap_up_pct'))}% / D+{row.get('days_since_earnings', '—')}"
            ),
        )
    with col_runup:
        _render_strategy_column(
            "📅 런업 압축형 (보조 2선발 · 100만원)",
            pool.get("runup", []),
            strategy="RUNUP",
            extra_fields_fn=lambda row: st.caption(f"D-{row.get('d_day', '—')}"),
        )
    with col_rsi2:
        _render_strategy_column(
            "⚡ RSI2 반등 (현금회전 3선발 · 80만원)",
            pool.get("rsi2", []),
            strategy="RSI2",
            extra_fields_fn=lambda c: st.caption(
                f"시총: USD {(c.get('market_cap_b') or 0.0):.1f}B | "
                f"3일낙폭: {(c.get('drop_3d_pct') or 0.0):.1f}% | "
                f"200일선 이격: {(c.get('dist_sma200_pct') or 0.0):+.1f}%"
            ),
        )


def _shares_txt(shares: float) -> str:
    return str(int(shares)) if float(shares) == int(shares) else f"{shares:g}"


def _render_siren(rows: list[dict]) -> None:
    lead = portfolio.lead_guardian(rows)
    if lead is None:
        st.info("👀 [대기] 등록된 보유 종목이 없습니다.")
        return
    msg = f"{lead['badge']} {lead['ticker']} — {lead['order']}"
    alert = str(lead.get("alert") or "info")
    {"error": st.error, "warning": st.warning, "success": st.success}.get(alert, st.info)(msg)


def _render_summary(raw: list[dict]) -> None:
    if not raw:
        return
    usd_krw = _usd_krw()
    total_entry_krw = total_curr_krw = 0
    n_risk = 0
    for p in raw:
        m = portfolio.metrics(p)
        entry_fx = float(p.get("entry_fx") or 0) or usd_krw
        shares = float(m["shares"])
        entry_krw = entry_fx * float(m["entry_price"]) * shares
        total_entry_krw += entry_krw
        if m["has_quote"]:
            total_curr_krw += usd_krw * float(m["current_price"]) * shares
        else:
            total_curr_krw += entry_krw
        g = portfolio.guardian(m)
        if g["code"] in EXIT_REASON_LABEL:
            n_risk += 1
    pnl = total_curr_krw - total_entry_krw
    pnl_pct = (pnl / total_entry_krw * 100.0) if total_entry_krw else 0.0
    sign = "up" if pnl >= 0 else "dn"
    st.markdown(
        '<div class="summary-box">'
        f'<div class="summary-item"><div class="label">보유 종목</div><div class="value">{len(raw)}개</div></div>'
        f'<div class="summary-item"><div class="label">평가 손익</div>'
        f'<div class="value {sign}">{pnl:+,.0f}원 ({pnl_pct:+.2f}%)</div></div>'
        f'<div class="summary-item"><div class="label">오늘 조치 필요</div>'
        f'<div class="value {"dn" if n_risk else "up"}">{n_risk}건</div></div>'
        "</div>",
        unsafe_allow_html=True,
    )


def _render_position_card(p: dict) -> None:
    m = portfolio.metrics(p)
    g = portfolio.guardian(m)
    tk = m["ticker"]
    entry = float(m["entry_price"])
    shares = float(m["shares"])
    curr = float(m["current_price"])
    quoted = bool(m["has_quote"]) and curr > 0
    usd_krw = _usd_krw()
    entry_fx = float(p.get("entry_fx") or 0) or usd_krw

    entry_krw = int(round(entry * shares * entry_fx * 1.001))
    if quoted:
        curr_krw = int(round(curr * shares * usd_krw * 0.999))
        net_pnl_krw = curr_krw - entry_krw
        toss_pnl_pct = (net_pnl_krw / entry_krw) * 100 if entry_krw > 0 else 0.0
        net_pnl_usd = net_pnl_krw / usd_krw if usd_krw > 0 else 0.0
        pure_pct = ((curr - entry) / entry) * 100 if entry > 0 else 0.0
        now_html = (
            f"${curr:.2f} ({curr_krw:,}원) "
            f"(<span class='{'up' if pure_pct >= 0 else 'dn'}'>{pure_pct:+.2f}%</span>)"
        )
        fee_sign = "up" if net_pnl_krw >= 0 else "dn"
        pnl_html = (
            f'💰 <b>실수령 손익: <span class="{fee_sign}">{net_pnl_krw:+,}원 ({net_pnl_usd:+.2f}$)</span></b>'
            f' | 토스 실질 수익률: <b><span class="{fee_sign}">{toss_pnl_pct:+.2f}%</span></b>'
        )
        fee_note = f"순수 주가 등락 {pure_pct:+.2f}%"
    else:
        now_html = "시세 없음"
        pnl_html = "💰 <b>실수령 손익: 시세 없음. 0%로 계산하지 않습니다.</b>"
        fee_note = "시세 대기"

    strat_badge = {
        "PEAD": "📈 [PEAD]",
        "RUNUP": "📅 [런업 D-7~D-3]",
        "RSI2": "📉 [RSI2]",
    }.get(m["strategy"], "⚠️ [레거시]")
    d_day = m.get("d_day")
    if d_day is not None:
        cd_cls = "dn" if m.get("force_exit_d3") else "up"
        countdown = (
            f'<div class="diag"><span class="{cd_cls}">⏰ 실적 D-Day: D-{int(d_day)}일</span> '
            f'<span class="muted">({m["earnings_date"]})</span></div>'
        )
    else:
        countdown = ""
    sl_pct = {
        "PEAD": config.PEAD_SL_PCT,
        "RUNUP": config.RUNUP_SL_PCT,
        "RSI2": config.RSI2_SL_PCT,
    }.get(m.get("strategy"), config.RUNUP_SL_PCT)
    sl_px = entry * (1 - float(sl_pct) / 100.0)
    if m.get("strategy") == "RSI2" and m.get("sma5"):
        target_line = f"SMA5: ${float(m['sma5']):.2f}"
    elif m.get("strategy") == "PEAD":
        target_line = f"만기 {config.PEAD_MAX_HOLD_BDAYS}거래일"
    else:
        target_line = f"D-2 강제청산" if m.get("strategy") == "RUNUP" else "수동 검토"
    detail = (
        f'<div class="pos-metrics">{target_line} | '
        f'손절선: ${sl_px:.2f} (-{float(sl_pct):g}%) | {g["structure"]}</div>'
    )

    consensus = float(m["target_consensus"])
    wall = (
        f"🏛️ 월가 컨센서스: 목표가 ${consensus:.2f} (상승 여력 {float(m['upside']):+.1f}%) | 매수 {float(m['buy_ratio']):.0f}%"
        if consensus > 0 else "🏛️ 월가 컨센서스: —"
    )

    html = (
        f'<div class="pos-card">'
        f'<div class="pos-head">【 {tk} 】 {_shares_txt(shares)}주 | {strat_badge} | '
        f"진입 ${entry:.2f} ({entry_krw:,}원) | 현재 {now_html}</div>"
        f'<div class="diag">진단: {g["badge"]}</div>'
        f"{countdown}{detail}"
        f'<div class="pos-metrics">{pnl_html}</div>'
        f'<div class="muted">진입환율 {entry_fx:,.1f} ➔ 현재환율 {usd_krw:,.1f} · 수수료 0.2% 선반영 · {fee_note}</div>'
        f'<div class="diag-order">👉 전술 명령: "{g["order"]}"</div>'
        f'<div class="pos-wall">{wall}</div>'
        "</div>"
    )
    st.markdown(html, unsafe_allow_html=True)
    code = str(g.get("code") or "")
    d_show = m.get("d_day")
    d_txt = f"D-{d_show}" if d_show is not None else "D-?"
    if code in EXIT_REASON_LABEL:
        st.error(f"🚨 [지금 토스에서 전량 매도!] — 사유: {EXIT_REASON_LABEL[code]}")
    elif code == "HOLD":
        st.success(f"🟢 [{m.get('strategy')} 순항 — AI 감리로 매도 타이밍 판단]")
    elif code == "NO_QUOTE":
        st.warning("⚠️ 시세 조회 실패 — 수동 확인 필요")
    else:
        st.info("🔍 LEGACY 전략 — 수동 검토 필요 (자동판정 미지원)")
    if st.button(f"🗑️ {tk}만 청산", key=f"liq-{tk}", width="stretch"):
        portfolio.remove_ticker(tk)
        st.rerun(scope="fragment")


def _on_reg_ticker_change() -> None:
    ticker = str(st.session_state.get("reg_ticker_input") or "").strip().upper()
    cached = st.session_state.get("reg_auto_fetch") or {}
    if not ticker:
        st.session_state["reg_auto_fetch"] = None
        return
    if str(cached.get("ticker") or "") == ticker:
        return
    st.session_state["reg_auto_fetch"] = fetch_auto_fill_for_registration(ticker)
    filled = st.session_state["reg_auto_fetch"] or {}
    if filled.get("current_price"):
        st.session_state["reg_avg_price"] = float(filled["current_price"])
    if filled.get("earnings_date"):
        st.session_state["reg_earnings_date"] = str(filled["earnings_date"])


def _render_register_form() -> None:
    with st.expander("📌 포지션 등록", expanded=False):
        st.text_input(
            "티커 (칸을 벗어나면 FMP에서 현재가와 실적일을 채운다)",
            key="reg_ticker_input",
            on_change=_on_reg_ticker_change,
            placeholder="예: NET",
        )
        auto_fetch = st.session_state.get("reg_auto_fetch") or {}
        if auto_fetch.get("current_price"):
            st.caption(f"FMP 현재가 USD {auto_fetch['current_price']}")
        if auto_fetch.get("earnings_date"):
            st.caption(f"FMP 실적 발표일 {auto_fetch['earnings_date']}")
        if auto_fetch.get("error"):
            st.caption(f"자동 조회 경고: {auto_fetch['error']}")
        with st.form(key="add_position_form", clear_on_submit=True):
            strategy_label = st.radio(
                "전략 선택",
                list(STRATEGY_LABEL_TO_KEY.keys()),
                horizontal=True,
            )
            c1, c2 = st.columns(2)
            with c1:
                avg_price = st.number_input(
                    "평단가 (USD)",
                    min_value=0.0,
                    step=0.01,
                    format="%.2f",
                    key="reg_avg_price",
                )
            with c2:
                shares = st.number_input("수량", min_value=1, step=1, value=1)
            earnings_date_input = st.text_input(
                "실적 발표일 (YYYY-MM-DD, 비우면 자동 조회값)",
                key="reg_earnings_date",
            )
            submit = st.form_submit_button("📌 포지션 등록", width="stretch")
            if submit:
                strategy_key = STRATEGY_LABEL_TO_KEY.get(str(strategy_label), "")
                ticker_final = str(st.session_state.get("reg_ticker_input") or "").strip().upper()
                if not ticker_final:
                    st.error("등록 실패: 티커를 입력하라.")
                elif float(avg_price) <= 0:
                    st.error("등록 실패: 평단가가 없다. 자동 조회를 기다리거나 직접 입력하라.")
                elif not strategy_key:
                    st.error("등록 실패: 전략을 읽지 못했다.")
                else:
                    validation = validate_position_registration(
                        strategy_key, ticker_final, earnings_date_input,
                    )
                    if not validation["ok"]:
                        st.error(f"등록 실패: {validation['message']}")
                    else:
                        if validation.get("message"):
                            st.info(str(validation["message"]))
                        try:
                            pos = portfolio.add_position(
                                ticker_final,
                                float(avg_price),
                                float(shares),
                                strategy=strategy_key,
                                earnings_date=validation.get("earnings_date") or "",
                            )
                            engine.ensure_heartbeat()
                            extra = f" · 실적일 {pos['earnings_date']}" if pos.get("earnings_date") else ""
                            st.success(
                                f"{pos['ticker']} 등록 완료 ({strategy_key}). "
                                f"손절 USD {pos['stop_price']:.2f}{extra}"
                            )
                            st.session_state["reg_auto_fetch"] = None
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"등록 실패: {exc}")


@st.fragment(run_every="1s")
def _guard_dashboard_live() -> None:
    if _cockpit_busy():
        st.caption("⏸️ 스캔/AI 분석 진행 중 — 실시간 레이더 일시 정지")
        return
    if not bool(getattr(SHARED, "radar_on", True)):
        st.caption("레이더 OFF — 시세 폴링 정지. 마지막 스냅샷만 표시.")
    raw = portfolio.load_portfolio()
    if SHARED.portfolio_corrupt or SHARED.portfolio_error:
        st.error(f"🚨 [포트폴리오 파일 손상] {SHARED.portfolio_error or '복구 캐시 사용 중'}")
        _ring_keyed({"PORTFOLIO": "CORRUPT"}, "pf_corrupt")
        if not raw:
            st.error("복구 캐시가 없습니다. 토스 앱에서 포지션을 직접 확인하십시오. 빈 목록으로 위장하지 않습니다.")
            return
    if raw:
        engine.ensure_heartbeat()
    rows = [portfolio.metrics(p) for p in raw]
    if raw and bool(getattr(SHARED, "radar_on", True)):
        for p, m in zip(raw, rows):
            portfolio.maybe_persist_peak(m, p, raw)
    _ring_exit_sirens(rows)
    _render_siren(rows)
    _render_summary(raw)
    if not raw:
        st.markdown(
            '<div class="muted">보유 없음. 하단 등록 폼에서 티커·평단·수량만 입력하십시오.</div>',
            unsafe_allow_html=True,
        )
        return
    n_cols = 3 if len(raw) >= 3 else max(1, len(raw))
    cols = st.columns(n_cols, gap="medium")
    for i, p in enumerate(raw):
        with cols[i % n_cols]:
            _render_position_card(p)


snap = SHARED.snapshot()
st.session_state.is_scanning = bool(snap.get("is_scanning") or snap.get("scan_running"))


def _watch_icon(delay: float) -> str:
    if delay < 2.0:
        return "🟢"
    if delay < 5.0:
        return "🟡"
    return "🔴"


def _remember_review(review: dict) -> None:
    book = st.session_state.get("ticker_reviews")
    if not isinstance(book, dict):
        book = {}
    ticker = str(review.get("ticker") or "UNKNOWN")
    book[ticker] = review
    st.session_state["ticker_reviews"] = book
    st.session_state["ticker_review_focus"] = ticker
    st.session_state["ticker_review_result"] = review


def _failed_review(ticker: str, exc: Exception) -> dict:
    return {
        "ticker": ticker,
        "verdict_code": "WAIT_CONDITIONAL",
        "verdict_label": "🟡 [조건부 대기 진입]",
        "reason_line": f"심의 호출 실패: {exc}",
        "entry_range": "정보 없음",
        "target_price": "정보 없음",
        "stop_price": "정보 없음",
        "position_size": "정보 없음",
        "expected_pnl": "정보 없음",
        "staff_4lines": {},
        "order_copy_text": f"[{ticker}] 심의 실패",
        "cio_report_full": f"심의 호출 실패: {exc}",
        "staff_reports": {},
        "errors": [str(exc)[:400]],
    }


def _hx(value: object) -> str:
    return html.escape(str(value or "정보 없음").replace("$", "USD "))


def _cockpit_css() -> None:
    st.markdown(
        """
        <style>
        .cockpit-card {
            background-color: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 8px;
            padding: 10px 14px;
            margin-bottom: 8px;
            font-size: 15px;
            line-height: 1.6;
            word-break: break-word;
            overflow-wrap: anywhere;
        }
        .cockpit-row {
            display: flex;
            flex-wrap: wrap;
            gap: 6px 18px;
            font-size: 14px;
        }
        .cockpit-label { color: rgba(255,255,255,0.55); font-size: 13px; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_audit_cards(audit: object) -> None:
    if isinstance(audit, str):
        _show_md(audit)
        return
    if not isinstance(audit, dict):
        st.caption("감리 결과가 비어 있다.")
        return
    cards = ai_advisor.collapse_audit_cards(audit.get("position_cards") or [])
    if not cards:
        st.caption("현재 보유 포지션이 없거나 카드가 비었다.")
    verdict_bg = {"HOLD": "rgba(0,230,118,0.10)", "TRIM": "rgba(255,214,0,0.10)", "EXIT": "rgba(255,82,82,0.10)"}
    verdict_border = {"HOLD": "#00e676", "TRIM": "#ffd600", "EXIT": "#ff5252"}
    for card in cards:
        if not isinstance(card, dict):
            continue
        code = str(card.get("verdict_code") or "HOLD")
        bg = verdict_bg.get(code, "rgba(255,255,255,0.03)")
        border = verdict_border.get(code, "rgba(255,255,255,0.12)")
        st.markdown(
            f"""
<div class="cockpit-card" style="background-color:{bg}; border-left: 4px solid {border};">
  <div style="font-size:16px; font-weight:700; margin-bottom:4px;">{_hx(card.get("verdict_label"))}</div>
  <div class="cockpit-row">
    <span><b>{_hx(card.get("ticker"))}</b></span>
    <span class="cockpit-label">평단가</span> <span>{_hx(card.get("avg_price"))}</span>
    <span class="cockpit-label">목표가</span> <span style="color:#00e676; font-weight:bold;">{_hx(card.get("target_price"))}</span>
    <span class="cockpit-label">손절선</span> <span style="color:#ff5252; font-weight:bold;">{_hx(card.get("stop_price"))}</span>
    <span class="cockpit-label">기한</span> <span style="color:#ffd600; font-weight:bold;">{_hx(card.get("deadline"))}</span>
  </div>
  <div style="margin-top:4px; font-size:14px;">행동: {_hx(card.get("action_line"))}</div>
</div>
""",
            unsafe_allow_html=True,
        )
    with st.expander("🔍 포트폴리오 감리 상세 리포트", expanded=False):
        _show_md(audit.get("full_report") or "데이터 없음")
        staff = audit.get("staff_reports") if isinstance(audit.get("staff_reports"), dict) else {}
        for key, label in (("fundamental", "📊 펀더멘털"), ("flow", "📈 수급"), ("risk", "⚠️ 리스크")):
            st.markdown(f"**{label}**")
            _show_md(staff.get(key) or "정보 없음")


def _render_verdict_card(review: dict) -> None:
    verdict_code = str(review.get("verdict_code") or "")
    verdict_label = str(review.get("verdict_label") or "🟡 [조건부 대기 진입]")
    verdict_bg = {
        "ENTER_NOW": "rgba(0,230,118,0.10)",
        "WAIT_CONDITIONAL": "rgba(255,214,0,0.10)",
        "NO_ENTRY": "rgba(255,82,82,0.10)",
    }
    verdict_border = {"ENTER_NOW": "#00e676", "WAIT_CONDITIONAL": "#ffd600", "NO_ENTRY": "#ff5252"}
    bg = verdict_bg.get(verdict_code, "rgba(255,255,255,0.03)")
    border = verdict_border.get(verdict_code, "rgba(255,255,255,0.12)")
    pnl_raw = str(review.get("expected_pnl") or "정보 없음").replace("$", "USD ")
    parts = re.split(r"/", pnl_raw, maxsplit=1) if "/" in pnl_raw else [pnl_raw, ""]
    pnl_html = f'<span style="color:#00e676; font-weight:bold;">{html.escape(parts[0].strip())}</span>'
    if len(parts) > 1 and parts[1].strip():
        pnl_html += f' / <span style="color:#ff5252; font-weight:bold;">{html.escape(parts[1].strip())}</span>'
    staff_4 = review.get("staff_4lines") if isinstance(review.get("staff_4lines"), dict) else {}
    st.markdown(
        f"""
<div class="cockpit-card" style="background-color:{bg}; border-left: 4px solid {border};">
  <div style="font-size:18px; font-weight:700; margin-bottom:6px;">{_hx(verdict_label)} — {_hx(review.get("ticker"))}</div>
  <div style="margin-bottom:8px; font-size:14px;">판결 사유: {_hx(review.get("reason_line"))}</div>
  <div class="cockpit-row" style="flex-direction:column; align-items:flex-start;">
    <div><span class="cockpit-label">진입 타점 (22:45 KST 이후) </span>
      <span style="color:#40c4ff; font-weight:bold;">{_hx(review.get("entry_range"))}</span></div>
    <div><span class="cockpit-label">목표 익절가 </span>
      <span style="color:#00e676; font-weight:bold;">{_hx(review.get("target_price"))}</span></div>
    <div><span class="cockpit-label">강제 손절가 </span>
      <span style="color:#ff5252; font-weight:bold;">{_hx(review.get("stop_price"))}</span></div>
    <div><span class="cockpit-label">권장 투입 비중 </span>
      <span style="color:#ffd600; font-weight:bold;">{_hx(review.get("position_size"))}</span></div>
    <div><span class="cockpit-label">예상 손익 </span> {pnl_html}</div>
  </div>
</div>
<div class="cockpit-card">
  <div>🌐 <b>매크로</b>: {_hx(staff_4.get("macro"))}</div>
  <div>📊 <b>펀더멘털</b>: {_hx(staff_4.get("fundamental"))}</div>
  <div>📈 <b>테크니컬</b>: {_hx(staff_4.get("technical"))}</div>
  <div>⚠️ <b>리스크</b>: {_hx(staff_4.get("risk"))}</div>
</div>
""",
        unsafe_allow_html=True,
    )
    st.markdown("#### 원클릭 주문 복사")
    st.code(str(review.get("order_copy_text") or "").replace("$", "USD "), language="text")
    errs = review.get("errors") or []
    if errs:
        st.caption("⚠️ 일부 단계 경고: " + "; ".join(str(item)[:180] for item in errs))
    with st.expander("🔍 4인 실무진 상세 회의록 및 CIO 전문", expanded=False):
        st.markdown("##### CIO 전체 원문")
        _show_md(review.get("cio_report_full") or review.get("cio_report") or "데이터 없음")
        staff_reports = review.get("staff_reports") if isinstance(review.get("staff_reports"), dict) else {}
        for key, label in (
            ("macro", "🌐 매크로"),
            ("fundamental", "📊 펀더멘털"),
            ("technical", "📈 테크니컬"),
            ("risk", "⚠️ 리스크"),
        ):
            st.markdown(f"**{label}**")
            _show_md(staff_reports.get(key) or "정보 없음")


def _show_md(text: object) -> None:
    st.markdown(str(text or "").replace("$", "USD "))


def _ai_committee_controls(controls_locked: bool) -> None:
    """클릭은 1초 헤더 안에서만 받는다. 결재판 본문은 데스크에 한 번만 그린다."""
    import traceback

    from screener import manual_lookup

    st.markdown("##### 🏆 오늘 밤 최우선 사격 후보 Top 3 요약")
    top3_clicked = st.button(
        "⚡ 오늘 밤 Top 3 즉시 요약 브리핑 소집",
        key="btn_run_top3_summary_exec",
        width="stretch",
        disabled=controls_locked,
    )
    st.markdown("##### 심의 소집 — Groq 실무 4인 + DeepSeek CIO")
    audit_clicked = st.button(
        "🛡️ 감리 실행 (DeepSeek CIO)",
        key="portfolio_audit_btn",
        width="stretch",
        disabled=controls_locked,
    )
    pool = _strategy_pool()

    def _run_single_review(strategy: str, row: dict, ticker: str) -> None:
        st.session_state["ai_busy"] = True
        status_box = st.status(f"{ticker} 5인 위원회 소집 중...", expanded=True)
        try:
            review = ai_advisor.run_ticker_review(
                ticker_data=row,
                regime_data=SHARED.regime if isinstance(SHARED.regime, dict) else {},
                on_progress=status_box.write,
            )
            _remember_review(review)
            status_box.update(label="✅ 심의 완료", state="complete", expanded=False)
        except Exception as exc:  # noqa: BLE001
            _remember_review(_failed_review(ticker, exc))
            status_box.update(label="❌ 심의 실패", state="error", expanded=True)
        finally:
            st.session_state["ai_busy"] = False
        st.rerun(scope="app")

    def _candidate_caption(strategy: str, row: dict) -> str:
        if strategy == "PEAD":
            return f"서프라이즈 +{_num_txt(row.get('eps_beat_pct'))}%"
        if strategy == "RUNUP":
            return f"D-{row.get('d_day', '—')} / 상승여력 {_num_txt(row.get('upside_pct'))}%"
        return f"3일낙폭 {_num_txt(row.get('drop_3d_pct'))}%"

    strategy_sections = (
        ("🔥 PEAD 후보", "PEAD", pool.get("pead") or []),
        ("📅 런업 후보", "RUNUP", pool.get("runup") or []),
        ("⚡ RSI2 후보", "RSI2", pool.get("rsi2") or []),
    )
    any_candidates = any(rows for _, _, rows in strategy_sections)

    st.markdown("##### 🎯 사냥터 발굴 종목 원클릭 심의소")
    if not any_candidates:
        st.caption("후보 없음. 사냥터 구축 뒤에 전략별 후보가 여기 뜬다.")
    else:
        for label, strategy, rows in strategy_sections:
            if not rows:
                continue
            st.markdown(f"**{label}** ({len(rows)}건)")
            chunk_size = 3
            for start in range(0, len(rows), chunk_size):
                chunk = rows[start:start + chunk_size]
                grid_cols = st.columns(len(chunk))
                for col, row in zip(grid_cols, chunk):
                    ticker = str(row.get("ticker") or "")
                    price_txt = _num_txt(row.get("current_price") or row.get("price"), 2)
                    with col:
                        st.caption(_candidate_caption(strategy, row))
                        if st.button(
                            f"🔍 {ticker} (${price_txt}) 심의",
                            key=f"review_grid_{strategy}_{ticker}_{start}",
                            width="stretch",
                            disabled=controls_locked or not ticker,
                        ):
                            _run_single_review(strategy, row, ticker)
    custom_ticker = st.text_input(
        "수동 티커 (예: TSLA, PLTR)",
        key="custom_ticker_input",
    ).upper().strip()
    manual_clicked = st.button(
        "🔍 긴급 심의 소집",
        key="manual_review_btn",
        width="stretch",
        disabled=controls_locked or not custom_ticker,
    )
    if top3_clicked:
        st.session_state["ai_busy"] = True
        status_box = st.status("실무 4인이 사냥터 전체를 읽고 CIO가 Top 3를 고르는 중...", expanded=True)
        try:
            pool_data = SHARED.safe_get("hunting_pool") or {}
            regime_data = SHARED.safe_get("regime") or {}
            st.session_state["top3_summary_result"] = ai_advisor.run_top3_summary_briefing(
                pool_data if isinstance(pool_data, dict) else {},
                regime_data if isinstance(regime_data, dict) else {},
                on_progress=status_box.write,
            )
            status_box.update(label="✅ Top 3 요약 완료", state="complete", expanded=False)
        except Exception as exc:  # noqa: BLE001
            st.session_state["top3_summary_result"] = {"top3": [], "error": str(exc)[:400]}
            status_box.update(label="❌ Top 3 요약 실패", state="error", expanded=True)
        finally:
            st.session_state["ai_busy"] = False
        st.rerun(scope="app")
    if audit_clicked:
        st.session_state["ai_busy"] = True
        status_box = st.status("포트폴리오 감리 시작...", expanded=True)
        try:
            _snap, _earn, held, _closed = _live_hunt_payload()
            result = ai_advisor.run_portfolio_audit(
                regime_data=_snap.get("regime") or {},
                portfolio_data=held,
                on_progress=status_box.write,
            )
            st.session_state["portfolio_audit_result"] = result or "❌ Groq 본문이 비었다."
            status_box.update(label="✅ 감리 완료", state="complete", expanded=False)
        except Exception as exc:  # noqa: BLE001
            st.session_state["portfolio_audit_result"] = f"❌ 감리 실패: {exc}"
            st.session_state["portfolio_audit_error_trace"] = traceback.format_exc()
            status_box.update(label="❌ 감리 실패", state="error", expanded=True)
        finally:
            st.session_state["ai_busy"] = False
        st.rerun(scope="app")
    if manual_clicked and custom_ticker:
        st.session_state["ai_busy"] = True
        status_box = st.status(f"{custom_ticker} 데이터 수집 및 위원회 소집 중...", expanded=True)
        try:
            status_box.write("시세, 프로필, 실적일, 컨센서스 수집 중...")
            ticker_data = manual_lookup.fetch_manual_ticker_data(custom_ticker)
            review = ai_advisor.run_ticker_review(
                ticker_data=ticker_data,
                regime_data=SHARED.regime if isinstance(SHARED.regime, dict) else {},
                on_progress=status_box.write,
            )
            _remember_review(review)
            status_box.update(label="✅ 심의 완료", state="complete", expanded=False)
        except Exception as exc:  # noqa: BLE001
            _remember_review(_failed_review(custom_ticker, exc))
            status_box.update(label="❌ 심의 실패", state="error", expanded=True)
        finally:
            st.session_state["ai_busy"] = False
        st.rerun(scope="app")


_WARROOM_SCAN_TIMEOUT_SEC = 180.0  # 헤비 스캔 체감이 3분을 넘으면 이 숫자만 올려라.


def _warroom_controls(controls_locked: bool) -> None:
    """22:30 원클릭 지휘소 상태기계. 클릭은 1초 헤더 안에서만 받는다.
    스캔 → 퀵스캔 → 포트폴리오 감리 → Top3 요약을 자동 연쇄 실행한다.
    각 단계는 기존 ai_advisor/engine 함수를 그대로 재사용하며, 새 AI 호출을 만들지 않는다.
    """
    stage = str(st.session_state.get("warroom_stage") or "idle")
    stage_label = {
        "idle": "대기 중",
        "scanning": "1/4 사냥터 전수 스캔 중...",
        "quickscan": "2/4 본장 체결가 동기화 중...",
        "audit": "3/4 내 포트폴리오 긴급 생사 판결 중...",
        "topreview": "4/4 오늘 밤 Top 3 정밀 심의 중...",
        "done": "✅ 작전 완료",
    }

    st.markdown("##### ⚡ 22:30 오늘 밤 총괄 작전")
    if stage != "idle":
        st.caption(f"진행 상태: {stage_label.get(stage, stage)}")
    errors = st.session_state.get("warroom_errors") or []
    if errors:
        st.caption("⚠️ " + " / ".join(str(e)[:150] for e in errors))

    run_clicked = st.button(
        "🚀 22:30 오늘 밤 총괄 작전 개시 (스캔 ➔ 포트폴리오 감리 ➔ Top 3 정밀 추천)",
        key="btn_run_oneclick_warroom",
        width="stretch",
        type="primary",
        disabled=controls_locked or stage not in ("idle", "done"),
    )

    if run_clicked:
        st.session_state["warroom_stage"] = "scanning"
        st.session_state["warroom_errors"] = []
        st.session_state["warroom_scan_started_ts"] = time.time()
        started = engine.start_scan_async()
        if not started and not getattr(SHARED, "heavy_scan_running", False):
            st.session_state["warroom_stage"] = "idle"
            st.session_state["warroom_errors"] = ["스캔 시작 실패 — 사이드바 API 키를 확인하라."]
        st.rerun(scope="app")
        return

    if stage == "scanning":
        started_ts = float(st.session_state.get("warroom_scan_started_ts") or time.time())
        if getattr(SHARED, "heavy_scan_running", False):
            if time.time() - started_ts > _WARROOM_SCAN_TIMEOUT_SEC:
                errs = list(st.session_state.get("warroom_errors") or [])
                errs.append(f"스캔이 {int(_WARROOM_SCAN_TIMEOUT_SEC)}초를 넘겨 강제 종료, 현재 풀로 계속 진행한다.")
                st.session_state["warroom_errors"] = errs
                st.session_state["warroom_stage"] = "quickscan"
                st.rerun(scope="app")
            return
        st.session_state["warroom_stage"] = "quickscan"
        st.rerun(scope="app")
        return

    if stage == "quickscan":
        try:
            refresh = getattr(engine, "refresh_pool_snapshot", None)
            if callable(refresh):
                refresh()
                SHARED.last_quick_scan_ts = time.time()
        except Exception as exc:  # noqa: BLE001
            errs = list(st.session_state.get("warroom_errors") or [])
            errs.append(f"체결가 동기화 실패(무시하고 진행): {exc}")
            st.session_state["warroom_errors"] = errs
        st.session_state["warroom_stage"] = "audit"
        st.rerun(scope="app")
        return

    if stage == "audit":
        st.session_state["ai_busy"] = True
        status_box = st.status("제1부 — 내 포트폴리오 긴급 생사 판결 중...", expanded=True)
        try:
            _snap, _earn, held, _closed = _live_hunt_payload()
            result = ai_advisor.run_portfolio_audit(
                regime_data=_snap.get("regime") or {},
                portfolio_data=held,
                on_progress=status_box.write,
            )
            st.session_state["portfolio_audit_result"] = result or "❌ Groq 본문이 비었다."
            status_box.update(label="✅ 제1부 완료", state="complete", expanded=False)
        except Exception as exc:  # noqa: BLE001
            errs = list(st.session_state.get("warroom_errors") or [])
            errs.append(f"포트폴리오 감리 실패: {exc}")
            st.session_state["warroom_errors"] = errs
            st.session_state["portfolio_audit_result"] = f"❌ 감리 실패: {exc}"
            status_box.update(label="❌ 제1부 실패", state="error", expanded=True)
        finally:
            st.session_state["ai_busy"] = False
        st.session_state["warroom_stage"] = "topreview"
        st.rerun(scope="app")
        return

    if stage == "topreview":
        st.session_state["ai_busy"] = True
        status_box = st.status("제2부 — 오늘 밤 사격 타겟 Top 3 정밀 심의 중...", expanded=True)
        try:
            pool_data = SHARED.safe_get("hunting_pool") or {}
            regime_data = SHARED.safe_get("regime") or {}
            st.session_state["top3_summary_result"] = ai_advisor.run_top3_summary_briefing(
                pool_data if isinstance(pool_data, dict) else {},
                regime_data if isinstance(regime_data, dict) else {},
                on_progress=status_box.write,
            )
            status_box.update(label="✅ 제2부 완료", state="complete", expanded=False)
        except Exception as exc:  # noqa: BLE001
            errs = list(st.session_state.get("warroom_errors") or [])
            errs.append(f"Top3 심의 실패: {exc}")
            st.session_state["warroom_errors"] = errs
            st.session_state["top3_summary_result"] = {"top3": [], "error": str(exc)[:400]}
            status_box.update(label="❌ 제2부 실패", state="error", expanded=True)
        finally:
            st.session_state["ai_busy"] = False
        st.session_state["warroom_stage"] = "done"
        st.session_state["warroom_completed_ts"] = time.time()
        payload = {
            "audit": st.session_state.get("portfolio_audit_result"),
            "top3": st.session_state.get("top3_summary_result"),
            "ts": st.session_state["warroom_completed_ts"],
            "errors": st.session_state.get("warroom_errors") or [],
        }
        if not SHARED.safe_set("warroom_last_result", payload):
            SHARED.warroom_last_result = payload
        st.rerun(scope="app")
        return


def _render_top3_briefing() -> None:
    result = st.session_state.get("top3_summary_result")
    if not isinstance(result, dict):
        st.caption("위 헤더의 [⚡ 오늘 밤 Top 3 즉시 요약 브리핑 소집]을 누르면 카드가 여기 붙는다.")
        return
    reviewed = int(result.get("reviewed") or 0)
    error = str(result.get("error") or "").strip()
    rows = result.get("top3") or []
    if reviewed:
        st.caption(f"실무 4인 + CIO가 사냥터 {reviewed}종을 읽고 고른 순위다.")
    if error and not rows:
        st.warning(str(error).replace("$", "USD "))
    elif error:
        st.caption(str(error).replace("$", "USD "))
    medals = ["🥇 1위", "🥈 2위", "🥉 3위"]
    cols = st.columns(min(len(rows), 3)) if rows else []

    def _px(value: object) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    for idx, item in enumerate(rows[:3]):
        card = item if isinstance(item, dict) else {}
        with cols[idx]:
            summary = _hx(card.get("summary") or "").replace("\n", "<br>")
            st.markdown(
                f"""<div style="border:1px solid #444; border-radius:8px; padding:12px; background:#1e1e1e;">
                <h4>{medals[idx]} {_hx(card.get("ticker"))} <span style="font-size:14px; color:#aaa;">({_hx(card.get("strategy"))})</span></h4>
                <p style="margin:4px 0;"><b>현재가:</b> USD {_px(card.get("current_price")):.2f} | <b>목표가:</b> <span style="color:#00e676;">USD {_px(card.get("target_price")):.2f}</span> | <b>손절:</b> <span style="color:#ff5252;">USD {_px(card.get("stop_price")):.2f}</span></p>
                <p style="font-size:13px; color:#ddd; margin-top:8px;">{summary}</p>
                </div>""",
                unsafe_allow_html=True,
            )
    reports = result.get("staff_reports") if isinstance(result.get("staff_reports"), dict) else {}
    if reports:
        labels = (
            ("macro", "매크로"),
            ("fundamental", "펀더멘털"),
            ("technical", "테크니컬"),
            ("risk", "리스크"),
        )
        with st.expander("실무 4인 메모"):
            for key, label in labels:
                st.markdown(f"**{label}**")
                st.text(str(reports.get(key) or "응답 없음").replace("$", "USD "))


def _render_ai_desk() -> None:
    _cockpit_css()
    st.subheader("🧠 AI 투자심의위원회 — DeepSeek 총괄 CIO")
    st.markdown("### 🏆 오늘 밤 최우선 사격 후보 Top 3 요약")
    _render_top3_briefing()
    st.markdown("---")
    st.caption("버튼은 위 헤더에 있다. 감리와 티커 심의는 서로 결과를 지우지 않는다.")
    col_left, col_right = st.columns([1, 1.3])
    with col_left:
        st.markdown("### 🛡️ 내 포트폴리오 정밀 감리")
        audit = st.session_state.get("portfolio_audit_result")
        if audit:
            _render_audit_cards(audit)
            if st.session_state.get("portfolio_audit_error_trace"):
                with st.expander("🔧 상세 에러 트레이스"):
                    st.code(st.session_state["portfolio_audit_error_trace"])
        else:
            st.info("아직 실행된 감리가 없다. 위 [🛡️ 감리 실행]을 눌러라.")
    with col_right:
        st.markdown("### 📋 티커 결재판")
        book = st.session_state.get("ticker_reviews")
        if not isinstance(book, dict) or not book:
            st.info("위 Top 1 카드 또는 수동 티커로 심의를 소집하면 결재판이 여기 쌓인다.")
            return
        focus = str(st.session_state.get("ticker_review_focus") or "")
        latest = st.session_state.get("ticker_review_result")
        if isinstance(latest, dict) and latest.get("ticker"):
            focus = str(latest.get("ticker"))
        ordered = [focus] + [name for name in book if name != focus] if focus in book else list(book)
        for name in ordered:
            st.markdown(f"##### {name}")
            _render_verdict_card(book.get(name) or {})


def _render_warroom_desk() -> None:
    _cockpit_css()
    st.subheader("⚡ 22:30 긴급 원클릭 지휘소")
    st.caption("버튼은 위 헤더에 있다. 한 번 눌러두면 탭을 옮겨도 결과는 그대로 남는다.")

    audit = st.session_state.get("portfolio_audit_result")
    top3 = st.session_state.get("top3_summary_result")
    completed_ts = st.session_state.get("warroom_completed_ts")

    if audit is None and top3 is None:
        last = SHARED.safe_get("warroom_last_result")
        if isinstance(last, dict) and last.get("ts"):
            audit = last.get("audit")
            top3 = last.get("top3")
            completed_ts = last.get("ts")
            st.session_state["portfolio_audit_result"] = audit
            st.session_state["top3_summary_result"] = top3
            st.session_state["warroom_completed_ts"] = completed_ts
            st.info("이전 세션의 마지막 작전 결과를 복구했다.")

    if completed_ts:
        held = portfolio.load_portfolio()
        by_strategy: dict[str, int] = {}
        for row in held:
            key = str(row.get("strategy") or "LEGACY")
            by_strategy[key] = by_strategy.get(key, 0) + 1
        reg = SHARED.safe_get("regime") or {}
        reg_state = str(reg.get("state") or reg.get("label") or "UNKNOWN")
        slots_total = 3  # 전략당 동시 1포지션 가정. 가디언/등록 상한이 아니다.
        slots_used = sum(1 for key in ("PEAD", "RUNUP", "RSI2") if by_strategy.get(key))
        slots_free = max(0, slots_total - slots_used)
        st.markdown(
            f"""<div class="cockpit-card">
  <b>오늘 밤 종합 작전 요약</b> · 완료 {time.strftime('%H:%M:%S', time.localtime(float(completed_ts)))}<br>
  국면: <b>{_hx(reg_state)}</b> · 가용 슬롯: <b>{slots_free}/{slots_total}</b>
  (PEAD {by_strategy.get('PEAD', 0)}/1 · RUNUP {by_strategy.get('RUNUP', 0)}/1 · RSI2 {by_strategy.get('RSI2', 0)}/1) ·
  총 예산 {config.TOTAL_BUDGET_KRW:,}원
</div>""",
            unsafe_allow_html=True,
        )
    else:
        st.info("아직 실행된 작전이 없다. 위 [🚀 22:30 오늘 밤 총괄 작전 개시]를 눌러라.")
        return

    st.markdown("---")
    st.markdown("### 🛡️ 제1부 — 내 보유 계좌 긴급 생사 판결")
    if audit:
        _render_audit_cards(audit)
    else:
        st.caption("보유 포지션이 없거나 감리 결과가 비었다.")

    st.markdown("---")
    st.markdown("### 🎯 제2부 — 오늘 밤 사격 타겟 Top 3 정밀 결재판")
    _render_top3_briefing()


@st.fragment(run_every="1s", key="header_live")
def _header_live() -> None:
    now = radar.now_kst()
    sess = radar.kst_session(now)
    _speak_market_open(now, sess["key"])
    short = {"PRE": "🌙 프리마켓", "RTH": "☀️ 정규장", "AH": "🌆 애프터마켓", "CLOSED": "💤 휴장"}
    clock = f'{short.get(sess["key"], "💤 휴장")} · {now.strftime("%H:%M:%S")} KST · {sess.get("et_clock") or ""} ET'
    telem = SHARED.telemetry()
    now_ts = time.time()
    fmp_delay = max(0.0, now_ts - float(telem["fmp_last_time"]))
    alpaca_delay = max(0.0, now_ts - float(telem["alpaca_last_time"]))
    fmp_text = f'{_watch_icon(fmp_delay)} FMP: {fmp_delay:.1f}s 전 [{int(telem["fmp_tick_count"]):,}건]'
    alpaca_text = f'{_watch_icon(alpaca_delay)} IEX: {alpaca_delay:.1f}s 전 [{int(telem["alpaca_tick_count"]):,}틱]'
    reg = telem.get("regime") or {}
    color = str(reg.get("color") or "YELLOW")
    state = str(reg.get("state") or reg.get("label") or "UNKNOWN")
    dist_n = reg.get("distribution_days")
    dist_txt = f" · 분산 {int(dist_n)}" if isinstance(dist_n, int) and state != "UNKNOWN" else ""
    phase = str(telem.get("scan_phase") or "")
    _bind_shared_flags()
    ai_busy = bool(st.session_state.get("ai_busy", False))
    running = bool(
        telem.get("scan_running")
        or telem.get("heavy_scan_running")
        or getattr(SHARED, "heavy_scan_running", False)
    )
    controls_locked = running or ai_busy
    fire_ok = radar.session_allows_fire_alert()
    interlock = "🔓 사격허용" if fire_ok else f"🔒 사격잠금({radar.fire_unlock_time_label()})"
    banner = str(telem.get("banner") or "")
    if running:
        banner = f"⏳ 스캔 {phase} · {banner}"
    st.markdown(
        '<div class="telem-row">'
        f'<div class="badge {sess["css"]}">{clock}</div>'
        f'<div class="badge">{fmp_text}</div>'
        f'<div class="badge">{alpaca_text}</div>'
        f'<div class="badge reg-{color}">🚦 {state}{dist_txt}</div>'
        f'<div class="badge">{interlock}</div>'
        f'<div class="banner">{banner}</div>'
        "</div>",
        unsafe_allow_html=True,
    )
    col_a, col_b, col_c = st.columns([1.2, 1.2, 1])
    with col_a:
        if st.button(
            "🚀 사냥터 구축 (헤비 스캔)",
            width="stretch",
            type="primary",
            key="hdr-heavy",
            disabled=controls_locked,
        ):
            _kick_scan()
            if st.session_state.get("need_keys"):
                st.rerun()
        if running:
            prog = max(0.0, min(1.0, float(telem.get("scan_progress") or SHARED.heavy_scan_progress or 0.0)))
            status = str(telem.get("scan_status_text") or "유망주 풀 구축 중...")
            st.progress(prog, text=f"⏳ {status} ({int(prog * 100)}%)")
    with col_b:
        raw_pool = _strategy_pool()
        quick_ok = any(raw_pool.get(k) for k in ("pead", "runup", "rsi2")) or bool(SHARED.earnings_targets)
        if st.button(
            "⚡ 순위 새로고침 (퀵 스캔)",
            width="stretch",
            key="hdr-quick",
            disabled=controls_locked or not quick_ok,
        ):
            refresh = getattr(engine, "refresh_pool_snapshot", None)
            if refresh is None:
                st.warning("퀵 스캔이 아직 이 프로세스에 없다. Streamlit Manage app → Reboot.")
            else:
                refresh()
                SHARED.last_quick_scan_ts = time.time()
                st.session_state["last_quick_scan_ts"] = SHARED.last_quick_scan_ts
                st.rerun(scope="app")
        last_q = SHARED.last_quick_scan_ts or st.session_state.get("last_quick_scan_ts")
        if last_q:
            st.caption(f"마지막 새로고침: {time.strftime('%H:%M:%S', time.localtime(float(last_q)))}")
    with col_c:
        if "radar_toggle" not in st.session_state:
            st.session_state.radar_toggle = True
        radar_on = st.toggle(
            "🔴/🟢 실시간 레이더",
            key="radar_toggle",
            help="OFF: 시세 폴링 중지, 화면 정지 (배터리/트래픽 절약) / ON: 1초 실시간 추적",
        )
        SHARED.radar_on = bool(radar_on)

        if "desk" not in st.session_state:
            st.session_state.desk = "hunt"
        cur = st.session_state.desk
        d1, d2, d3, d4 = st.columns(4)
        with d1:
            if st.button(
                "🎯 듀얼 사냥 데스크",
                key="desk-hunt",
                type="primary" if cur == "hunt" else "secondary",
                width="stretch",
            ):
                st.session_state.desk = "hunt"
                st.rerun(scope="app")
        with d2:
            if st.button(
                "🛡️ 수호 & 포트폴리오",
                key="desk-guard",
                type="primary" if cur == "guard" else "secondary",
                width="stretch",
            ):
                st.session_state.desk = "guard"
                st.rerun(scope="app")
        with d3:
            if st.button(
                "🧠 AI 전술 통제소",
                key="desk-ai",
                type="primary" if cur == "ai" else "secondary",
                width="stretch",
            ):
                st.session_state.desk = "ai"
                st.rerun(scope="app")
        with d4:
            if st.button(
                "⚡ 22:30 지휘소",
                key="desk-warroom",
                type="primary" if cur == "warroom" else "secondary",
                width="stretch",
            ):
                st.session_state.desk = "warroom"
                st.rerun(scope="app")
        if cur == "ai":
            _ai_committee_controls(controls_locked)
        if cur == "warroom":
            _warroom_controls(controls_locked)


_header_live()

if st.session_state.get("need_keys"):
    st.warning("사이드바에 FMP / Alpaca 키를 저장하십시오.")

if "desk" not in st.session_state:
    st.session_state.desk = "hunt"
desk = st.session_state.desk

if desk == "guard":
    _guard_dashboard_live()
    _render_register_form()
elif desk == "ai":
    _render_ai_desk()
elif desk == "warroom":
    _render_warroom_desk()
else:
    _hunt_deck_live()
