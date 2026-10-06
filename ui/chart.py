"""Plotly candlestick from SharedState OHLCV. SMA20/200, pivot=BaseHigh*1.002, stop."""
from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import config
from data.yf_download import download_intraday
from state import SHARED


def bars(ticker: str, timeframe: str) -> pd.DataFrame:
    t = ticker.upper().strip()
    daily = SHARED.get_ohlcv(t)
    if timeframe == "1d":
        return daily
    if timeframe in {"5m", "60m"}:
        interval = "5m" if timeframe == "5m" else "60m"
        intra = download_intraday(t, interval)
        if intra is not None and not intra.empty:
            return intra
        return daily
    return daily


def figure(df: pd.DataFrame, row: dict[str, Any], height: int = 640) -> go.Figure:
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.78, 0.22],
        vertical_spacing=0.03,
    )
    layout = dict(
        height=height,
        paper_bgcolor="#0B0F14",
        plot_bgcolor="#0B0F14",
        font=dict(color="#E8EEF4", size=11),
        margin=dict(l=8, r=8, t=28, b=8),
        legend=dict(orientation="h", y=1.08, x=0, bgcolor="rgba(0,0,0,0)"),
        xaxis_rangeslider_visible=False,
        uirevision=str(row.get("ticker") or "chart"),
        hovermode="x unified",
    )
    if df is None or df.empty or not {"Open", "High", "Low", "Close"}.issubset(df.columns):
        fig.update_layout(**layout)
        fig.add_annotation(
            text="OHLCV 없음 — 스캔 후 종목을 클릭하십시오.",
            xref="paper",
            yref="paper",
            x=0.5,
            y=0.5,
            showarrow=False,
            font=dict(color="#8A97A8", size=14),
        )
        return fig

    idx = df.index
    fig.add_trace(
        go.Candlestick(
            x=idx,
            open=df["Open"],
            high=df["High"],
            low=df["Low"],
            close=df["Close"],
            name=str(row.get("ticker") or ""),
            increasing_line_color="#3DDC97",
            decreasing_line_color="#FF5C7A",
            showlegend=False,
        ),
        row=1,
        col=1,
    )
    close = df["Close"]
    if len(close) >= config.SMA_FAST:
        fig.add_trace(
            go.Scatter(x=idx, y=close.rolling(config.SMA_FAST).mean(), name="SMA20", line=dict(color="#4DA3FF", width=1.5)),
            row=1,
            col=1,
        )
    if len(close) >= config.SMA_SLOW:
        fig.add_trace(
            go.Scatter(x=idx, y=close.rolling(config.SMA_SLOW).mean(), name="SMA200", line=dict(color="#C084FC", width=1.7)),
            row=1,
            col=1,
        )

    base_high = float(row.get("base_high") or 0)
    pivot = float(row.get("pivot") or (base_high * 1.002 if base_high else 0))
    stop = float(row.get("stop") or 0)
    if pivot > 0:
        fig.add_hline(
            y=pivot,
            line_dash="dot",
            line_color="#F5A623",
            line_width=2,
            annotation_text="PIVOT",
            annotation_font_color="#F5A623",
            row=1,
            col=1,
        )
    if stop > 0:
        fig.add_hline(
            y=stop,
            line_dash="solid",
            line_color="#FF3B5C",
            line_width=1.8,
            annotation_text="STOP",
            annotation_font_color="#FF3B5C",
            row=1,
            col=1,
        )

    base_bars = int(row.get("base_bars") or config.BASE_BARS)
    if len(df) >= 2 and pivot > 0:
        x0 = idx[max(0, len(idx) - base_bars)]
        fig.add_shape(
            type="rect",
            x0=x0,
            x1=idx[-1],
            y0=float(row.get("base_low") or stop),
            y1=float(base_high or pivot),
            fillcolor="rgba(245,166,35,0.12)",
            line=dict(color="rgba(245,166,35,0.35)", width=1),
            row=1,
            col=1,
        )

    if "Volume" in df.columns:
        colors = ["#3DDC97" if c >= o else "#FF5C7A" for o, c in zip(df["Open"], df["Close"])]
        fig.add_trace(
            go.Bar(x=idx, y=df["Volume"], marker_color=colors, name="Vol", showlegend=False),
            row=2,
            col=1,
        )
    fig.update_layout(**layout)
    fig.update_xaxes(gridcolor="#1E2733", showgrid=True)
    fig.update_yaxes(gridcolor="#1E2733", showgrid=True, side="right")
    return fig
