"""눌림 반등 손절 검증 (v4.5.1): 봇 규칙 그대로(하루 1개, 60일 상승률 1위, 7일 재추천 금지)에 손절 -x% 를 붙이면?

일봉 pickle({심볼: DataFrame(date, open, high, low, close, volume)})을 읽는다. 신호·선별은 screener/pullback.py 와 같다.

손절 방식
  - intraday : 장중 저가가 진입가 × (1 - x%) 이하면 그 가격에 청산 (시가가 이미 아래면 시가 체결).
  - close    : 종가가 진입가 × (1 - x%) 이하면 그날 종가에 청산.
청산(손절 외): 종가 > 5일선 첫날 종가, 아니면 보유 5거래일째 종가. 수수료 왕복 0.2%.

실행: python backtest/backtest_pullback_stop.py EOD.pkl --start 2023-10-01 --split 2025-10-01
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from screener.pullback import rsi2  # noqa: E402

FEE = 0.002
STOPS = [None, 3, 5, 7, 10, 12, 15, 20]


def signals(eod: dict, start: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for sym, b in eod.items():
        if sym in ("SPY", "QQQ") or len(b) < 230:
            continue
        b = b.reset_index(drop=True)
        cs = b["close"].astype(float)
        c, o, h, l = (b[k].to_numpy(float) for k in ("close", "open", "high", "low"))
        r2 = rsi2(cs).to_numpy()
        s5 = cs.rolling(5).mean().to_numpy()
        s50 = cs.rolling(50).mean().to_numpy()
        s200 = cs.rolling(200).mean().to_numpy()
        dv = (cs * b["volume"].astype(float)).rolling(20).mean().to_numpy()
        r60 = cs.pct_change(60).to_numpy()
        dates = b["date"].to_numpy()
        for i in range(200, len(c) - 1):
            if dates[i] < np.datetime64(start):
                continue
            if not (r2[i] < 10 and c[i] >= 10 and dv[i] >= 20e6 and c[i] > s200[i] and s50[i] > s200[i]):
                continue
            rows.append({"date": pd.Timestamp(dates[i]), "sym": sym, "r60": r60[i],
                         "path": (o[i + 1:i + 6], h[i + 1:i + 6], l[i + 1:i + 6], c[i + 1:i + 6], s5[i + 1:i + 6])})
    return pd.DataFrame(rows)


def pick(sig: pd.DataFrame) -> pd.DataFrame:
    """하루 1개, 60일 상승률 1위, 7일(달력일) 안에 추천한 종목은 건너뜀."""
    last: dict = {}
    out = []
    for d, g in sig.sort_values("date").groupby("date", sort=True):
        for _, r in g.sort_values("r60", ascending=False).iterrows():
            prev = last.get(r["sym"])
            if prev is not None and 0 < (d - prev).days < 7:
                continue
            if len(r["path"][3]) < 5:
                break  # 데이터 끝: 아직 끝나지 않은 거래
            last[r["sym"]] = d
            out.append(r)
            break
    return pd.DataFrame(out)


def trade(path, stop, mode):
    o, h, l, c, s5 = path
    px = o[0]
    for j in range(5):
        if stop is not None:
            lvl = px * (1 - stop / 100.0)
            if mode == "intraday" and l[j] <= lvl:
                fill = min(o[j], lvl) if j > 0 else lvl
                return fill / px - 1 - FEE, j + 1, True
            if mode == "close" and c[j] <= lvl:
                return c[j] / px - 1 - FEE, j + 1, True
        if c[j] > s5[j] or j == 4:
            return c[j] / px - 1 - FEE, j + 1, False
    return None


def stats(rets: np.ndarray, stopped: np.ndarray) -> dict:
    w = rets > 0
    return {"n": len(rets), "win%": round(w.mean() * 100, 1), "avg%": round(rets.mean() * 100, 2),
            "med%": round(np.median(rets) * 100, 2), "p5%": round(np.quantile(rets, 0.05) * 100, 2),
            "worst%": round(rets.min() * 100, 2), "sum%": round(rets.sum() * 100, 1),
            "stopped%": round(stopped.mean() * 100, 1),
            "pf": round(rets[w].sum() / max(1e-9, -rets[~w].sum()), 2)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("eod")
    ap.add_argument("--start", default="2023-10-01")
    ap.add_argument("--split", default="2025-10-01")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    eod = pickle.load(open(a.eod, "rb"))
    picks = pick(signals(eod, pd.Timestamp(a.start)))
    split = pd.Timestamp(a.split)
    print(f"거래 {len(picks)}건 · {picks['date'].min().date()} ~ {picks['date'].max().date()}")
    res = []
    for mode in ("intraday", "close"):
        for stop in STOPS:
            if stop is None and mode == "close":
                continue
            t = [trade(p, stop, mode) for p in picks["path"]]
            df = picks[["date", "sym"]].copy()
            df["ret"] = [x[0] for x in t]
            df["stopped"] = [x[2] for x in t]
            for part, sub in (("전체", df), ("학습", df[df["date"] < split]), ("검증", df[df["date"] >= split])):
                st = stats(sub["ret"].to_numpy(), sub["stopped"].to_numpy())
                res.append({"손절": "없음" if stop is None else f"-{stop}%", "방식": mode if stop else "-",
                            "구간": part, **st})
    R = pd.DataFrame(res)
    pd.set_option("display.width", 200)
    print(R.to_string(index=False))
    if a.out:
        R.to_csv(a.out, index=False)


if __name__ == "__main__":
    main()
