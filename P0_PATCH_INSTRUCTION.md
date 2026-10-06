# P0 긴급 시스템 안정화 패치 지시서

- **대상:** Cursor Pro (프로젝트 파일 통째 교체)
- **근거:** `AUDIT_2026-10-03.md`
- **기한:** 2026-10-05 프리마켓 이전
- **규칙:** 아래 각 파일 절의 코드 블록이 해당 경로의 **최종 전체 원문**이다. 중간 생략 없음. 부분 패치 금지. `p0_payload/` 아래 파일과 바이트 단위로 동일하다. 복사 명령이 실패하면 코드 블록을 그대로 덮어쓴다.
- **금지:** `.env` 커밋, `git config` 변경, `--no-verify`, force push, LDPB/HFT/토스 주문 API 재도입, broker send.

---

## 0. 작업 순서 (이 순서를 어기면 안 된다)

1. 사전 방어선 — 현재 트리를 `baseline_pre_p0` 으로 고정한다. **코드 수정 전.**
2. `p0_payload/` 원문을 프로젝트 루트 대응 경로로 복사한다. 또는 이 문서의 파일별 전체 코드로 덮어쓴다.
3. 세션 시계 검증 스크립트를 실행한다.
4. Streamlit 를 재기동하고 수용 기준을 확인한다.

---

## 1. 사전 방어선 (Git 커밋 0건)

저장소에 `.git` 이 이미 있다. `git init` 은 `.git` 이 없을 때만 실행한다.

```bash
cd /Users/mac-edit/Desktop/swing-screener
if [ ! -d .git ]; then git init; fi
git add -A
git status
git commit -m "$(cat <<'EOF'
baseline_pre_p0

EOF
)"
```

제약:

- `git config` 를 수정하지 않는다. identity 오류로 커밋이 실패하면 중단하고 보고한다. 우회 훅 생략 금지.
- `.gitignore` 가 `.env`, `portfolio.json`, `.venv/` 를 막고 있는지 커밋 전에 확인한다. 시크릿이 staged 되면 `git reset` 후 제외한다.
- `portfolio.json` 은 무시된다. 실전 포지션 파일은 커밋하지 않는다.

성공 확인: `git log -1 --oneline` 이 `baseline_pre_p0` 을 보여야 한다. 이 커밋이 생기기 전에 아래 파일을 덮어쓰지 않는다.

---

## 2. 파일 복사 맵 (권장)

`baseline_pre_p0` 커밋 직후:

```bash
cd /Users/mac-edit/Desktop/swing-screener
cp p0_payload/screener/radar.py screener/radar.py
cp p0_payload/state.py state.py
cp p0_payload/screener/portfolio.py screener/portfolio.py
cp p0_payload/engine.py engine.py
cp p0_payload/ai_advisor.py ai_advisor.py
cp p0_payload/data_feed.py data_feed.py
cp p0_payload/app.py app.py
mkdir -p tests
cp p0_payload/tests/test_session_clock.py tests/test_session_clock.py
```

복사 후 `diff -u` 로 `p0_payload/` 와 루트가 동일한지 확인한다. 다르면 이 문서의 전체 코드 블록으로 다시 덮는다.

---

## 3. 결함별 적용 요지 (구현은 전체 파일에 이미 들어 있다)

### 3.1 미국 세션 시계 (`screener/radar.py`)

KST 고정 분기(`wd == 5`, `17:00`, `22:30`)를 삭제했다. 판정은 `zoneinfo.ZoneInfo("America/New_York")` 만 사용한다.

| key | ET |
|---|---|
| PRE | 04:00 ≤ t < 09:30 |
| RTH | 09:30 ≤ t < 16:00 |
| AH | 16:00 ≤ t < 20:00 |
| CLOSED | 그 외, 주말, 2025–2027 NYSE 풀데이 휴장 |

`kst_session()` 은 UI 시계(KST 표시)만 담당하고, `key` 는 ET 판정을 그대로 반환한다. `session_allows_fire_alert()` 는 RTH 이고 09:45:00.000 ET 이후일 때만 True. `is_rth_open_chime_window()` 는 09:30:00–09:30:05 ET.

### 3.2 프리마켓 사격 차단 + 청산 사이렌 (`app.py`, `engine.py`, `screener/radar.py`)

- `_ring_new_shooters` 는 `session_allows_fire_alert()` 가 False 이면 return. seen-set 도 갱신하지 않는다. 09:45 에 한 번 울릴 수 있게 한다.
- `_session_gate_rows` 는 같은 구간에서 표의 🎯 를 `🌙 장전 대기 (09:45 ET)` 로 바꾼다.
- `_speak_market_open` 은 KST 22:30 을 버린다. ET 09:30 첫 5초만 방송한다.
- 수호 데스크는 SL / D3 / FRIDAY 와 포트폴리오 손상에 사이렌 + "즉시 청산" 음성을 울린다.

### 3.3 스캔 비동기화 (`app.py`, `engine.py`)

`_kick_scan()` 은 `engine.start_scan_async()` 만 호출한다. UI 스레드에서 `run_scan()` 을 부르지 않는다. 헤더 1초 fragment 는 `SHARED.scan_phase` / `scan_running` 을 표시한다. 스캔 중에도 수호 데스크 fragment 는 산다. `stop_scan()` 은 시세 피드가 유지된다고 말한다.

### 3.4 포트폴리오 fail-closed (`screener/portfolio.py`, `state.py`)

`load_portfolio()` 가 파싱 실패 시 `[]` 로 침묵하지 않는다. 마지막 정상 캐시 → `portfolio.json.bak` → 그래도 없으면 빈 목록이지만 `SHARED.portfolio_corrupt=True` 와 빨간 배너. 손상+무캐시 상태에서 빈 파일로 덮어쓰지 않는다. 저장은 pid+ns 임시 파일, `fsync`, 기존 파일을 `.bak` 으로 복사 후 `os.replace`.

### 3.5 Tab 3 AI 프롬프트 (`app.py`, `ai_advisor.py`)

부팅 시 `ai_prompt_gen` 캐시를 삭제했다. 실행/미리보기 순간에 라이브 스캔·시세·포트폴리오·세션을 조립한다. textarea 는 추가 질문 전용. JSON 은 `dumps_valid_json` 이 하위 랭크 행을 제거해서 예산을 맞춘다. 문자열 `[:4000]` 절단은 없다.

### 3.6 Yahoo 429 백오프 (`data_feed.py`)

429 시 60초 대기, 이후 120 / 240 / 480 / 900초. 백오프 동안 Yahoo URL 을 치지 않고 Nasdaq 폴백만 쓴다. 200 성공 시 60초로 리셋.

---

## 4. 적용 후 검증

```bash
cd /Users/mac-edit/Desktop/swing-screener
PYTHONPATH=. .venv/bin/python tests/test_session_clock.py
.venv/bin/python -m py_compile app.py engine.py state.py ai_advisor.py data_feed.py screener/radar.py screener/portfolio.py
```

`failed=0` 과 컴파일 성공이 필수다. 세션 케이스는 감사에서 깨진 시각을 그대로 포함한다: 일요일 22:31 CLOSED, 월요일 02:00 CLOSED, 토요일 01:00 RTH, 11/2 22:45 PRE, 11/3 05:30 RTH.

수동:

1. 앱 기동 후 헤더에 `ET` 시각과 `🔒 사격잠금(09:45 ET)` 또는 `🔓 사격허용` 이 보이는지.
2. 스캔 시작을 눌러도 수호 데스크 1초 갱신이 멈추지 않는지. 배너에 `⏳ 스캔 …` 이 뜨는지.
3. Tab 3 에서 미리보기를 누르면 스캔 후 데이터가 `[]` 가 아닌지. JSON 이 파손되지 않았는지.
4. `portfolio.json` 을 잠시 깨뜨리면 빨간 배너와 사이렌이 나오고, 복구 캐시가 있으면 포지션이 사라지지 않는지. 검증 후 원본을 되돌린다.

---

## 5. 파일별 최종 전체 원문

아래 블록을 해당 경로에 그대로 저장한다. 한 줄도 생략하지 않았다.

### `screener/radar.py`

**목적지:** 프로젝트 루트의 `screener/radar.py` 를 이 내용으로 통째 교체한다. 줄 수 376.

```python
"""Strike radar: regime, volume-confirmed breakout, pocket pivot, Toss guide."""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import config

NEAR_PCT = 1.5
_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")
_RTH_MINUTES = 390.0
_PRE_START_MIN = 4 * 60
_RTH_START_MIN = 9 * 60 + 30
_RTH_END_MIN = 16 * 60
_AH_END_MIN = 20 * 60
_FIRE_LOCK_MIN = 15.0

# NYSE / Nasdaq full-day closures. America/New_York DST is applied by zoneinfo;
# these dates are calendar dates in ET, not KST.
_US_MARKET_HOLIDAYS = frozenset({
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
})


def now_et(now: datetime | None = None) -> datetime:
    """Always return an aware America/New_York datetime. DST is zoneinfo's job."""
    if now is None:
        return datetime.now(_ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=_ET)
    return now.astimezone(_ET)


def now_kst(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(_KST)
    if now.tzinfo is None:
        return now.replace(tzinfo=_KST)
    return now.astimezone(_KST)


def is_us_market_holiday(d: date | None = None) -> bool:
    return (d or now_et().date()) in _US_MARKET_HOLIDAYS


def _session_parts(ts: datetime) -> tuple[str, str, str]:
    if ts.weekday() >= 5 or is_us_market_holiday(ts.date()):
        return "CLOSED", "💤 미국장 휴장 (Closed)", "sess-CLOSED"
    minutes = ts.hour * 60 + ts.minute
    if _PRE_START_MIN <= minutes < _RTH_START_MIN:
        return "PRE", "🌙 프리마켓 (Pre-Market)", "sess-PRE"
    if _RTH_START_MIN <= minutes < _RTH_END_MIN:
        return "RTH", "☀️ 정규장 (Regular Market)", "sess-RTH"
    if _RTH_END_MIN <= minutes < _AH_END_MIN:
        return "AH", "🌆 애프터마켓 (After-Market)", "sess-AH"
    return "CLOSED", "💤 미국장 휴장 (Closed)", "sess-CLOSED"


def us_session(now: datetime | None = None) -> dict[str, str]:
    """Canonical US cash session. Input tz is converted to ET. Never use KST wall clock."""
    ts = now_et(now)
    key, badge, css = _session_parts(ts)
    return {
        "key": key,
        "badge": badge,
        "css": css,
        "et_clock": ts.strftime("%H:%M:%S"),
        "et_date": ts.date().isoformat(),
        "clock": ts.strftime("%H:%M"),
        "label": f"{badge} · {ts.strftime('%H:%M')} ET",
    }


def rth_elapsed_minutes(now: datetime | None = None) -> float:
    """Minutes since 09:30 ET. Negative before the open. 0 on non-RTH days before open."""
    ts = now_et(now)
    return (
        ts.hour * 60
        + ts.minute
        + ts.second / 60.0
        + ts.microsecond / 60_000_000.0
        - float(_RTH_START_MIN)
    )


def session_allows_fire_alert(now: datetime | None = None) -> bool:
    """True only in RTH after 09:45:00.000 ET. Premarket / first 15 minutes / weekend / holiday = False."""
    ts = now_et(now)
    key, _badge, _css = _session_parts(ts)
    if key != "RTH":
        return False
    return rth_elapsed_minutes(ts) >= _FIRE_LOCK_MIN


def is_rth_open_chime_window(now: datetime | None = None) -> bool:
    """First five seconds of 09:30 ET on a live RTH day. DST-safe."""
    ts = now_et(now)
    key, _badge, _css = _session_parts(ts)
    if key != "RTH":
        return False
    return ts.hour == 9 and ts.minute == 30 and ts.second <= 5


def calculate_sri(score: float, dist_pct: float, lam: float = 0.25) -> float:
    """Strike readiness. Near the pivot keeps the score. Distance decays it."""
    try:
        s = float(score or 0.0)
        d = abs(float(dist_pct or 0.0))
        return round(s * math.exp(-lam * d), 2)
    except Exception:  # noqa: BLE001
        return 0.0


def dist_pct(price: float, pivot: float) -> float:
    """((피봇돌파가 - 현재가) / 피봇돌파가) * 100. Through-pivot is negative."""
    if pivot <= 0:
        return 0.0
    return ((pivot - price) / pivot) * 100.0


def session_elapsed_minutes(now: datetime | None = None) -> tuple[str, float]:
    """US cash clock. Returns (PRE|RTH|AH|CLOSED, minutes since 09:30, capped at 390)."""
    ts = now_et(now)
    key, _badge, _css = _session_parts(ts)
    if key == "CLOSED":
        return "CLOSED", _RTH_MINUTES
    if key == "PRE":
        return "PRE", 0.0
    if key == "AH":
        return "AH", _RTH_MINUTES
    return "RTH", min(max(rth_elapsed_minutes(ts), 0.0), _RTH_MINUTES)


def check_breakout_volume(
    current_volume: float,
    avg_volume_50d: float,
    now: datetime | None = None,
) -> bool | None:
    """U-shaped full-day projection versus the 50-day average.

    None means premarket or the first 15 minutes: volume is not confirmed yet.
    """
    phase, elapsed = session_elapsed_minutes(now)
    if phase == "PRE" or (phase == "RTH" and elapsed < 15.0):
        return None
    avg = float(avg_volume_50d or 0.0)
    vol = float(current_volume or 0.0)
    if avg <= 0 or vol <= 0:
        return False
    if elapsed < 30.0:
        factor = 0.55 * (_RTH_MINUTES / max(elapsed, 1.0))
    elif elapsed < 60.0:
        factor = 0.75 * (_RTH_MINUTES / max(elapsed, 1.0))
    else:
        factor = _RTH_MINUTES / max(elapsed, 1.0)
    projected = vol * factor
    return projected >= avg * config.PIVOT_VOLUME_MULT


def regime_state(regime: Any) -> str:
    """Map the shared regime dict onto NORMAL / PRESSURE / CORRECTION."""
    if isinstance(regime, dict):
        raw = str(regime.get("state") or regime.get("label") or "").upper()
    else:
        raw = str(regime or "").upper()
    if raw in {"CORRECTION", "RISK-OFF", "RED"}:
        return "CORRECTION"
    if raw in {"PRESSURE", "CAUTION"}:
        return "PRESSURE"
    return "NORMAL"


def classify_signal(
    price: float,
    pivot: float,
    *,
    regime: Any = "NORMAL",
    is_pocket_pivot: bool = False,
    current_volume: float = 0.0,
    avg_volume_50d: float = 0.0,
    score: float = 0.0,
    now: datetime | None = None,
) -> tuple[str, str, float]:
    """Shot gate. Correction allows a shot only for a score of 85 or more with volume."""
    if pivot <= 0:
        return "⏳ 눌림 관망 (반등 대기)", "반등 대기", 2.0
    state = regime_state(regime)
    distance = (price / pivot - 1.0) * 100.0
    if distance > 3.5:
        return f"🚫 추격 금지 (과열 +{distance:.1f}%)", f"과열 (+{distance:.1f}%)", 3.0
    if distance < 0.0:
        if is_pocket_pivot and (state != "CORRECTION" or score >= 85.0):
            return "💎 포켓 피봇 (선행 매집)", "피봇 하단 선행 매집", 1.5
        return "⏳ 눌림 관망 (반등 대기)", "반등 대기", 2.0
    vol_status = check_breakout_volume(current_volume, avg_volume_50d, now=now)
    sweet = f"${pivot:.2f} ~ ${pivot * 1.035:.2f}"
    if vol_status is None:
        if state == "CORRECTION" and score < 85.0:
            return "⏳ 눌림 관망 (조정장: 85점 이상 대기)", "조정장 엄격 대기", 2.0
        return "🌙 장전 돌파 (개장 후 확증 필수)", f"장전 포착 {sweet}", 1.2
    if state == "CORRECTION":
        if score < 85.0:
            return "⏳ 눌림 관망 (조정장: 85점 이상 대기)", "조정장 엄격 대기", 2.0
        if vol_status is True:
            return "🎯 지금 사격! (조정장 엄선)", sweet, 1.0
        return "🟡 저볼륨 돌파 (거래량 관찰)", "거래량 확증 대기", 1.8
    if vol_status is True:
        return "🎯 지금 사격!", sweet, 1.0
    return "🟡 저볼륨 돌파 (거래량 관찰)", "거래량 확증 대기", 1.8


def fire_signal(price: float, pivot: float, **kwargs: Any) -> tuple[str, str]:
    """Signal text plus the sweet-spot line. Extra kwargs feed classify_signal."""
    sig, guide, _pri = classify_signal(price, pivot, **kwargs)
    return sig, guide


def status_text(price: float, pivot: float, distance: float | None = None, **kwargs: Any) -> str:
    _ = distance
    sig, _guide = fire_signal(price, pivot, **kwargs)
    return sig


def shot_priority(status: str) -> float:
    """🎯 1, 🌙 1.2, 💎 1.5, 🟡 1.8, ⏳ 2, 🚫 3, ⏸️ 4."""
    text = str(status or "")
    if text.startswith("🎯"):
        return 1.0
    if text.startswith("🌙"):
        return 1.2
    if text.startswith("💎"):
        return 1.5
    if text.startswith("🟡"):
        return 1.8
    if text.startswith("⏳"):
        return 2.0
    if text.startswith("🚫"):
        return 3.0
    if text.startswith("⏸"):
        return 4.0
    return 3.0


def rank_targets(
    rows: list[dict[str, Any]],
    regime: Any = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Priority, then strike readiness, then absolute distance to the pivot."""
    out: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        price = float(row.get("price") or 0.0)
        pivot = float(row.get("pivot") or 0.0)
        gap = dist_pct(price, pivot)
        sig, sweet, pri = classify_signal(
            price,
            pivot,
            regime=regime if regime is not None else "NORMAL",
            is_pocket_pivot=bool(row.get("is_pocket_pivot")),
            current_volume=float(row.get("day_volume") or 0.0),
            avg_volume_50d=float(row.get("avg_volume_50d") or 0.0),
            score=float(row.get("score") or 0.0),
            now=now,
        )
        row["dist_pct"] = gap
        row["status"] = sig
        row["sweet"] = sweet
        row["_priority"] = pri
        row["sri"] = calculate_sri(row.get("score"), gap)
        out.append(row)
    out.sort(key=lambda r: (float(r["_priority"]), -float(r.get("sri") or 0.0), abs(float(r["dist_pct"]))))
    for i, row in enumerate(out, start=1):
        row["rank"] = i
    return out


def kst_session(now: datetime | None = None) -> dict[str, str]:
    """UI clock helper. Session key is ET. Display clock is KST."""
    ts_kst = now_kst(now)
    info = us_session(ts_kst)
    clock = ts_kst.strftime("%H:%M")
    return {
        "key": info["key"],
        "badge": info["badge"],
        "css": info["css"],
        "clock": clock,
        "et_clock": info["et_clock"],
        "et_date": info["et_date"],
        "label": f"{info['badge']} · {clock} KST",
    }


def toss_guide(row: dict[str, Any]) -> dict[str, str]:
    """Two-line Toss path. Uses the ranked status so volume and regime are not recomputed blind."""
    ticker = str(row.get("ticker") or "")
    price = float(row.get("price") or 0.0)
    pivot = float(row.get("pivot") or 0.0)
    stop = float(row.get("stop") or 0.0)
    gap = dist_pct(price, pivot)
    stop_pct = (stop / price - 1.0) * 100.0 if price > 0 else 0.0
    sig = str(row.get("status") or "")
    sweet = str(row.get("sweet") or "")
    if not sig:
        sig, sweet = fire_signal(
            price,
            pivot,
            is_pocket_pivot=bool(row.get("is_pocket_pivot")),
            current_volume=float(row.get("day_volume") or 0.0),
            avg_volume_50d=float(row.get("avg_volume_50d") or 0.0),
            score=float(row.get("score") or 0.0),
        )
    line_px = f"• 시세: 현재가 ${price:.2f} | 피봇 ${pivot:.2f} | {sweet} | 손절 ${stop:.2f} ({stop_pct:.1f}%)"
    if sig.startswith("⏸"):
        title = f"⏸️ [{ticker} 시장국면: 신규진입 보류]"
        line1 = line_px
        line2 = "• 행동: SPY 조정. 신규 예약주문 금지. 보유만 수호."
        kind = "halt"
    elif sig.startswith("🚫"):
        title = f"🚫 [{ticker} 추격 금지: 과열 구간]"
        line1 = line_px
        line2 = "• 행동: 추격 매수 금지. 피봇 근처 눌림만 조건주문."
        kind = "chase"
    elif sig.startswith("🎯"):
        title = f"🎯 [{ticker} 지금 사격: 엄선 대장주]"
        line1 = f"• 시세: 현재가 ${price:.2f} | 진입 {sweet} | 손절 ${stop:.2f} ({stop_pct:.1f}%)"
        if "조정장" in sig:
            line2 = "• 행동: 토스 [일반주문 ➔ 지정가] (⚠️ 조정장 리스크: 평소 50% 비중 진입 권장) ➔ 체결 즉시 조건주문 손절가 설정"
        else:
            line2 = "• 행동: 토스 [일반주문 ➔ 지정가 매수] ➔ 체결 즉시 조건주문 손절가 설정"
        kind = "go"
    elif sig.startswith("🌙"):
        title = f"🌙 [{ticker} 장전 돌파: 정규장 확증 대기]"
        line1 = f"• 시세: 현재가 ${price:.2f} | 진입 {sweet} | 손절 ${stop:.2f} ({stop_pct:.1f}%)"
        line2 = "• 행동: 프리장 돌파 확인! 토스 [지정가 예약]만 걸고, 10시 반 개장 후 거래량 폭발 시 체결 확인"
        kind = "wait"
    elif sig.startswith("💎"):
        title = f"💎 [{ticker} 포켓 피봇: 선행 매집]"
        line1 = (
            f"• 시세: 현재가 ${price:.2f} (피봇 ${pivot:.2f} 까지 {max(gap, 0):.2f}%) | "
            f"손절 ${stop:.2f} ({stop_pct:.1f}%)"
        )
        line2 = f"• 행동: 피봇 ${pivot:.2f} 예약 지정가. 돌파 거래량은 아직 없다."
        kind = "pocket"
    elif sig.startswith("🟡"):
        title = f"🟡 [{ticker} 저볼륨 돌파: 거래량 관찰]"
        line1 = line_px
        line2 = "• 행동: 가격은 피봇 위다. 50일 평균의 1.4배가 안 되면 사격하지 마라."
        kind = "soft"
    else:
        title = f"⏳ [{ticker} 눌림 관망: 길목 지키기]"
        line1 = (
            f"• 시세: 현재가 ${price:.2f} (피봇 ${pivot:.2f} 까지 {max(gap, 0):.2f}%) | "
            f"손절 ${stop:.2f} ({stop_pct:.1f}%)"
        )
        line2 = f"• 행동: 토스 [조건주문 ➔ 구매(더 살래요)] 감시가 ${pivot:.2f} · 정규장일 때만"
        kind = "wait"
    if kind == "wait" and gap > 10.0 and sig.startswith("⏳"):
        line2 = f"• 행동: 피봇까지 거리가 {gap:.1f}%로 멉니다 (사정권 밖). 상위 1~3위 사정권 종목을 우선 관찰하십시오."
    html = (
        f'<div class="guide-title">{title}</div>'
        f'<div class="guide-line">{line1}</div>'
        f'<div class="guide-line">{line2}</div>'
    )
    return {"kind": kind, "html": html, "md": f"**{title}**\n\n{line1}\n\n{line2}"}
```

### `state.py`

**목적지:** 프로젝트 루트의 `state.py` 를 이 내용으로 통째 교체한다. 줄 수 149.

```python
"""Process-wide cockpit state. Scan writes here; UI only reads."""
from __future__ import annotations

import threading
import time
from typing import Any

import pandas as pd


class SharedState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.scan_running = False
        self.is_scanning = False
        self.scan_phase = "IDLE"
        self.scan_error = ""
        self.banner = "대기. 스캔 시작을 누르십시오."
        self.universe_n = 0
        self.scored_n = 0

        # LEGACY (LDPB, unused) — kept so nothing that still references it explodes.
        self.targets: list[dict[str, Any]] = []
        self.ohlcv: dict[str, pd.DataFrame] = {}
        self.bars: dict[str, pd.DataFrame] = self.ohlcv
        self.selected: str = ""

        # Tab 1 — event-driven scanners
        self.earnings_targets: list[dict[str, Any]] = []
        self.earnings_note = ""
        self.earnings_ts = 0.0
        self.squeeze_targets: list[dict[str, Any]] = []
        self.squeeze_note = ""
        self.squeeze_ts = 0.0

        self.regime = {"color": "YELLOW", "label": "UNKNOWN", "detail": ""}
        self.fmp_ok = False
        self.alpaca_ok = False
        self.last_scan_ts = 0.0
        self.price_gen = 0
        self.timeframe = "1d"
        self.scan_done = 0
        self.scan_total = 0
        self.live_quotes: dict[str, dict] = {}
        now = time.time()
        self.fmp_tick_count = 0
        self.fmp_last_time = now
        self.alpaca_tick_count = 0
        self.alpaca_last_time = now

        # Fail-closed portfolio I/O. Empty string = disk is readable.
        self.portfolio_error = ""
        self.portfolio_corrupt = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "scan_running": self.scan_running,
                "is_scanning": self.is_scanning,
                "price_gen": self.price_gen,
                "scan_phase": self.scan_phase,
                "scan_error": self.scan_error,
                "banner": self.banner,
                "universe_n": self.universe_n,
                "scored_n": self.scored_n,
                "targets": [dict(r) for r in self.targets],
                "earnings_targets": [dict(r) for r in self.earnings_targets],
                "earnings_note": self.earnings_note,
                "squeeze_targets": [dict(r) for r in self.squeeze_targets],
                "squeeze_note": self.squeeze_note,
                "selected": self.selected,
                "regime": dict(self.regime),
                "fmp_ok": self.fmp_ok,
                "alpaca_ok": self.alpaca_ok,
                "last_scan_ts": self.last_scan_ts,
                "timeframe": self.timeframe,
                "scan_done": self.scan_done,
                "scan_total": self.scan_total,
                "live_quotes": dict(self.live_quotes),
                "portfolio_error": self.portfolio_error,
                "portfolio_corrupt": self.portfolio_corrupt,
            }

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self.scan_phase = phase

    def set_banner(self, text: str) -> None:
        with self._lock:
            self.banner = text

    def set_earnings_targets(self, rows: list[dict[str, Any]], note: str = "") -> None:
        with self._lock:
            self.earnings_targets = [dict(r) for r in rows]
            self.earnings_note = str(note or "")
            self.earnings_ts = time.time()

    def set_squeeze_targets(self, rows: list[dict[str, Any]], note: str = "") -> None:
        with self._lock:
            self.squeeze_targets = [dict(r) for r in rows]
            self.squeeze_note = str(note or "")
            self.squeeze_ts = time.time()

    def set_portfolio_health(self, error: str = "", corrupt: bool = False) -> None:
        with self._lock:
            self.portfolio_error = str(error or "")
            self.portfolio_corrupt = bool(corrupt)

    def note_feed(self, feed: str, tick: bool = True) -> None:
        now = time.time()
        with self._lock:
            if feed == "fmp":
                if tick:
                    self.fmp_tick_count += 1
                self.fmp_last_time = now
            elif feed == "alpaca":
                if tick:
                    self.alpaca_tick_count += 1
                self.alpaca_last_time = now

    def telemetry(self) -> dict[str, Any]:
        with self._lock:
            return {
                "fmp_tick_count": int(self.fmp_tick_count),
                "fmp_last_time": float(self.fmp_last_time),
                "alpaca_tick_count": int(self.alpaca_tick_count),
                "alpaca_last_time": float(self.alpaca_last_time),
                "regime": dict(self.regime),
                "banner": self.banner,
                "scan_phase": self.scan_phase,
                "scan_running": self.scan_running,
                "portfolio_error": self.portfolio_error,
                "portfolio_corrupt": self.portfolio_corrupt,
            }

    def quote(self, ticker: str) -> float:
        with self._lock:
            row = self.live_quotes.get(str(ticker).upper().strip())
            if isinstance(row, dict):
                raw = row.get("price")
            else:
                raw = row
        try:
            return float(raw or 0)
        except (TypeError, ValueError):
            return 0.0


SHARED = SharedState()
```

### `screener/portfolio.py`

**목적지:** 프로젝트 루트의 `screener/portfolio.py` 를 이 내용으로 통째 교체한다. 줄 수 420.

```python
"""portfolio.json 관리 + 4대 청산 헌법 (FRIDAY FLAT / TP / SL / D-3). No LDPB."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import config
from data.fmp import fetch_fmp_analyst_data
from screener import event_driven as ed
from state import SHARED

PORTFOLIO_PATH = Path(__file__).resolve().parent.parent / "portfolio.json"
BACKUP_PATH = PORTFOLIO_PATH.with_name("portfolio.json.bak")
_PF_LOCK = threading.RLock()
_CACHE: list[dict[str, Any]] = []
_CACHE_OK = False


def _parse_rows(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("portfolio.json 은 리스트여야 합니다")
    return [row for row in raw if isinstance(row, dict) and row.get("ticker")]


def _read_path(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"{path.name} 이 비어 있습니다")
    return _parse_rows(json.loads(text))


def _read_raw() -> list[dict[str, Any]]:
    return _read_path(PORTFOLIO_PATH)


def _clone(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _fsync_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())


def _remember(rows: list[dict[str, Any]]) -> None:
    global _CACHE, _CACHE_OK
    _CACHE = _clone(rows)
    _CACHE_OK = True
    SHARED.set_portfolio_health("", False)


def load_portfolio() -> list[dict[str, Any]]:
    """Fail-closed. Never hide a corrupt file behind an empty list.

    Order: live file → in-memory last-good → portfolio.json.bak.
    UI must read SHARED.portfolio_corrupt / portfolio_error before treating [] as 'no positions'.
    """
    global _CACHE, _CACHE_OK
    with _PF_LOCK:
        try:
            rows = _read_raw()
            _remember(rows)
            return _clone(rows)
        except FileNotFoundError:
            _remember([])
            return []
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            if _CACHE_OK:
                SHARED.set_portfolio_health(
                    f"portfolio.json 손상. 마지막 정상 캐시 {len(_CACHE)}건 유지. {detail}",
                    True,
                )
                return _clone(_CACHE)
            try:
                if not BACKUP_PATH.exists():
                    raise FileNotFoundError(f"{BACKUP_PATH.name} 없음")
                bak = _read_path(BACKUP_PATH)
            except Exception as bak_exc:  # noqa: BLE001
                SHARED.set_portfolio_health(
                    f"portfolio.json 손상, 백업도 실패. 빈 목록으로 위장하지 않음. "
                    f"{detail} / bak {type(bak_exc).__name__}: {bak_exc}",
                    True,
                )
                return []
            _CACHE = _clone(bak)
            _CACHE_OK = True
            SHARED.set_portfolio_health(
                f"portfolio.json 손상. {BACKUP_PATH.name} 에서 {len(bak)}건 복구. {detail}",
                True,
            )
            return _clone(bak)


def save_portfolio(data: list[dict[str, Any]]) -> None:
    rows = _parse_rows(data)
    payload = json.dumps(rows, ensure_ascii=False, indent=2)
    tmp = PORTFOLIO_PATH.with_name(
        f"{PORTFOLIO_PATH.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    with _PF_LOCK:
        try:
            if PORTFOLIO_PATH.exists() and PORTFOLIO_PATH.stat().st_size > 0:
                current = PORTFOLIO_PATH.read_text(encoding="utf-8")
                try:
                    _parse_rows(json.loads(current))
                except Exception:  # noqa: BLE001
                    current = ""
                if current:
                    _fsync_write(BACKUP_PATH, current)
            _fsync_write(tmp, payload)
            os.replace(tmp, PORTFOLIO_PATH)
            _remember(rows)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


def _rows_for_write() -> list[dict[str, Any]]:
    rows = load_portfolio()
    if SHARED.portfolio_corrupt and not rows and not _CACHE_OK:
        raise ValueError(
            "portfolio.json 이 손상되었고 복구 캐시도 없습니다. 빈 파일로 덮어쓰지 않습니다."
        )
    return rows


def held_tickers() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for row in load_portfolio():
        tk = str(row.get("ticker") or "").upper().strip()
        if tk and tk not in seen:
            seen.add(tk)
            out.append(tk)
    return out


def add_position(
    ticker: str,
    entry_price: float,
    shares: float,
    strategy: str = "EARNINGS",
    earnings_date: str = "",
) -> dict[str, Any]:
    tk = (ticker or "").upper().strip()
    entry = float(entry_price)
    qty = float(shares)
    if not tk or entry <= 0 or qty <= 0:
        raise ValueError("티커 / 평단가 / 수량 필요")
    strat = str(strategy or "EARNINGS").upper().strip()
    if strat not in ("EARNINGS", "SQUEEZE"):
        raise ValueError("전략은 EARNINGS 또는 SQUEEZE")

    edate, date_src = "", ""
    row_e: dict[str, Any] | None = None
    typed = str(earnings_date or "").strip()
    if typed:
        if ed.days_to_event(typed) is None:
            raise ValueError("실적 발표일 형식은 YYYY-MM-DD")
        edate, date_src = typed[:10], "MANUAL"
    else:
        pool = SHARED.earnings_targets if strat == "EARNINGS" else SHARED.squeeze_targets
        with SHARED._lock:
            for r in pool:
                if str(r.get("ticker") or "").upper() == tk:
                    row_e = dict(r)
                    break
        if row_e and row_e.get("earnings_date"):
            edate, date_src = str(row_e["earnings_date"])[:10], "CAL"
        elif strat == "EARNINGS":
            raise ValueError("실적 런업 종목은 실적 발표일이 필수입니다 (표에 없으면 직접 입력)")
        else:
            try:
                found = ed.next_earnings_date(tk)
            except Exception:  # noqa: BLE001
                found = ""
            if found:
                edate, date_src = found, "AUTO"

    if edate:
        left = ed.days_to_event(edate)
        if left is not None and left < 0:
            raise ValueError("이미 지난 실적 발표일입니다")

    if row_e and (row_e.get("buy_ratio") or row_e.get("target_consensus")):
        wall: dict[str, Any] = {
            "target_consensus": row_e.get("target_consensus"),
            "buy_ratio": row_e.get("buy_ratio"),
        }
    else:
        try:
            wall = fetch_fmp_analyst_data(tk) or {}
        except Exception:  # noqa: BLE001
            wall = {}

    fx = SHARED.quote("KRW=X")
    if fx <= 500:
        fx = 1360.0
    stop = round(entry * (1.0 - float(config.EXIT_SL_PCT) / 100.0), 2)

    pos: dict[str, Any] = {
        "ticker": tk,
        "strategy": strat,
        "entry_price": round(entry, 4),
        "shares": qty,
        "stop_price": stop,
        "entry_date": ed.today_et().isoformat(),
        "entry_fx": round(fx, 1),
        "target_consensus": float(wall.get("target_consensus") or 0.0),
        "buy_ratio": float(wall.get("buy_ratio") or 0.0),
    }
    if edate:
        pos["earnings_date"] = edate
        pos["date_src"] = date_src

    with _PF_LOCK:
        data = [p for p in _rows_for_write() if str(p.get("ticker") or "").upper() != tk]
        data.append(pos)
        save_portfolio(data)
    return pos


def _log_closed(pos: dict[str, Any]) -> None:
    try:
        m = metrics(pos)
        rec = {
            "ticker": m["ticker"], "strategy": m["strategy"],
            "entry_price": m["entry_price"], "entry_date": m["entry_date"],
            "exit_date": ed.today_et().isoformat(), "exit_mark": m["current_price"],
            "pct": round(float(m["pct"]), 3), "r_mult": round(float(m["r_mult"]), 3),
        }
        path = PORTFOLIO_PATH.with_name("closed_trades.jsonl")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:  # noqa: BLE001
        pass


def remove_ticker(ticker: str) -> None:
    tk = (ticker or "").upper().strip()
    with _PF_LOCK:
        rows = _rows_for_write()
        for p in rows:
            if str(p.get("ticker") or "").upper() == tk:
                _log_closed(p)
        save_portfolio([p for p in rows if str(p.get("ticker") or "").upper() != tk])


def holding_mark(pos: dict[str, Any]) -> float:
    ticker = str(pos.get("ticker") or "").upper().strip()
    px = SHARED.quote(ticker)
    if px > 0:
        return px
    try:
        return float(pos.get("entry_price") or 0)
    except (TypeError, ValueError):
        return 0.0


def sync_earnings_dates(cal: dict[str, str]) -> int:
    """Refresh earnings_date of positions from the fresh calendar. Manual dates untouched."""
    n = 0
    with _PF_LOCK:
        rows = _rows_for_write()
        for p in rows:
            if str(p.get("date_src") or "").upper() == "MANUAL":
                continue
            tk = str(p.get("ticker") or "").upper()
            new = cal.get(tk)
            if new and new != str(p.get("earnings_date") or ""):
                p["earnings_date"] = new
                p["date_src"] = "CAL"
                n += 1
        if n:
            save_portfolio(rows)
    return n


def metrics(pos: dict[str, Any]) -> dict[str, Any]:
    ticker = str(pos.get("ticker") or "").upper()
    entry = float(pos.get("entry_price") or 0)
    shares = float(pos.get("shares") or 0)
    stop = float(pos.get("stop_price") or 0)
    consensus = float(pos.get("target_consensus") or 0)
    buy_ratio = float(pos.get("buy_ratio") or 0)
    px = holding_mark(pos)
    one_r = max(entry - stop, 0.01)
    r_mult = (px - entry) / one_r if one_r and px > 0 else 0.0
    pct = ((px - entry) / entry) * 100.0 if entry and px > 0 else 0.0
    pnl = (px - entry) * shares if px > 0 else 0.0
    raw_date = str(pos.get("entry_date") or date.today().isoformat())
    strategy = str(pos.get("strategy") or "EARNINGS").upper().strip()
    if strategy not in ("EARNINGS", "SQUEEZE"):
        strategy = pos.get("strategy") or "LEGACY"
    e_raw = str(pos.get("earnings_date") or "").strip()
    return {
        "ticker": ticker, "strategy": strategy, "entry_price": entry, "shares": shares,
        "stop_price": stop, "entry_date": raw_date, "target_consensus": consensus,
        "buy_ratio": buy_ratio, "current_price": px, "one_r": one_r, "r_mult": r_mult,
        "pct": pct, "pnl": pnl,
        "upside": ((consensus - px) / px * 100.0) if consensus > 0 and px > 0 else 0.0,
        "has_quote": SHARED.quote(ticker) > 0, "earnings_date": e_raw,
        "d_day": ed.days_to_event(e_raw) if e_raw else None,
        "force_exit_d3": ed.must_exit_for_earnings(e_raw) if e_raw else False,
        "hold_bdays": ed.business_days_held(raw_date),
    }


def guardian(m: dict[str, Any]) -> dict[str, Any]:
    """4대 청산 헌법: FRIDAY FLAT / TP / SL / D-3, 순서는 config.EXIT_PRIORITY."""
    strategy = str(m.get("strategy") or "")
    if strategy not in ("EARNINGS", "SQUEEZE"):
        return {
            "ticker": m.get("ticker", ""), "code": "LEGACY", "rank": 9, "alert": "warning",
            "badge": "⚠️ [레거시 전략 — 수동 검토]",
            "order": "이 포지션은 폐기된 전략으로 등록되어 자동 진단을 지원하지 않습니다. "
                     "토스 앱에서 직접 확인 후 정리하십시오.",
            "days": int(m.get("hold_bdays") or 1), "structure": "미지원 전략",
            "r_mult": float(m.get("r_mult") or 0), "pct": float(m.get("pct") or 0),
        }

    tk = str(m.get("ticker") or "")
    pct = float(m.get("pct") or 0)
    hold = int(m.get("hold_bdays") or 1)
    d_day = m.get("d_day")
    tp, sl = float(config.EXIT_TP_PCT), float(config.EXIT_SL_PCT)
    quoted = bool(m.get("has_quote"))
    friday = ed.is_friday_flat_window()
    force_d3 = bool(m.get("force_exit_d3"))

    rules: dict[str, dict[str, Any]] = {
        "FRIDAY": {
            "hit": friday,
            "code": "FRIDAY", "rank": 0, "alert": "warning",
            "badge": "🟠 [FRIDAY FLAT]",
            "order": "금요일 마감이 가까워집니다. 주말 갭다운 리스크를 피하기 위해 지금 전량 시장가 매도하십시오.",
        },
        "TP": {
            "hit": quoted and pct >= tp,
            "code": "TP", "rank": 1, "alert": "success",
            "badge": f"🟡 [목표 달성 익절 +{tp:.1f}%]",
            "order": f"목표 수익(+{tp:.1f}%) 달성! 욕심부리지 말고 지금 전량 시장가 익절하십시오.",
        },
        "SL": {
            "hit": quoted and pct <= -sl,
            "code": "SL", "rank": 0, "alert": "error",
            "badge": f"🔴 [기계적 손절 -{sl:.1f}%]",
            "order": f"손절선(-{sl:.1f}%) 도달! 즉시 손절하여 손실을 최소화하십시오.",
        },
        "D3": {
            "hit": force_d3,
            "code": "D3", "rank": 0, "alert": "error",
            "badge": "🚨 [D-3 강제 청산]",
            "order": "실적 발표 3일 전입니다! 어닝 갭 리스크를 피하기 위해 손익 무관 지금 전량 시장가 매도하십시오.",
        },
    }

    for key in config.EXIT_PRIORITY:
        rule = rules.get(key)
        if rule and rule["hit"]:
            structure = f"실적 D-{int(d_day)}" if d_day is not None else f"{hold}거래일 보유"
            return {
                "ticker": tk, "code": rule["code"], "rank": rule["rank"], "alert": rule["alert"],
                "badge": rule["badge"], "order": rule["order"], "days": hold,
                "structure": structure, "r_mult": float(m.get("r_mult") or 0), "pct": pct,
            }

    if not quoted:
        return {
            "ticker": tk, "code": "NOQUOTE", "rank": 0, "alert": "error",
            "badge": "⛔ [시세 끊김]",
            "order": "시세 수신이 없습니다. 토스 앱에서 직접 확인하십시오.",
            "days": hold, "structure": "시세 없음", "r_mult": 0.0, "pct": 0.0,
        }

    when = f"D-{int(d_day)}일 전" if d_day is not None else "실적일 미상"
    order = f"현재 안전 구간입니다({when}). 목표(+{tp:.1f}%)/손절(-{sl:.1f}%)선 도달 전까지 편안히 홀딩하십시오."
    low_upside = (
        strategy == "SQUEEZE"
        and float(m.get("target_consensus") or 0) > 0
        and float(m.get("upside") or 0) < float(config.SQUEEZE_UPSIDE_MIN_PCT)
    )
    if low_upside:
        order += (
            f" ⚠️ 단, 월가 목표가 기준 상승여력이 {float(m.get('upside') or 0):+.1f}%로 "
            f"{config.SQUEEZE_UPSIDE_MIN_PCT:.0f}% 미만인 저탄력 구간입니다 — 수급상 숏커버링이 "
            "임박했더라도 스캘핑 관점으로만 짧게 보십시오."
        )
    return {
        "ticker": tk, "code": "HOLD", "rank": 6, "alert": "success",
        "badge": "🟢 [순항 홀딩]" if not low_upside else "🟡 [저탄력 순항]",
        "order": order,
        "days": hold, "structure": f"실적 {when}" if d_day is not None else f"{hold}거래일 보유",
        "r_mult": float(m.get("r_mult") or 0), "pct": pct,
    }


def lead_guardian(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for row in rows:
        item = guardian(row)
        if best is None or int(item["rank"]) < int(best["rank"]):
            best = item
    return best
```

### `engine.py`

**목적지:** 프로젝트 루트의 `engine.py` 를 이 내용으로 통째 교체한다. 줄 수 199.

```python
"""이벤트 드리븐 스캔(실적런업 + 숏스퀴즈) + 실시간 레이더 하트비트. No broker send. No LDPB."""
from __future__ import annotations

import threading
from typing import Any

import pandas as pd

import env_settings
from data import alpaca_feed, fmp
from data.yf_download import download_daily, spy as load_spy, wikipedia_universe
from screener import event_driven, portfolio, regime
from slog import slog
from state import SHARED

_SCAN_THREAD: threading.Thread | None = None
_SCAN_THREAD_LOCK = threading.Lock()


def healthcheck() -> None:
    SHARED.fmp_ok = fmp.ping()
    SHARED.alpaca_ok = alpaca_feed.ping()
    slog(f"ping FMP={SHARED.fmp_ok} ALPACA={SHARED.alpaca_ok}")


def _universe() -> list[dict[str, Any]]:
    SHARED.set_phase("UNIVERSE")
    SHARED.set_banner("FMP 공용 유니버스 확장 중…")
    try:
        rows = event_driven.fetch_universe()
        if not rows:
            raise RuntimeError("빈 유니버스")
        SHARED.fmp_ok = True
        return rows
    except Exception as exc:  # noqa: BLE001
        SHARED.scan_error = f"FMP screener 폴백: {exc}"
        SHARED.fmp_ok = False
        rows = wikipedia_universe()
        slog(f"FMP 실패 → 위키폴백 {len(rows)}개 ({exc})")
        return rows


def get_active_watchlist() -> list[str]:
    """실적런업 Top10 ∪ 숏스퀴즈 Top10 ∪ portfolio.json. 매 폴링마다 재계산."""
    snap = SHARED.snapshot()
    seen: set[str] = set()
    out: list[str] = []
    for pool_key in ("earnings_targets", "squeeze_targets"):
        for row in snap.get(pool_key) or []:
            tk = str(row.get("ticker") or "").upper().strip()
            if tk and tk not in seen:
                seen.add(tk)
                out.append(tk)
    try:
        held = [str(p.get("ticker") or "").upper().strip() for p in portfolio.load_portfolio()]
    except Exception:  # noqa: BLE001
        held = []
    for tk in held:
        if tk and tk not in seen:
            seen.add(tk)
            out.append(tk)
    if "KRW=X" not in seen:
        out.append("KRW=X")
    return out


def ensure_realtime() -> None:
    if not isinstance(SHARED.live_quotes, dict):
        SHARED.live_quotes = {}
    from data_feed import start_dual_realtime_engine

    start_dual_realtime_engine(get_active_watchlist, SHARED.live_quotes, SHARED._lock)


def start_heartbeat() -> None:
    SHARED.is_scanning = True
    ensure_realtime()


def ensure_heartbeat() -> None:
    ensure_realtime()


def _claim_scan() -> bool:
    with SHARED._lock:
        if SHARED.scan_running:
            return False
        SHARED.scan_running = True
        return True


def run_scan() -> None:
    """유니버스 → 일봉 → 실적런업 + 숏스퀴즈 동시 스캔 → 레이더 하트비트 유지.

    UI 스레드에서 호출하지 말 것. start_scan_async() 가 백그라운드로 돌린다.
    """
    if not _claim_scan():
        slog("스캔 이미 진행 중. 중복 실행 거부.")
        return
    if not env_settings.api_keys_ready():
        SHARED.scan_error = "FMP_API_KEY / ALPACA 키 없음."
        SHARED.set_phase("NO_KEYS")
        SHARED.set_banner("키가 없다. 사이드바에 입력.")
        SHARED.scan_running = False
        return

    SHARED.is_scanning = False
    SHARED.scan_error = ""
    SHARED.set_phase("START")
    SHARED.set_banner("스캔 시작…")
    try:
        slog("스캔 시작")
        healthcheck()
        rows = _universe()
        SHARED.universe_n = len(rows)
        meta = {str(r.get("symbol") or "").upper(): r for r in rows if r.get("symbol")}
        tickers = [t for t in meta if t]
        if "SPY" not in tickers:
            tickers.append("SPY")
        SHARED.scan_total = len(tickers)
        SHARED.scan_done = 0
        SHARED.set_phase(f"EOD 0/{len(tickers)}")
        SHARED.set_banner(f"일봉 수신 {len(tickers)}종…")
        hist = download_daily(tickers)
        slog(f"일봉 확보 {len(hist)}")

        spy_df = hist.get("SPY")
        if spy_df is None or getattr(spy_df, "empty", True):
            spy_df = load_spy()
        SHARED.regime = regime.get_market_regime(spy_df)
        slog(
            f"국면 {SHARED.regime.get('state')} "
            f"분산 {SHARED.regime.get('distribution_days')} {SHARED.regime.get('detail')}"
        )

        SHARED.set_phase("EARNINGS")
        SHARED.set_banner("실적 런업 채점 중…")
        earn_top, earn_note, cal = event_driven.scan_earnings(rows, hist)
        SHARED.set_earnings_targets(earn_top, earn_note)
        changed = portfolio.sync_earnings_dates(cal) if cal else 0
        slog(f"실적 런업 Top {len(earn_top)} / 보유 실적일 갱신 {changed}")

        SHARED.set_phase("SQUEEZE")
        SHARED.set_banner("숏스퀴즈 채점 중…")
        sqz_top, sqz_note = event_driven.scan_squeeze(rows, hist)
        SHARED.set_squeeze_targets(sqz_top, sqz_note)
        slog(f"숏스퀴즈 Top {len(sqz_top)}")

        watched = get_active_watchlist()
        slog(f"실시간 감시 {len(watched)}종: {', '.join(watched)}")
        ensure_realtime()
        SHARED.set_phase("LIVE")
        if not earn_top and not sqz_top:
            SHARED.scan_error = "실적런업/숏스퀴즈 모두 0건. 캘린더/공매도 데이터 확인."
            SHARED.set_banner("분석 완료. 표시 종목 0.")
        else:
            SHARED.scan_error = ""
            SHARED.set_banner(f"🟢 레이더 가동. 실적런업 {len(earn_top)} · 숏스퀴즈 {len(sqz_top)}.")
    except Exception as exc:  # noqa: BLE001
        slog(f"스캔 실패 {exc}")
        SHARED.scan_error = str(exc)
        SHARED.set_phase("ERROR")
        SHARED.set_banner(f"스캔 실패: {exc}")
    finally:
        SHARED.scan_running = False
        ensure_realtime()
        SHARED.is_scanning = bool(SHARED.earnings_targets or SHARED.squeeze_targets)
        if not SHARED.is_scanning:
            slog("스캔 완료. 표시 종목 0.")


def start_scan() -> None:
    run_scan()


def start_scan_async() -> bool:
    """UI 스레드를 막지 않는다. True = 스레드 기동. False = 이미 돌거나 키 없음."""
    global _SCAN_THREAD
    if not env_settings.api_keys_ready():
        SHARED.scan_error = "FMP_API_KEY / ALPACA 키 없음."
        SHARED.set_phase("NO_KEYS")
        SHARED.set_banner("키가 없다. 사이드바에 입력.")
        return False
    with _SCAN_THREAD_LOCK:
        if SHARED.scan_running:
            return False
        alive = _SCAN_THREAD is not None and _SCAN_THREAD.is_alive()
        if alive:
            return False
        _SCAN_THREAD = threading.Thread(target=run_scan, daemon=True, name="event-scan")
        _SCAN_THREAD.start()
    return True


def stop_scan() -> None:
    SHARED.is_scanning = False
    SHARED.set_phase("STOPPED")
    SHARED.set_banner("레이더 중지 요청. 시세 피드는 유지. 진행 중 스캔은 이번 주기 끝까지 간다.")
    slog("레이더 중지")
```

### `ai_advisor.py`

**목적지:** 프로젝트 루트의 `ai_advisor.py` 를 이 내용으로 통째 교체한다. 줄 수 255.

```python
"""Gemini 기반 AI 전술 참모. 완전 독립 모듈. 스캐너/포트폴리오 파이프라인을 import 하지 않는다.

v3.4: 프롬프트 조립과 API 호출 분리. JSON 은 행을 줄여서 자르지, 문자열 한가운데를 자르지 않는다.
  - build_prompt_text(...)        : 데이터를 받아 편집 가능한 프롬프트 '텍스트'만 돌려준다. API 호출 없음.
  - run_briefing_from_text(text)  : 완성된(혹은 사용자가 수정한) 프롬프트 텍스트를 그대로 Gemini 에 전달한다.
  - generate_tactical_briefing(...) : 하위 호환용 래퍼. 위 두 함수를 순서대로 호출할 뿐이다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# 주의: gemini-1.5-flash 는 2025-09-29 에 완전히 shutdown 되어 더는 호출할 수 없다.
# google-generativeai 패키지 자체도 deprecated (google-genai 로 대체됨). 반드시 google-genai 사용.
try:
    from google import genai
except ImportError:
    genai = None

# 최신순 폴백 후보. Google 이 몇 달 단위로 모델을 shutdown 하므로 하나만 하드코딩하지 않는다.
# 최신 상태는 https://ai.google.dev/gemini-api/docs/deprecations 에서 확인할 것.
# 2026-09 기준: 3.8 > 3.7 > 3.6 > 3.5-flash-lite/3.5-flash(2027-05-19까지 보장) > 3-flash-preview
# > 2.5 세대 순. 신규 발급 키는 2.5 세대가 막혀 있을 수 있어 최신 세대를 먼저 시도한다.
_MODEL_CANDIDATES = [
    os.environ.get("GEMINI_MODEL", "").strip() or None,
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]
_MODEL_CANDIDATES = [m for m in _MODEL_CANDIDATES if m]

_JSON_BUDGET = 12000

_EARN_KEYS = (
    "rank", "ticker", "name", "score", "signal", "price",
    "earnings_date", "d_day", "upside", "buy_ratio", "target_consensus", "sma50",
)
_SQZ_KEYS = (
    "rank", "ticker", "name", "score", "signal", "price",
    "short_float_pct", "rvol", "upside", "days_to_cover", "target_consensus",
)
_PF_KEYS = (
    "ticker", "strategy", "entry_price", "shares", "stop_price",
    "entry_date", "earnings_date", "target_consensus", "buy_ratio", "entry_fx",
)
_REGIME_KEYS = ("state", "color", "label", "detail", "distribution_days")


def _load_gemini_key() -> str:
    """env_settings 를 우선 쓰되, 없거나 구조가 다르면 .env/환경변수를 직접 읽는다."""
    try:
        import env_settings

        keys = env_settings.get_api_keys()
        if isinstance(keys, dict) and keys.get("GEMINI_API_KEY"):
            return str(keys["GEMINI_API_KEY"]).strip()
    except Exception:  # noqa: BLE001
        pass
    env_val = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_val:
        return env_val
    env_path = Path.home() / "Desktop" / "swing-screener" / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _list_live_models(client: "genai.Client") -> list[str]:
    """최후 폴백: 이 키가 실제로 쓸 수 있는 모델을 직접 조회한다.
    curated 후보가 전부 실패했을 때만 호출한다(평소엔 API 호출 1번 아끼려고 안 부른다)."""
    try:
        names: list[str] = []
        for m in client.models.list():
            name = str(getattr(m, "name", "") or "").split("/")[-1]
            if not name or "flash" not in name:
                continue
            if any(bad in name for bad in ("image", "audio", "live", "tts", "embedding", "robotics")):
                continue
            actions = getattr(m, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            names.append(name)
        return names
    except Exception:  # noqa: BLE001
        return []


def _pick(row: Any, keys: tuple[str, ...]) -> Any:
    if not isinstance(row, dict):
        return row
    out: dict[str, Any] = {}
    for key in keys:
        if key in row:
            out[key] = row[key]
    return out


def dumps_valid_json(payload: Any, keys: tuple[str, ...] | None = None, budget: int = _JSON_BUDGET) -> str:
    """Always return parseable JSON. If over budget, drop the last list rows. Never slice the string."""
    data: Any
    if isinstance(payload, list):
        data = [_pick(row, keys) if keys else row for row in payload]
    elif isinstance(payload, dict) and keys:
        data = _pick(payload, keys)
    else:
        data = payload
    note = ""
    while True:
        try:
            text = json.dumps(data, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            text = json.dumps({"error": "serialize_failed", "repr": str(payload)[:500]}, ensure_ascii=False)
            break
        if len(text) <= budget:
            if note:
                if isinstance(data, list):
                    return json.dumps({"rows": data, "note": note}, ensure_ascii=False, default=str)
            return text
        if isinstance(data, list) and len(data) > 1:
            data = data[:-1]
            note = f"token budget {budget}: lowest ranks dropped, {len(data)} rows kept"
            continue
        if isinstance(data, dict) and data:
            data = {"truncated": True, "keys": list(data.keys())[:12]}
            continue
        return json.dumps({"error": "payload_too_large", "budget": budget}, ensure_ascii=False)


def build_prompt_text(
    regime_data: Any,
    portfolio_data: Any,
    runup_data: Any,
    squeeze_data: Any,
    extra_questions: str = "",
    session_meta: Any = None,
) -> str:
    """프롬프트 텍스트만 조립. API 호출 없음. 호출자가 실행 시점 데이터를 넣어야 한다."""

    extra = (extra_questions or "").strip()
    extra_block = extra if extra else "(추가 질문 없음)"
    session_json = dumps_valid_json(session_meta or {}, budget=2000)
    return f"""당신은 월가 1티어 프랍 트레이딩 펌의 수석 퀀트 참모입니다. 트레이더가 방금 스캔한 아래 데이터를 보고,
감정 빼고 기계적으로 다음 5가지 질문에 답하십시오.

[절대 안전 헌법 — 위반 시 답변 전체가 무효]
- 아래 [세션 메타]의 key 가 PRE / CLOSED / AH 이거나 fire_alert_allowed 가 false 이면
  신규 매수 추천을 금지한다. 관망만 허용한다. "오늘 밤 사라"는 문장을 쓰지 마라.
- 정규장 개장 후 15분(09:45 ET) 이전에는 사격 신호가 있어도 진입 금지라고 명시하라.
- 위 데이터에 없는 숫자(진입가, 손절가, 목표가, 승률, 예상 수익률)를 만들지 마라.
  없으면 "데이터 없음"이라고 써라.
- JSON 이 비어 있거나 0건이면 그 사실을 인정하고 억지 추천을 만들지 마라.

[세션 메타]
{session_json}

[현재 시스템 데이터]
- 시장 국면: {dumps_valid_json(regime_data, _REGIME_KEYS, 2000)}
- 트레이더의 현재 포트폴리오 (보유 종목): {dumps_valid_json(portfolio_data, _PF_KEYS)}
- 실적 런업 Top 후보: {dumps_valid_json(runup_data, _EARN_KEYS)}
- 숏스퀴즈 Top 후보 (상승여력 5% 미만은 이미 제외됨): {dumps_valid_json(squeeze_data, _SQZ_KEYS)}

[당신의 임무: 5대 전술 명령 작성]
구구절절한 설명은 빼고, 군대식으로 차갑고 명확하게 행동(Action) 위주로 아래 5개 번호를 매겨 작성할 것.
1. [시황 판독]: 현재 시장 국면과 세션 메타를 바탕으로 지금 공격적으로 매수할지, 보수적으로 쉴지 딱 1줄로 지시하라.
2. [내 계좌 생사 판결]: 포트폴리오 보유 종목들을 분석하여 당장 익절/손절/홀딩/교체 중 어떤 행동을 해야 하는지
   종목별로 명확히 1줄씩 지시하라. 위 데이터에 이미 계산된 손절가/목표가/D-Day 가 있으면 그 수치를 그대로 인용하라.
   데이터에 없는 수치를 새로 만들어내지 마라.
3. [런업 타겟 추천]: 실적 런업 후보 중 정규장 09:45 ET 이후 진입이 가능한 1종목만 고르거나, 없으면 사지 말라고 단호히 말하라.
   진입가/손절가가 데이터에 있을 때만 그 수치를 그대로 명시하라.
4. [스퀴즈 타겟 추천]: 숏스퀴즈 후보 중 가장 점수가 높은 1종목을 고르되, 세션 인터락이 걸려 있으면 관망만 지시하라.
   상승여력 수치가 있으면 같이 언급하라.
5. [최종 자금 집행 명령]: 트레이더는 실적 런업 120만 원 / 숏스퀴즈 60만 원 / 현금 버퍼 120만 원이다.
   세션이 허용할 때만 슬롯을 채운다. 어떤 주식을 들고, 무엇을 비울지 한 줄로 요약하라.

위 데이터에 특정 값이 비어 있거나 0건이면, 그 사실을 그대로 인정하고 억지로 추천을 만들어내지 마라.

[추가 질문/지시사항]
{extra_block}"""


# 구버전 호출부 호환용 별칭
_build_prompt = build_prompt_text


def run_briefing_from_text(prompt_text: str) -> str:
    """완성된(혹은 사용자가 수정한) 프롬프트 텍스트를 그대로 Gemini 에 전달한다.
    데이터 조립은 전혀 하지 않는다 — 호출자가 무엇을 보냈든 그대로 보낸다."""
    if genai is None:
        return "❌ google-genai 패키지가 설치되어 있지 않습니다. `pip install google-genai` 후 다시 시도하십시오."

    text = (prompt_text or "").strip()
    if not text:
        return "❌ 프롬프트가 비어 있습니다. 분석할 내용을 입력한 뒤 다시 시도하십시오."

    api_key = _load_gemini_key()
    if not api_key:
        return "❌ GEMINI_API_KEY가 설정되지 않았습니다. 사이드바나 .env를 확인하십시오."

    try:
        client = genai.Client(api_key=api_key)
    except Exception as exc:  # noqa: BLE001
        return f"❌ Gemini 클라이언트 생성 실패: {exc}"

    tried: list[str] = []
    errors: list[str] = []

    def _attempt(model_name: str) -> str | None:
        if model_name in tried:
            return None
        tried.append(model_name)
        try:
            response = client.models.generate_content(model=model_name, contents=text)
            out = getattr(response, "text", None)
            if out:
                return out
            errors.append(f"{model_name}: 응답이 비어 있음")
        except Exception as exc:  # noqa: BLE001
            # 404 = 모델명이 틀렸거나 이 키에 없음 / 403 = 권한(요금제) 문제 / 429 = 쿼터 초과.
            errors.append(f"{model_name}: {exc}")
        return None

    for model_name in _MODEL_CANDIDATES:
        result = _attempt(model_name)
        if result:
            return result

    live_models = [m for m in _list_live_models(client) if m not in tried]
    for model_name in live_models[:5]:  # 과도한 재시도 방지, 상위 5개만
        result = _attempt(model_name)
        if result:
            return result

    detail = "\n".join(f"  - {e}" for e in errors) or "(오류 상세 없음)"
    return (
        "❌ AI 분석 중 오류가 발생했습니다: 시도한 모델이 모두 실패했습니다.\n"
        f"{detail}\n"
        "https://ai.google.dev/gemini-api/docs/deprecations 에서 현재 사용 가능한 모델명을 "
        "확인해 GEMINI_MODEL 환경변수로 지정하거나, 위 에러가 403/권한 관련이면 "
        "Google AI Studio에서 이 API 키의 요금제/모델 접근 권한을 확인하십시오."
    )


def generate_tactical_briefing(regime_data: Any, portfolio_data: Any, runup_data: Any, squeeze_data: Any) -> str:
    """하위 호환용 래퍼. build_prompt_text() + run_briefing_from_text() 를 순서대로 호출한다."""
    prompt = build_prompt_text(regime_data, portfolio_data, runup_data, squeeze_data)
    return run_briefing_from_text(prompt)
```

### `data_feed.py`

**목적지:** 프로젝트 루트의 `data_feed.py` 를 이 내용으로 통째 교체한다. 줄 수 560.

```python
"""FMP 1.0s quote poll + Alpaca IEX websocket. Display prices only. No orders."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from typing import Any, Callable
from urllib.parse import quote

import aiohttp
import websockets

import env_settings
from slog import slog

WS_URL = "wss://stream.data.alpaca.markets/v2/iex"
_started = False
_start_lock = threading.Lock()
_feed_thread: threading.Thread | None = None


def _term(line: str) -> None:
    """Streamlit이 stdout을 삼켜도 기동 터미널에 남긴다."""
    text = line if line.endswith("\n") else line + "\n"
    try:
        sys.__stderr__.write(text)
        sys.__stderr__.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        print(text, end="", flush=True)
    except Exception:  # noqa: BLE001
        pass


def _keys() -> tuple[str, str, str]:
    env_settings.load()
    fmp = env_settings.fmp_key()
    key, secret = env_settings.alpaca_keys()
    return fmp, key, secret


def _note_feed(feed: str, tick: bool = True) -> None:
    from state import SHARED

    SHARED.note_feed(feed, tick=tick)


def _put(store: dict[str, Any], lock: threading.Lock, sym: str, **fields: Any) -> None:
    sym = sym.upper().strip()
    if not sym:
        return
    with lock:
        row = store.get(sym)
        if not isinstance(row, dict):
            row = {}
        for name, value in fields.items():
            if name == "price" and not value:
                continue
            row[name] = value
        store[sym] = row


def _apply_ws(message: str | bytes, store: dict[str, Any], lock: threading.Lock) -> None:
    try:
        data = json.loads(message)
    except Exception:  # noqa: BLE001
        return
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return
    for event in data:
        if not isinstance(event, dict):
            continue
        sym = str(event.get("S") or "").upper().strip()
        if not sym:
            continue
        kind = event.get("T")
        if kind == "t":
            try:
                price = float(event.get("p") or 0)
            except (TypeError, ValueError):
                continue
            if price > 0:
                _put(
                    store,
                    lock,
                    sym,
                    price=price,
                    last_trade=price,
                    trade_ts=time.time(),
                    source="IEX_TRADE",
                )
                _note_feed("alpaca", tick=True)
        elif kind == "q":
            try:
                bid = float(event.get("bp") or 0)
                ask = float(event.get("ap") or 0)
            except (TypeError, ValueError):
                continue
            with lock:
                row = store.get(sym)
                has_trade = isinstance(row, dict) and "last_trade" in row
                src = str(row.get("source") or "") if isinstance(row, dict) else ""
            fields: dict[str, Any] = {"bid": bid, "ask": ask}
            if (
                bid > 0
                and ask > 0
                and not has_trade
                and src not in ("PRE_MARKET", "POST_MARKET", "REGULAR", "LIVE")
            ):
                fields["price"] = round((bid + ask) / 2, 2)
                fields["source"] = "IEX_QUOTE"
            _put(store, lock, sym, **fields)
            _note_feed("alpaca", tick=True)


async def _sync_subs(websocket: Any, subscribed: set[str], tickers: list[str]) -> None:
    want = {str(t).upper().strip() for t in tickers if t}
    drop = subscribed - want
    add = want - subscribed
    if drop:
        await websocket.send(
            json.dumps({"action": "unsubscribe", "trades": sorted(drop), "quotes": sorted(drop)})
        )
        subscribed -= drop
    if add:
        names = sorted(add)
        await websocket.send(json.dumps({"action": "subscribe", "trades": names, "quotes": names}))
        subscribed |= add
        _term(f"[Alpaca IEX] 신규 실시간 구독 반영: {names}")


async def alpaca_iex_ws_loop(
    get_tickers_func: Callable[[], list[str]],
    live_quotes_dict: dict[str, Any],
    lock: threading.Lock,
) -> None:
    while True:
        try:
            _, key, secret = _keys()
            if not key or not secret:
                await asyncio.sleep(2)
                continue
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as websocket:
                conn_msg = await asyncio.wait_for(websocket.recv(), timeout=5)
                await websocket.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
                auth_res = await asyncio.wait_for(websocket.recv(), timeout=5)
                if "authenticated" not in str(auth_res):
                    raise RuntimeError(f"auth rejected after {conn_msg!r}: {auth_res!r}"[:240])
                _note_feed("alpaca", tick=False)
                _term(f"[Alpaca IEX] 웹소켓 인증 완벽 성공: {auth_res}")
                subscribed: set[str] = set()
                last_sync = 0.0
                while True:
                    now_mono = time.monotonic()
                    if now_mono - last_sync >= 1.0:
                        last_sync = now_mono
                        current = {
                            str(t).upper().strip()
                            for t in (get_tickers_func() or [])
                            if t and "=" not in str(t)
                        }
                        if current != subscribed:
                            await _sync_subs(websocket, subscribed, sorted(current))
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                        _apply_ws(message, live_quotes_dict, lock)
                    except asyncio.TimeoutError:
                        _note_feed("alpaca", tick=False)
                        continue
        except Exception as exc:  # noqa: BLE001
            slog(f"Alpaca IEX 웹소켓 재접속: {exc}")
            _term(f"[WS ERROR] Alpaca IEX 연결 재시도: {exc}")
            await asyncio.sleep(2)


def _dual_sample(store: dict[str, Any], lock: threading.Lock, tickers: list[str]) -> str:
    with lock:
        pairs = []
        for sym, row in list(store.items())[:2]:
            px = row.get("price") if isinstance(row, dict) else row
            pairs.append(f"{sym}: ${px}")
    if not pairs:
        pairs = [f"{sym}: $—" for sym in tickers[:2]]
    return "[DUAL 1.0s 실시간] " + ", ".join(pairs)


def _apply_fmp_row(row: dict[str, Any], store: dict[str, Any], lock: threading.Lock) -> None:
    sym = str(row.get("symbol") or "").upper().strip()
    if not sym:
        return
    try:
        price = float(row.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    try:
        day_high = float(row.get("dayHigh") or row.get("day_high") or 0)
    except (TypeError, ValueError):
        day_high = 0.0
    try:
        volume = int(float(row.get("volume") or 0))
    except (TypeError, ValueError):
        volume = 0
    with lock:
        current = store.get(sym)
        if not isinstance(current, dict):
            current = {}
        current["dayHigh"] = day_high
        current["volume"] = volume
        trade_ts = float(current.get("trade_ts") or 0)
        fresh_trade = (
            current.get("source") == "IEX_TRADE" and (time.time() - trade_ts) < 1.0
        )
        if price > 0 and not fresh_trade:
            current["price"] = price
            current["source"] = "FMP"
        store[sym] = current
    _note_feed("fmp")


async def _fmp_one(session: aiohttp.ClientSession, sym: str, key: str, base: str) -> dict[str, Any] | int | None:
    timeout = aiohttp.ClientTimeout(total=2)
    try:
        async with session.get(
            f"{base}/stable/quote",
            params={"symbol": sym, "apikey": key},
            timeout=timeout,
        ) as resp:
            if resp.status == 429:
                return 429
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


async def fmp_realtime_polling_loop(
    get_tickers_func: Callable[[], list[str]],
    live_quotes_dict: dict[str, Any],
    lock: threading.Lock,
) -> None:
    timeout = aiohttp.ClientTimeout(total=3.0)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                tickers = [str(t).upper().strip() for t in (get_tickers_func() or []) if t]
                fmp_key, _, _ = _keys()
                base = env_settings.fmp_base()
                if tickers and fmp_key:
                    symbol_str = ",".join(tickers)
                    url = f"{base}/api/v3/quote/{symbol_str}"
                    async with session.get(url, params={"apikey": fmp_key}, timeout=timeout) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if isinstance(data, list) and data:
                                for quote in data:
                                    if isinstance(quote, dict):
                                        _apply_fmp_row(quote, live_quotes_dict, lock)
                                _term(_dual_sample(live_quotes_dict, lock, tickers))
                                await asyncio.sleep(1.0)
                                continue
                        elif resp.status == 429:
                            await asyncio.sleep(3.0)
                            continue
                    rows = await asyncio.gather(
                        *[_fmp_one(session, sym, fmp_key, base) for sym in tickers],
                        return_exceptions=True,
                    )
                    if any(row == 429 for row in rows):
                        await asyncio.sleep(3.0)
                        continue
                    applied = False
                    for row in rows:
                        if isinstance(row, dict):
                            _apply_fmp_row(row, live_quotes_dict, lock)
                            applied = True
                    if applied:
                        _term(_dual_sample(live_quotes_dict, lock, tickers))
                        await asyncio.sleep(1.0)
                        continue
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1.0)


_fx_px = 0.0
_fx_at = 0.0
_YAHOO_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}
_yahoo_blocked = False
_yahoo_blocked_until = 0.0
_yahoo_backoff_sec = 60.0
_YAHOO_BACKOFF_MIN = 60.0
_YAHOO_BACKOFF_MAX = 15 * 60.0


def _yahoo_in_backoff() -> bool:
    return time.time() < _yahoo_blocked_until


def _yahoo_trip_429() -> None:
    """Stop hitting Yahoo. 60s, then 120s, 240s, 480s, cap 15 minutes."""
    global _yahoo_blocked, _yahoo_blocked_until, _yahoo_backoff_sec
    wait = max(_YAHOO_BACKOFF_MIN, min(_yahoo_backoff_sec, _YAHOO_BACKOFF_MAX))
    _yahoo_blocked = True
    _yahoo_blocked_until = time.time() + wait
    nxt = min(_YAHOO_BACKOFF_MAX, wait * 2.0)
    _yahoo_backoff_sec = nxt
    _term(f"[LIVE] Yahoo quote 429. {wait:.0f}s 백오프. 다음 대기 {nxt:.0f}s. Nasdaq 시세 유지.")


def _yahoo_clear_backoff() -> None:
    global _yahoo_blocked, _yahoo_blocked_until, _yahoo_backoff_sec
    _yahoo_blocked = False
    _yahoo_blocked_until = 0.0
    _yahoo_backoff_sec = _YAHOO_BACKOFF_MIN


def _money(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).replace("$", "").replace(",", "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _pick_session_price(
    market_state: str, reg_price: float, pre_price: float, post_price: float
) -> tuple[float, str]:
    state = market_state.upper()
    if pre_price > 0 and state in ("PRE", "PREPRE"):
        return pre_price, "PRE_MARKET"
    if post_price > 0 and state in ("POST", "POSTPOST"):
        return post_price, "POST_MARKET"
    if reg_price > 0:
        return reg_price, "REGULAR"
    price = pre_price or reg_price or post_price or 0.0
    return price, "LIVE"


def _write_live_price(
    store: dict[str, Any],
    lock: threading.Lock,
    sym: str,
    price: float,
    day_high: float,
    volume: int,
    source: str,
) -> None:
    if price <= 0:
        return
    with lock:
        current = store.get(sym)
        if not isinstance(current, dict):
            current = {}
        current["price"] = price
        if day_high > 0:
            current["dayHigh"] = day_high
        if volume > 0:
            current["volume"] = volume
        current["source"] = source
        store[sym] = current


def _nasdaq_fields(sym: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    primary = data.get("primaryData") if isinstance(data.get("primaryData"), dict) else {}
    secondary = data.get("secondaryData") if isinstance(data.get("secondaryData"), dict) else {}
    status = str(data.get("marketStatus") or "")
    live = _money(primary.get("lastSalePrice"))
    prev = _money(secondary.get("lastSalePrice"))
    state = status.upper()
    pre = post = reg = 0.0
    if "PRE" in state:
        session = "PRE"
        pre, reg = live, prev
    elif "POST" in state or "AFTER" in state:
        session = "POST"
        post, reg = live, prev
    else:
        session = "REGULAR"
        reg = live or prev
    try:
        volume = int(_money(primary.get("volume")))
    except (TypeError, ValueError):
        volume = 0
    if volume < 1000:
        volume = 0
    return {
        "symbol": sym,
        "marketState": session,
        "regularMarketPrice": reg,
        "preMarketPrice": pre,
        "postMarketPrice": post,
        "regularMarketDayHigh": 0.0,
        "regularMarketVolume": volume,
    }


async def _nasdaq_one(session: aiohttp.ClientSession, sym: str) -> dict[str, Any] | None:
    url = f"https://api.nasdaq.com/api/quote/{sym}/info?assetclass=stocks"
    try:
        async with session.get(
            url,
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json(content_type=None)
    except Exception:  # noqa: BLE001
        return None
    return _nasdaq_fields(sym, payload if isinstance(payload, dict) else {})


def _apply_quote_item(item: dict[str, Any], store: dict[str, Any], lock: threading.Lock) -> bool:
    sym = str(item.get("symbol") or "").upper().strip()
    if not sym:
        return False
    reg_price = _money(item.get("regularMarketPrice"))
    pre_price = _money(item.get("preMarketPrice"))
    post_price = _money(item.get("postMarketPrice"))
    day_high = _money(item.get("regularMarketDayHigh"))
    try:
        volume = int(_money(item.get("regularMarketVolume")))
    except (TypeError, ValueError):
        volume = 0
    price, source = _pick_session_price(
        str(item.get("marketState") or ""), reg_price, pre_price, post_price
    )
    if price <= 0:
        return False
    _write_live_price(store, lock, sym, price, day_high, volume, source)
    return True


async def _krw_rate(session: aiohttp.ClientSession) -> float:
    """Yahoo KRW=X is 429 here. Frankfurter is the USD/KRW print until Yahoo answers."""
    global _fx_px, _fx_at
    if _fx_px > 500 and time.time() - _fx_at < 60:
        return _fx_px
    try:
        async with session.get(
            "https://api.frankfurter.app/latest?from=USD&to=KRW",
            timeout=aiohttp.ClientTimeout(total=3),
        ) as resp:
            if resp.status != 200:
                return _fx_px
            data = await resp.json(content_type=None)
        px = float(((data or {}).get("rates") or {}).get("KRW") or 0)
    except Exception:  # noqa: BLE001
        return _fx_px
    if px > 500:
        _fx_px = px
        _fx_at = time.time()
    return _fx_px


async def realtime_quote_polling_loop(
    get_tickers_func: Callable[[], list[str]],
    live_quotes_dict: dict[str, Any],
    lock: threading.Lock,
) -> None:
    """Yahoo v7 batch first. 429 trips exponential backoff 60s→15m. That cycle uses Nasdaq."""
    timeout = aiohttp.ClientTimeout(total=8.0)
    async with aiohttp.ClientSession(headers=_YAHOO_UA, timeout=timeout) as session:
        while True:
            try:
                tickers = [str(t).upper().strip() for t in (get_tickers_func() or []) if t]
                if tickers:
                    applied = False
                    if not _yahoo_in_backoff():
                        symbol_str = ",".join(quote(sym, safe="") for sym in tickers)
                        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol_str}"
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                            if resp.status == 200:
                                _yahoo_clear_backoff()
                                data = await resp.json(content_type=None)
                                results = []
                                if isinstance(data, dict):
                                    results = (data.get("quoteResponse") or {}).get("result") or []
                                for item in results:
                                    if isinstance(item, dict) and _apply_quote_item(item, live_quotes_dict, lock):
                                        applied = True
                            elif resp.status == 429:
                                _yahoo_trip_429()
                    if not applied:
                        stocks = [sym for sym in tickers if "=" not in sym]
                        rows = await asyncio.gather(
                            *[_nasdaq_one(session, sym) for sym in stocks],
                            return_exceptions=True,
                        )
                        for row in rows:
                            if isinstance(row, dict) and _apply_quote_item(row, live_quotes_dict, lock):
                                applied = True
                    if "KRW=X" in tickers:
                        with lock:
                            fx_row = live_quotes_dict.get("KRW=X")
                            yahoo_fx = (
                                isinstance(fx_row, dict)
                                and float(fx_row.get("price") or 0) > 500
                                and fx_row.get("source") != "FX"
                            )
                        if not yahoo_fx:
                            fx = await _krw_rate(session)
                            if fx > 500:
                                _write_live_price(live_quotes_dict, lock, "KRW=X", fx, 0, 0, "FX")
                                applied = True
                    if applied:
                        _note_feed("fmp")
                        _term(_dual_sample(live_quotes_dict, lock, tickers))
                        await asyncio.sleep(1.0)
                        continue
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1.0)


def start_dual_realtime_engine(
    get_tickers_func: Callable[[], list[str]],
    live_quotes_dict: dict[str, Any],
    lock: threading.Lock | None = None,
) -> None:
    """One daemon. A dead thread is started again. Watchlist is re-read once a second."""
    global _started, _feed_thread
    gate = lock or threading.Lock()

    def run_async_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.create_task(alpaca_iex_ws_loop(get_tickers_func, live_quotes_dict, gate))
        loop.create_task(realtime_quote_polling_loop(get_tickers_func, live_quotes_dict, gate))
        loop.run_forever()

    with _start_lock:
        if _feed_thread is not None and _feed_thread.is_alive():
            return
        _started = True
        _feed_thread = threading.Thread(target=run_async_loop, daemon=True, name="ldpb-dual-feed")
        _feed_thread.start()
    slog("듀얼 실시간 엔진 가동. LIVE 1.0s + Alpaca IEX")
```

### `app.py`

**목적지:** 프로젝트 루트의 `app.py` 를 이 내용으로 통째 교체한다. 줄 수 734.

```python
"""이벤트 드리븐 스윙 콕핏 v3.0. 풀와이드 3탭: 🎯 듀얼 사냥 데스크 / 🛡️ 수호 & 포트폴리오 / 🧠 AI."""
from __future__ import annotations

import fd_limit  # noqa: F401

import time
import warnings

warnings.filterwarnings("ignore")

import pandas as pd
import streamlit as st

import ai_advisor
import config
import engine
import env_settings
from screener import event_driven as ed
from screener import portfolio, radar
from state import SHARED

env_settings.load()
_keys_ready = env_settings.api_keys_ready()

st.set_page_config(
    page_title=config.APP_TITLE,
    layout="wide",
    initial_sidebar_state="expanded",
)
if "is_scanning" not in st.session_state:
    st.session_state.is_scanning = False

_FIRE_HOLD = "🌙 장전 대기 (09:45 ET)"
_EXIT_CODES = frozenset({"SL", "D3", "FRIDAY"})

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
        "🔑 FMP / Alpaca / Gemini",
        expanded=(not env_settings.api_keys_ready()) or not (_saved_keys.get("GEMINI_API_KEY") or "").strip(),
    ):
        fmp_in = st.text_input("FMP_API_KEY", key="api_input_FMP_API_KEY")
        alpaca_key_in = st.text_input("ALPACA_API_KEY", key="api_input_ALPACA_API_KEY")
        alpaca_secret_in = st.text_input("ALPACA_SECRET_KEY", key="api_input_ALPACA_SECRET_KEY", type="password")
        gemini_in = st.text_input(
            "GEMINI_API_KEY",
            key="api_input_GEMINI_API_KEY",
            type="password",
            help="AI 전술 통제소 탭에서 사용. 없어도 기존 스캐너는 정상 동작한다.",
        )
        if st.button("💾 키 저장", type="primary", width="stretch"):
            missing = env_settings.save_api_keys_and_apply({
                "FMP_API_KEY": fmp_in,
                "ALPACA_API_KEY": alpaca_key_in,
                "ALPACA_SECRET_KEY": alpaca_secret_in,
                "GEMINI_API_KEY": gemini_in,
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
    out: list[dict] = []
    for raw in rows:
        row = dict(raw)
        if str(row.get("signal") or "").startswith("🎯"):
            row["signal"] = _FIRE_HOLD
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
    sqz = _session_gate_rows(ed.sqz_rank_live(snap.get("squeeze_targets") or [], SHARED.quote))
    return snap, earn, sqz, portfolio.load_portfolio()


def _assemble_ai_prompt(extra: str) -> str:
    snap, earn, sqz, held = _live_hunt_payload()
    return ai_advisor.build_prompt_text(
        snap.get("regime") or {},
        held,
        earn,
        sqz,
        extra_questions=extra,
        session_meta=_session_meta(),
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
        st.caption(note or "스캔 전 IDLE. 상단 🚀 스캔 시작.")
        return
    options = ["🎯 1위 자동추적"] + sorted(r["ticker"] for r in ranked)
    if st.session_state.get("earn_pick") not in options:
        st.session_state["earn_pick"] = options[0]
    pick = st.selectbox("브리핑 종목", options, key="earn_pick", label_visibility="collapsed")
    row_map = {r["ticker"]: r for r in ranked}
    brief = row_map.get(pick) or ranked[0]
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
        st.caption(note or "스캔 전 IDLE. 상단 🚀 스캔 시작.")
        return
    options = ["🎯 1위 자동추적"] + sorted(r["ticker"] for r in ranked)
    if st.session_state.get("sqz_pick") not in options:
        st.session_state["sqz_pick"] = options[0]
    pick = st.selectbox("브리핑 종목", options, key="sqz_pick", label_visibility="collapsed")
    row_map = {r["ticker"]: r for r in ranked}
    brief = row_map.get(pick) or ranked[0]
    guide = ed.squeeze_guide(brief, _usd_krw(), config.EARN_BUDGET_KRW)
    cls = {"go": "guide-go", "wait": "guide-wait", "chase": "guide-chase"}.get(guide["kind"], "guide-wait")
    st.markdown(f'<div class="{cls}">{guide["html"]}</div>', unsafe_allow_html=True)
    table = _sqz_table(ranked)
    _ring_new_shooters(table, "사격신호", ed.SQZ_FIRE, "sqz_shoot")
    st.dataframe(table, hide_index=True, width="stretch", height=380)
    if note:
        st.caption(note)


@st.fragment(run_every="1s")
def _hunt_deck_live() -> None:
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
        if g["code"] in ("SL", "D3", "FRIDAY"):
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
    tp_px = entry * (1 + config.EXIT_TP_PCT / 100.0)
    detail = (
        f'<div class="pos-metrics">목표가: ${tp_px:.2f} (+{config.EXIT_TP_PCT:g}%) | '
        f'손절선: ${float(m["stop_price"]):.2f} | {g["structure"]}</div>'
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
    running = bool(telem.get("scan_running"))
    fire_ok = radar.session_allows_fire_alert()
    interlock = "🔓 사격허용" if fire_ok else "🔒 사격잠금(09:45 ET)"
    banner = str(telem.get("banner") or "")
    if running:
        banner = f"⏳ 스캔 {phase} · {banner}"
    h0, h1, h2 = st.columns([7.4, 1.05, 1.05])
    with h0:
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
    with h1:
        if st.button("🚀 스캔 시작", width="stretch", type="primary", key="hdr-scan"):
            _kick_scan()
            st.rerun()
    with h2:
        if st.button("⏹️ 스캔 중지", width="stretch", key="hdr-stop"):
            engine.stop_scan()
            st.session_state.is_scanning = False
            st.rerun()


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
```

### `tests/test_session_clock.py`

**목적지:** 프로젝트 루트의 `tests/test_session_clock.py` 를 이 내용으로 통째 교체한다. 줄 수 89.

```python
"""P0 session clock: ET + DST. Run from repo root:

    .venv/bin/python tests/test_session_clock.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "screener" / "radar.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib.util

_radar_path = ROOT / "screener" / "radar.py"
_spec = importlib.util.spec_from_file_location("p0_radar", _radar_path)
radar = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(radar)

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")


def _kst(y, m, d, h, mi, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=KST)


CASES = [
    (_kst(2026, 10, 4, 17, 30), "CLOSED", "Sun 17:30 KST = Sun 04:30 ET"),
    (_kst(2026, 10, 4, 22, 31), "CLOSED", "Sun 22:31 KST = Sun 09:31 ET"),
    (_kst(2026, 10, 5, 2, 0), "CLOSED", "Mon 02:00 KST = Sun 13:00 ET"),
    (_kst(2026, 10, 5, 17, 30), "PRE", "Mon 17:30 KST = Mon 04:30 ET pre"),
    (_kst(2026, 10, 5, 21, 0), "PRE", "Mon 21:00 KST = Mon 08:00 ET pre"),
    (_kst(2026, 10, 5, 22, 29, 59), "PRE", "Mon 22:29:59 KST still PRE"),
    (_kst(2026, 10, 5, 22, 30, 0), "RTH", "Mon 22:30:00 KST = 09:30 ET open"),
    (_kst(2026, 10, 5, 22, 44, 59), "RTH", "Mon 22:44:59 KST RTH but fire locked"),
    (_kst(2026, 10, 5, 22, 45, 0), "RTH", "Mon 22:45:00 KST fire unlock"),
    (_kst(2026, 10, 6, 4, 59), "RTH", "Tue 04:59 KST = Mon 15:59 ET"),
    (_kst(2026, 10, 6, 5, 0), "AH", "Tue 05:00 KST = Mon 16:00 ET"),
    (_kst(2026, 10, 6, 8, 59), "AH", "Tue 08:59 KST = Mon 19:59 ET"),
    (_kst(2026, 10, 6, 9, 0), "CLOSED", "Tue 09:00 KST = Mon 20:00 ET"),
    (_kst(2026, 10, 10, 1, 0), "RTH", "Sat 01:00 KST = Fri 12:00 ET RTH"),
    (_kst(2026, 11, 2, 22, 45), "PRE", "DST end: Mon 22:45 KST = Mon 08:45 ET"),
    (_kst(2026, 11, 3, 5, 30), "RTH", "DST end: Tue 05:30 KST = Mon 15:30 ET"),
    (_kst(2026, 11, 26, 23, 0), "CLOSED", "Thanksgiving 2026"),
]


def main() -> int:
    failed = 0
    for ts, expect, label in CASES:
        got = radar.us_session(ts)["key"]
        ok = got == expect
        fire = radar.session_allows_fire_alert(ts)
        if ts == _kst(2026, 10, 5, 22, 44, 59) and fire:
            print(f"FAIL fire lock {label}: fire={fire}")
            failed += 1
        if ts == _kst(2026, 10, 5, 22, 45, 0) and not fire:
            print(f"FAIL fire unlock {label}: fire={fire}")
            failed += 1
        if not ok:
            et = ts.astimezone(ET)
            print(f"FAIL {label}: got {got} expected {expect} (ET {et:%a %H:%M})")
            failed += 1
        else:
            print(f"OK   {label}: {got} fire={fire}")
    # DST open chime must use 09:30 ET, not 22:30 KST
    dst_open = datetime(2026, 11, 2, 22, 30, 1, tzinfo=KST)
    if radar.is_rth_open_chime_window(dst_open):
        print("FAIL DST open chime at 22:30 KST after fall-back")
        failed += 1
    else:
        print("OK   DST 22:30 KST is not the open chime")
    real_open = datetime(2026, 11, 2, 23, 30, 1, tzinfo=KST)  # 09:30 ET after DST
    if not radar.is_rth_open_chime_window(real_open):
        print(f"FAIL real open chime {real_open.astimezone(ET)}")
        failed += 1
    else:
        print("OK   DST 23:30 KST = 09:30 ET open chime")
    print(f"failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

---

## 6. 적용 범위 밖 (이번 P0 에서 손대지 말 것)

- `config.py` 의 ±4% 브래킷 / 100만 원 예산 (P1).
- VWAP / OR15 / ATR 손절 / 스퀴즈 실시간 재채점 (P1).
- `screener/event_driven.py` 점수식 (P2).
- `closed_trades.jsonl` 스키마 (P1, 체결가 수동 입력).
- `.cursor/rules/ldpb-swing.mdc` 와 코드 유니버스 불일치 (거버넌스, P1).
