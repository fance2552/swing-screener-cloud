"""이벤트 드리븐 스윙 콕핏 v3.0. 풀와이드 3탭: 🎯 듀얼 사냥 데스크 / 🛡️ 수호 & 포트폴리오 / 🧠 AI."""
from __future__ import annotations

import fd_limit  # noqa: F401

import hmac
import os
import threading
import time
import warnings

warnings.filterwarnings("ignore")

import pandas as pd
import streamlit as st

import ai_advisor
import config
import engine
import env_settings
import fmp_news
from screener import event_driven as ed
from screener import portfolio, radar
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
        "hunting_pool": [],
        "scan_progress": 0.0,
        "scan_status_text": "",
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

_EXIT_CODES = frozenset({"SL", "D3", "FRIDAY", "TRAIL", "SQZ_TP", "SQZ_TIME"})
EXIT_REASON_LABEL = {
    "FRIDAY": "금요일 플랫 마감",
    "SL": "손절선 터치",
    "D3": "실적 D-3 강제청산",
    "TRAIL": "트레일링 고점 대비 반락 익절",
    "SQZ_TP": "숏스퀴즈 폭발 익절",
    "SQZ_TIME": "숏스퀴즈 보유기한 초과(시간청산)",
}

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
        expanded=(not env_settings.api_keys_ready()) or not (_saved_keys.get("GROQ_API_KEY") or "").strip(),
    ):
        fmp_in = st.text_input("FMP_API_KEY", key="api_input_FMP_API_KEY")
        alpaca_key_in = st.text_input("ALPACA_API_KEY", key="api_input_ALPACA_API_KEY")
        alpaca_secret_in = st.text_input("ALPACA_SECRET_KEY", key="api_input_ALPACA_SECRET_KEY", type="password")
        groq_in = st.text_input(
            "Groq API Key",
            key="api_input_GROQ_API_KEY",
            type="password",
            help="AI 전술 통제소 탭에서 사용. 없어도 기존 스캐너는 정상 동작한다.",
        )
        if st.button("💾 키 저장", type="primary", width="stretch"):
            missing = env_settings.save_api_keys_and_apply({
                "FMP_API_KEY": fmp_in,
                "ALPACA_API_KEY": alpaca_key_in,
                "ALPACA_SECRET_KEY": alpaca_secret_in,
                "GROQ_API_KEY": groq_in,
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


def _live_hunt_payload() -> tuple[dict, list, list, list, list]:
    snap = SHARED.snapshot()
    earn = _session_gate_rows(ed.earn_rank_live(snap.get("earnings_targets") or [], SHARED.quote))
    sqz = _session_gate_rows(ed.sqz_rank_live(snap.get("squeeze_targets") or [], SHARED.quote))
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
    return snap, earn, sqz, held_enriched, portfolio.get_recently_closed_today()


def _assemble_ai_prompt(extra: str) -> str:
    snap, earn, sqz, held, recently_closed = _live_hunt_payload()
    fmp_key = env_settings.get_api_keys().get("FMP_API_KEY", "")
    news_digest = fmp_news.fetch_news_for_candidates(earn, sqz, held, fmp_key, top_n=3)
    return ai_advisor.build_prompt_text(
        snap.get("regime") or {},
        held,
        earn,
        sqz,
        extra_questions=extra,
        session_meta=_session_meta(),
        news_digest=news_digest,
        recently_closed_today=recently_closed,
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


def _sqz_table(ranked: list[dict]) -> pd.DataFrame:
    recs = [{
        "순위": int(r["rank"]), "티커": r["ticker"], "종목명": str(r.get("name") or "")[:18],
        "시총": ed.fmt_cap(r.get("market_cap")), "스퀴즈점수": round(float(r["score"]), 1),
        "숏비율": f"{float(r.get('short_float_pct') or 0):.1f}%",
        "거래량배율": f"{float(r.get('rvol') or 0):.1f}x",
        "상승여력": f"{float(r.get('upside') or 0):+.1f}%",
        "숏커버일수": f"{float(r.get('days_to_cover') or 0):.1f}일",
        "현재가": round(float(r.get("price") or 0.0), 2),
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


def _render_sqz_deck() -> None:
    snap = SHARED.snapshot()
    ranked = _session_gate_rows(ed.sqz_rank_live(snap.get("squeeze_targets") or [], SHARED.quote))
    note = str(snap.get("squeeze_note") or "")
    st.markdown("**🔥 숏스퀴즈 Top 10**")
    if not ranked:
        _ring_new_shooters(pd.DataFrame(), "사격신호", ed.SQZ_FIRE, "sqz_shoot")
        st.caption(note or "스캔 전 IDLE. 상단 🚀 사냥터 구축.")
        return
    options = ["🎯 1위 자동추적"] + sorted(r["ticker"] for r in ranked)
    if st.session_state.get("sqz_pick") not in options:
        st.session_state["sqz_pick"] = options[0]
    pick = st.selectbox("브리핑 종목", options, key="sqz_pick", label_visibility="collapsed")
    row_map = {r["ticker"]: r for r in ranked}
    brief = row_map.get(pick) or ranked[0]
    _fire_badge_markdown(brief)
    guide = ed.squeeze_guide(brief, _usd_krw(), config.EARN_BUDGET_KRW)
    cls = {"go": "guide-go", "wait": "guide-wait", "chase": "guide-chase"}.get(guide["kind"], "guide-wait")
    st.markdown(f'<div class="{cls}">{guide["html"]}</div>', unsafe_allow_html=True)
    table = _sqz_table(ranked)
    _ring_new_shooters(table, "사격신호", ed.SQZ_FIRE, "sqz_shoot")
    st.dataframe(table, hide_index=True, width="stretch", height=380)
    if note:
        st.caption(note)


def _fire_badge_markdown(row: dict) -> None:
    ticker = str(row.get("ticker") or "")
    d_day = row.get("d_day")
    d_txt = f"D-{int(d_day)}" if d_day is not None else ""
    sig = str(row.get("signal") or "")
    entry_window_open = radar.session_allows_fire_alert()
    entry_condition_met = sig.startswith("🎯") or sig == ed.SQZ_FIRE
    unlock = radar.fire_unlock_time_label()
    if entry_window_open and entry_condition_met:
        badge = f"🟢 [🎯 지금 즉시 사격! 토스 매수] — {sig}"
        color = "green"
    else:
        badge = f"🔴 [사격 대기] ({unlock} 해제)"
        color = "red"
    st.markdown(f":{color}[**{ticker}**  {d_txt}  {badge}]")


@st.fragment(run_every="1s")
def _hunt_deck_live() -> None:
    if not bool(getattr(SHARED, "radar_on", True)):
        st.caption("레이더 OFF — 시세 폴링 정지. 마지막 스냅샷만 표시.")
    col_l, col_r = st.columns(2, gap="medium")
    with col_l:
        _render_earn_deck()
    with col_r:
        _render_sqz_deck()


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
        if g["code"] in ("SL", "D3", "FRIDAY", "TRAIL", "SQZ_TP", "SQZ_TIME"):
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

    strat_badge = "📅 [실적 런업]" if m["strategy"] == "EARNINGS" else (
        "🔥 [숏스퀴즈]" if m["strategy"] == "SQUEEZE" else "⚠️ [레거시]"
    )
    d_day = m.get("d_day")
    if d_day is not None:
        cd_cls = "dn" if m.get("force_exit_d3") else "up"
        countdown = (
            f'<div class="diag"><span class="{cd_cls}">⏰ 실적 D-Day: D-{int(d_day)}일</span> '
            f'<span class="muted">({m["earnings_date"]})</span></div>'
        )
    else:
        countdown = ""
    sl_px = entry * (1 - config.EXIT_SL_PCT / 100.0)
    if m.get("strategy") == "SQUEEZE":
        tp_px = entry * (1 + config.SQZ_TP_PCT / 100.0)
        target_line = f"목표가: ${tp_px:.2f} (+{config.SQZ_TP_PCT:g}%)"
    else:
        peak = float(m.get("peak_price") or entry)
        target_line = f"고점: ${peak:.2f} (트레일링 -{config.EARN_TRAIL_DROP_PCT:g}%)"
    detail = (
        f'<div class="pos-metrics">{target_line} | '
        f'손절선: ${sl_px:.2f} (-{config.EXIT_SL_PCT:g}%) | {g["structure"]}</div>'
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
    if code in EXIT_REASON_LABEL:
        st.error(f"🚨 [지금 토스에서 전량 매도!] — 사유: {EXIT_REASON_LABEL[code]}")
    elif code in ("HOLD", "CRUISE"):
        sl_price = entry * (1 - config.EXIT_SL_PCT / 100.0)
        st.success(f"🟢 [홀딩 순항] (손절선: ${sl_price:,.2f})")
    if st.button(f"🗑️ {tk}만 청산", key=f"liq-{tk}", width="stretch"):
        portfolio.remove_ticker(tk)
        st.rerun(scope="fragment")


def _render_register_form() -> None:
    with st.expander("📌 포지션 등록", expanded=False):
        with st.form(key="add_position_form", clear_on_submit=True):
            f_strategy = st.radio("전략", ["📅 실적 런업", "🔥 숏스퀴즈"], index=0, horizontal=True)
            c1, c2, c3 = st.columns(3)
            with c1:
                f_ticker = st.text_input("티커", placeholder="NET")
            with c2:
                f_entry = st.number_input("평단가 ($)", min_value=0.01, step=0.1, format="%.2f")
            with c3:
                f_shares = st.number_input("수량", min_value=1, step=1, value=1)
            f_edate = st.text_input("실적 발표일 (YYYY-MM-DD, 비우면 표에서 자동/조회)", placeholder="2026-10-08")
            if st.form_submit_button("📌 포지션 등록", width="stretch"):
                tk = (f_ticker or "").upper().strip()
                strat = "EARNINGS" if str(f_strategy).startswith("📅") else "SQUEEZE"
                if tk and float(f_entry) > 0:
                    try:
                        with st.spinner(f"{tk} 등록 중…"):
                            pos = portfolio.add_position(
                                tk, float(f_entry), float(f_shares),
                                strategy=strat, earnings_date=(f_edate or "").strip(),
                            )
                        engine.ensure_heartbeat()
                        extra = f" · 실적일 {pos['earnings_date']}" if pos.get("earnings_date") else ""
                        st.success(f"{pos['ticker']} 등록 완료. 손절 ${pos['stop_price']:.2f}{extra}")
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"등록 실패: {exc}")
                else:
                    st.warning("티커와 평단가를 올바르게 입력하십시오.")


@st.fragment(run_every="1s")
def _guard_dashboard_live() -> None:
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


@st.fragment(run_every="1s")
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
    running = bool(
        telem.get("scan_running")
        or telem.get("heavy_scan_running")
        or getattr(SHARED, "heavy_scan_running", False)
    )
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
            disabled=running,
        ):
            _kick_scan()
            if st.session_state.get("need_keys"):
                st.rerun()
        if running:
            prog = max(0.0, min(1.0, float(telem.get("scan_progress") or SHARED.heavy_scan_progress or 0.0)))
            status = str(telem.get("scan_status_text") or "유망주 풀 구축 중...")
            st.progress(prog, text=f"⏳ {status} ({int(prog * 100)}%)")
    with col_b:
        pool = list(SHARED.hunting_pool or [])
        quick_ok = bool(pool or SHARED.earnings_targets or SHARED.squeeze_targets)
        if st.button(
            "⚡ 순위 새로고침 (퀵 스캔)",
            width="stretch",
            key="hdr-quick",
            disabled=not quick_ok,
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
        radar_on = st.toggle(
            "🔴/🟢 실시간 레이더",
            key="radar_toggle",
            value=True,
            help="OFF: 시세 폴링 중지, 화면 정지 (배터리/트래픽 절약) / ON: 1초 실시간 추적",
        )
        SHARED.radar_on = bool(radar_on)


_header_live()

if st.session_state.get("need_keys"):
    st.warning("사이드바에 FMP / Alpaca 키를 저장하십시오.")

tab_hunt, tab_guard, tab_ai = st.tabs(
    ["🎯 듀얼 사냥 데스크", "🛡️ 수호 & 포트폴리오", "🧠 AI 전술 통제소"]
)

with tab_hunt:
    _hunt_deck_live()

with tab_guard:
    _guard_dashboard_live()
    _render_register_form()

with tab_ai:
    st.markdown("**🧠 AI 전술 통제소**")
    st.caption(
        "실행 버튼을 누르는 순간의 라이브 스캔·시세·포트폴리오·세션을 조립한다. "
        "부팅 시 빈 배열을 재사용하지 않는다. 아래 칸은 추가 질문 전용이다."
    )
    st.text_area("추가 질문 (선택)", key="ai_extra_q", height=160)
    col_run, col_preview = st.columns([2, 1])
    with col_run:
        run_clicked = st.button(
            "🧠 AI 전술 분석 실행", type="primary", width="stretch", key="ai-run"
        )
    with col_preview:
        preview_clicked = st.button(
            "🔍 조립 프롬프트 미리보기", width="stretch", key="ai-preview"
        )

    if preview_clicked:
        st.session_state.ai_preview = _assemble_ai_prompt(st.session_state.get("ai_extra_q") or "")

    if run_clicked:
        prompt_now = _assemble_ai_prompt(st.session_state.get("ai_extra_q") or "")
        st.session_state.ai_preview = prompt_now
        with st.spinner("AI가 데이터를 분석 중입니다. (약 5~10초 소요)..."):
            st.session_state.ai_report = ai_advisor.run_briefing_from_text(prompt_now)
            st.session_state.ai_report_ts = time.time()

    if st.session_state.get("ai_preview"):
        with st.expander("이번에 조립된 프롬프트", expanded=False):
            st.code(st.session_state.ai_preview, language=None)

    if st.session_state.get("ai_report"):
        ts = st.session_state.get("ai_report_ts")
        if ts:
            age_min = (time.time() - ts) / 60.0
            st.caption(f"마지막 분석: {age_min:.1f}분 전 (자동 갱신 안 됨 — 다시 누르면 새로 분석)")
        with st.container(border=True):
            st.markdown(st.session_state.ai_report)
    else:
        st.info("아직 분석 요청이 없습니다. 추가 질문이 있으면 적고 [AI 전술 분석 실행]을 누르십시오.")
