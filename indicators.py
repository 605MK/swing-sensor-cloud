"""
指標計算モジュール
daily_update.py と init_db.py から共通利用
"""

import numpy as np
import pandas as pd


def calc_indicators(prices_df: pd.DataFrame, n225_df: pd.DataFrame | None) -> pd.DataFrame:
    """
    株価DataFrameから各種テクニカル指標を計算して返す。
    prices_df 必須カラム: date, open, high, low, close, volume
    n225_df   必須カラム: date, n225_close, n225_chg  (Noneでも可)
    戻り値: 元カラム + 指標カラム付き DataFrame
    """
    df = prices_df.copy().sort_values("date").reset_index(drop=True)

    # ── Moving Averages ─────────────────────────────────────────────────────
    df["ma5"]  = df["close"].rolling(5).mean()
    df["ma25"] = df["close"].rolling(25).mean()
    df["ma75"] = df["close"].rolling(75).mean()

    # ── RSI14 (Wilder's smoothing = EWM alpha=1/14) ───────────────────────
    delta    = df["close"].diff()
    gain     = delta.where(delta > 0, 0.0)
    loss     = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))

    # ── MACD (12, 26, 9) ─────────────────────────────────────────────────
    ema12           = df["close"].ewm(span=12, adjust=False).mean()
    ema26           = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]        = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"]   = df["macd"] - df["macd_signal"]

    # ── 出来高比 (25日MA比) ───────────────────────────────────────────────
    vol_ma25               = df["volume"].rolling(25).mean()
    df["volume_ratio25"]   = df["volume"] / vol_ma25

    # ── 25MA乖離率 (%) ────────────────────────────────────────────────────
    df["kairi25"] = (df["close"] - df["ma25"]) / df["ma25"] * 100

    # ── ローソク足成分 (%) ────────────────────────────────────────────────
    top    = df[["open", "close"]].max(axis=1)
    bottom = df[["open", "close"]].min(axis=1)
    df["body_pct"]     = (df["close"] - df["open"]) / df["open"] * 100
    df["upper_shadow"]  = (df["high"] - top)    / df["open"] * 100
    df["lower_shadow"]  = (bottom - df["low"])  / df["open"] * 100

    # ── 日経平均 結合 ─────────────────────────────────────────────────────
    if n225_df is not None and not n225_df.empty:
        df = df.merge(n225_df[["date", "n225_close", "n225_chg"]], on="date", how="left")
    else:
        df["n225_close"] = np.nan
        df["n225_chg"]   = np.nan

    return df


INDICATOR_COLS = [
    "ticker", "date",
    "ma5", "ma25", "ma75",
    "rsi14", "macd", "macd_signal", "macd_hist",
    "volume_ratio25", "kairi25",
    "body_pct", "upper_shadow", "lower_shadow",
    "n225_close", "n225_chg",
]
