"""눌림 반등(상승 추세 종목의 단기 과매도 반등) 엔진 백테스트. 하루 1~2개 선별, 1~5일 보유.

저장소 코드는 수정하지 않는다. 일봉 pickle({심볼: DataFrame(date, open, high, low, close, volume)})을 읽는다.

신호(t일 종가 기준, 장 마감 후 계산 → t+1일 매수)
  - 주가 ≥ 10달러, 20일 평균 거래대금 ≥ 2천만 달러
  - 추세: 종가 > 200일선 (옵션: 50일선 > 200일선)
  - 눌림: RSI(2) < rsi_max
  - 시장: SPY 종가 > SPY 200일선 (옵션)
진입
  - 'open'  : t+1 시가
  - 'limit' : t+1 에 t 종가 × (1 - limit_pct%) 지정가. 시가가 더 낮으면 시가 체결, 저가가 못 닿으면 미체결.
청산 (종가 기준)
  - 종가 > 5일선 이 되는 첫날, 또는 최대 보유일 경과 시 그날 종가. 손절 옵션: 종가 기준 -stop%.
선별: 같은 날 신호 중 rank 기준 상위 top_n 개.
수수료: 왕복 0.2% 차감 (봇 수호 데스크와 같은 기준).

실행: python backtest_pullback.py EOD.pkl --split 2025-10-01 --out DIR
"""
from __future__ import annotations

import argparse
import itertools
import os
import pickle

import numpy as np
import pandas as pd

FEE = 0.002


def rsi2(close: pd.Series) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def build_signals(eod: dict, rsi_cap: float = 15.0) -> pd.DataFrame:
    """느슨한 상한(rsi_cap)으로 모든 후보 신호와 특징값, 진입·청산 경로를 미리 만든다."""
    spy = eod["SPY"].set_index("date")["close"]
    spy_ok = (spy > spy.rolling(200).mean()).rename("spy_ok")
    rows = []
    for sym, b in eod.items():
        if sym in ("SPY", "QQQ") or len(b) < 230:
            continue
        b = b.reset_index(drop=True)
        c, o, h, l, v = (b[k].to_numpy(float) for k in ("close", "open", "high", "low", "volume"))
        cs = b["close"]
        r2 = rsi2(cs).to_numpy()
        s5 = cs.rolling(5).mean().to_numpy()
        s50 = cs.rolling(50).mean().to_numpy()
        s200 = cs.rolling(200).mean().to_numpy()
        dv = (cs * b["volume"]).rolling(20).mean().to_numpy()
        atr = (pd.concat([b["high"] - b["low"], (b["high"] - cs.shift()).abs(), (b["low"] - cs.shift()).abs()], axis=1)
               .max(axis=1).rolling(14).mean().to_numpy())
        r60 = cs.pct_change(60).to_numpy()
        hi20 = b["high"].rolling(20).max().to_numpy()
        n = len(c)
        for i in range(200, n - 1):
            if not (r2[i] < rsi_cap and c[i] >= 10 and dv[i] >= 20e6 and c[i] > s200[i]):
                continue
            rows.append({
                "date": b["date"].iloc[i], "sym": sym, "i": i, "rsi": r2[i], "close": c[i],
                "trend50": s50[i] > s200[i], "above50": c[i] > s50[i],
                "dist200": c[i] / s200[i] - 1, "r60": r60[i], "atr_pct": atr[i] / c[i],
                "dd20": c[i] / hi20[i] - 1, "dv": dv[i],
                "down3": c[i] < c[i - 1] < c[i - 2] < c[i - 3],
                "path": (o[i + 1:i + 7], h[i + 1:i + 7], l[i + 1:i + 7], c[i + 1:i + 7], s5[i + 1:i + 7]),
            })
    df = pd.DataFrame(rows)
    df = df.join(spy_ok, on="date")
    return df


def trade(row, entry: str, limit_pct: float, max_hold: int, stop: float | None):
    """(체결 여부, 순수익률, 보유일, 진입 당일 종가 > 진입가)."""
    o, h, l, c, s5 = row["path"]
    if len(c) < 1:
        return False, None, None, None
    if entry == "open":
        px = o[0]
    else:
        lim = row["close"] * (1 - limit_pct / 100.0)
        if l[0] > lim:
            return False, None, None, None
        px = min(o[0], lim)
    last = min(max_hold, len(c)) - 1
    for j in range(last + 1):
        if stop is not None and c[j] <= px * (1 - stop / 100.0):
            return True, c[j] / px - 1 - FEE, j + 1, c[0] > px
        if c[j] > s5[j] or j == last:
            return True, c[j] / px - 1 - FEE, j + 1, c[0] > px
    return True, None, None, None


def evaluate(sig: pd.DataFrame, p: dict) -> pd.DataFrame:
    s = sig[sig["rsi"] < p["rsi_max"]]
    if p["trend50"]:
        s = s[s["trend50"]]
    if p["spy"]:
        s = s[s["spy_ok"] == True]  # noqa: E712
    if p.get("down3"):
        s = s[s["down3"]]
    asc = p["rank"] in ("rsi", "atr_pct", "dd20")
    s = s.sort_values(p["rank"], ascending=asc)
    out = []
    for d, g in s.groupby("date", sort=True):
        k = 0
        for _, r in g.iterrows():
            if k >= p["top_n"]:
                break
            ok, ret, hold, d1 = trade(r, p["entry"], p["limit_pct"], p["max_hold"], p["stop"])
            if not ok or ret is None:
                continue  # 지정가 미체결이면 다음 순위로 넘어가지 않는다 (실전과 같게: 그날 그 종목만 못 산 것)
            out.append({"date": d, "sym": r["sym"], "ret": ret, "hold": hold, "day1": d1, "rsi": r["rsi"]})
            k += 1
    return pd.DataFrame(out)


def stats(t: pd.DataFrame, ndays: int) -> dict:
    if t.empty:
        return {"n": 0}
    w = t["ret"] > 0
    streak = mx = 0
    for x in w:
        streak = 0 if x else streak + 1
        mx = max(mx, streak)
    return {"n": len(t), "days": t["date"].nunique(), "day_cov": t["date"].nunique() / ndays,
            "win": w.mean(), "avg": t["ret"].mean(), "med": t["ret"].median(), "p5": t["ret"].quantile(0.05),
            "hold": t["hold"].mean(), "max_loss_streak": mx,
            "pf": t.loc[w, "ret"].sum() / max(1e-9, -t.loc[~w, "ret"].sum())}


GRID = {
    "rsi_max": [2, 5, 10],
    "trend50": [False, True],
    "spy": [False, True],
    "entry": ["open", "limit"],
    "limit_pct": [1.0, 2.0],
    "max_hold": [3, 5],
    "stop": [None, 5.0],
    "rank": ["rsi", "r60", "atr_pct", "dv"],
    "top_n": [2],
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("eod")
    ap.add_argument("--split", default="2025-10-01")
    ap.add_argument("--out", default="pb_out")
    ap.add_argument("--min-trades", type=int, default=150)
    a = ap.parse_args(argv)
    eod = pickle.load(open(a.eod, "rb"))
    sig = build_signals(eod)
    split = pd.Timestamp(a.split)
    all_days = sorted({d for b in [eod["SPY"]] for d in b["date"] if d >= sig["date"].min()})
    tr_days = sum(1 for d in all_days if d < split)
    te_days = sum(1 for d in all_days if d >= split)
    print(f"신호 {len(sig)}건, {sig['date'].min().date()} ~ {sig['date'].max().date()}, 학습 {tr_days}일 / 검증 {te_days}일", flush=True)
    train, test = sig[sig["date"] < split], sig[sig["date"] >= split]

    keys = list(GRID)
    res = []
    for vals in itertools.product(*GRID.values()):
        p = dict(zip(keys, vals))
        if p["entry"] == "open" and p["limit_pct"] != GRID["limit_pct"][0]:
            continue
        st = stats(evaluate(train, p), tr_days)
        if st["n"] < a.min_trades:
            continue
        res.append({**p, **{f"tr_{k}": v for k, v in st.items()}})
    R = pd.DataFrame(res)
    # 학습 구간 선정 기준: 수수료 뺀 평균 수익 > 0 인 것 중 적중률 최고. 동률이면 평균 수익.
    R = R[R["tr_avg"] > 0].sort_values(["tr_win", "tr_avg"], ascending=False)
    os.makedirs(a.out, exist_ok=True)
    top = R.head(10).copy()
    for idx, row in top.iterrows():
        p = {k: row[k] for k in keys}
        p["stop"] = None if pd.isna(p["stop"]) else p["stop"]
        t = evaluate(test, p)
        st = stats(t, te_days)
        for k, v in st.items():
            top.loc[idx, f"te_{k}"] = v
    R.to_csv(os.path.join(a.out, "grid_train.csv"), index=False)
    top.to_csv(os.path.join(a.out, "top10_train_test.csv"), index=False)
    best = {k: top.iloc[0][k] for k in keys}
    best["stop"] = None if pd.isna(best["stop"]) else best["stop"]
    for name, part in (("train", train), ("test", test)):
        t = evaluate(part, best)
        t.to_csv(os.path.join(a.out, f"trades_best_{name}.csv"), index=False)
    # 기준선: 아무 필터 없이 RSI2<5, 시가 진입, 하루 2개(RSI 최저)
    base = {"rsi_max": 5, "trend50": False, "spy": False, "entry": "open", "limit_pct": 1.0, "max_hold": 5,
            "stop": None, "rank": "rsi", "top_n": 2}
    for name, part, nd in (("train", train, tr_days), ("test", test, te_days)):
        print(f"기준선 {name}:", {k: round(v, 4) if isinstance(v, float) else v for k, v in stats(evaluate(part, base), nd).items()})
    pd.set_option("display.width", 250)
    cols = keys + [c for c in top.columns if c.startswith(("tr_", "te_")) and c.split("_", 1)[1] in ("n", "win", "avg", "day_cov", "p5", "max_loss_streak")]
    print(top[cols].round(4).to_string())
    print("최종 선택:", best)


if __name__ == "__main__":
    main()
