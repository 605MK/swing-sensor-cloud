"""
毎日の株価更新 + 指標計算スクリプト
東証引け後（15:30以降）に実行。直近5営業日分のデータを差分取得してDBを更新する。

実行方法:
    python daily_update.py
"""

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from indicators import INDICATOR_COLS, calc_indicators

DB_PATH    = Path(__file__).parent / "screening.db"
BATCH_SIZE = 100
SLEEP_SEC  = 1.5
LOOKBACK_DAYS = 110  # 指標計算用にDBから取得する過去日数（MA75確保のため）

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def get_tickers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT ticker FROM watchlist ORDER BY ticker").fetchall()
    return [r[0] for r in rows]


def fetch_n225(start: str, end: str) -> pd.DataFrame:
    try:
        raw = yf.download("^N225", start=start, end=end, progress=False, auto_adjust=False)
        if raw.empty:
            return pd.DataFrame()
        raw = raw.reset_index()
        raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
        raw["date"]       = pd.to_datetime(raw["Date"]).dt.strftime("%Y-%m-%d")
        raw["n225_close"] = raw["Close"].astype(float)
        raw["n225_chg"]   = raw["n225_close"].pct_change() * 100
        return raw[["date", "n225_close", "n225_chg"]].dropna()
    except Exception as e:
        log.warning(f"日経平均取得失敗: {e}")
        return pd.DataFrame()


def _download_batch(batch: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    if not batch:
        return {}
    raw = yf.download(
        batch, start=start, end=end,
        progress=False, auto_adjust=False,
        group_by="ticker",
    )
    if raw.empty:
        return {}

    results: dict[str, pd.DataFrame] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        available = raw.columns.get_level_values(0).unique().tolist()
        for ticker in batch:
            if ticker not in available:
                continue
            df = raw[ticker].copy().dropna(how="all")
            if df.empty:
                continue
            df.columns = [c.lower().replace(" ", "_") for c in df.columns]
            results[ticker] = df
    else:
        if len(batch) == 1:
            df = raw.copy().dropna(how="all")
            if not df.empty:
                df.columns = [c.lower().replace(" ", "_") for c in df.columns]
                results[batch[0]] = df
    return results


def load_prices_from_db(conn: sqlite3.Connection, ticker: str, days: int) -> pd.DataFrame:
    """DBから直近 days 日分の価格を取得"""
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume "
        "FROM prices_raw WHERE ticker=? ORDER BY date DESC LIMIT ?",
        (ticker, days),
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["date","open","high","low","close","volume"])
    return df.sort_values("date").reset_index(drop=True)


def process_ticker(
    conn: sqlite3.Connection,
    ticker: str,
    new_df: pd.DataFrame,
    n225_df: pd.DataFrame,
) -> int:
    """
    1銘柄分の新規データをDBへ保存し、新しい日付の指標を計算・保存する。
    戻り値: 追加した新規日数
    """
    # 新規価格の整形
    new_df = new_df.reset_index()
    date_col = next(c for c in new_df.columns if "date" in c.lower())
    new_df["date"]   = pd.to_datetime(new_df[date_col]).dt.strftime("%Y-%m-%d")
    new_df["ticker"] = ticker

    for col in ["open", "high", "low", "close", "volume"]:
        if col not in new_df.columns:
            new_df[col] = np.nan
    new_df["close"]  = pd.to_numeric(new_df["close"],  errors="coerce")
    new_df["volume"] = pd.to_numeric(new_df["volume"], errors="coerce")
    new_df = new_df.dropna(subset=["close"])
    if new_df.empty:
        return 0

    # 既存 DB の最新日を確認
    latest_row = conn.execute(
        "SELECT MAX(date) FROM prices_raw WHERE ticker=?", (ticker,)
    ).fetchone()
    latest_in_db = latest_row[0] if latest_row and latest_row[0] else "1900-01-01"

    # 新規行のみフィルタ
    truly_new = new_df[new_df["date"] > latest_in_db]
    if truly_new.empty:
        return 0

    # prices_raw 保存
    conn.executemany(
        "INSERT OR REPLACE INTO prices_raw "
        "(ticker, date, open, high, low, close, volume) "
        "VALUES (:ticker,:date,:open,:high,:low,:close,:volume)",
        truly_new[["ticker","date","open","high","low","close","volume"]].to_dict("records"),
    )

    # 指標計算: DB + 新規を結合して安定した指標を得る
    hist_df = load_prices_from_db(conn, ticker, LOOKBACK_DAYS)
    # 新規データを上書きマージ
    combined = pd.concat([hist_df, truly_new[["date","open","high","low","close","volume"]]])
    combined = combined.drop_duplicates("date").sort_values("date").reset_index(drop=True)

    ind_df = calc_indicators(combined, n225_df)
    ind_df["ticker"] = ticker
    ind_df = ind_df.dropna(subset=["ma75"])

    # 新規日付の指標のみ保存
    new_dates = set(truly_new["date"].tolist())
    ind_df = ind_df[ind_df["date"].isin(new_dates)]
    if ind_df.empty:
        return 0

    conn.executemany(
        "INSERT OR REPLACE INTO indicators "
        "(ticker,date,ma5,ma25,ma75,rsi14,macd,macd_signal,macd_hist,"
        " volume_ratio25,kairi25,body_pct,upper_shadow,lower_shadow,n225_close,n225_chg) "
        "VALUES (:ticker,:date,:ma5,:ma25,:ma75,:rsi14,:macd,:macd_signal,:macd_hist,"
        "        :volume_ratio25,:kairi25,:body_pct,:upper_shadow,:lower_shadow,:n225_close,:n225_chg)",
        ind_df[INDICATOR_COLS].to_dict("records"),
    )
    return len(truly_new)


PRUNE_KEEP_DAYS = 120  # prices_raw / indicators の保持日数（約4ヶ月）


def prune_old_data(conn: sqlite3.Connection) -> None:
    """prices_raw と indicators の古いデータを削除して DB サイズを安定させる。
    signals テーブルは削除しない（履歴として全期間保持）。"""
    cutoff = (datetime.now() - timedelta(days=PRUNE_KEEP_DAYS)).strftime("%Y-%m-%d")
    conn.execute("DELETE FROM prices_raw WHERE date < ?", (cutoff,))
    conn.execute("DELETE FROM indicators WHERE date < ?", (cutoff,))
    conn.commit()
    conn.execute("VACUUM")  # ファイルサイズを実際に縮小
    log.info(f"古いデータを削除しました（{cutoff} 以前の prices_raw / indicators）")


def main() -> None:
    if not DB_PATH.exists():
        log.error("screening.db が見つかりません。先に init_db.py を実行してください。")
        return

    conn = sqlite3.connect(DB_PATH)
    tickers = get_tickers(conn)
    log.info(f"対象銘柄数: {len(tickers)}")

    # 直近5営業日 + バッファ
    end   = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    log.info(f"取得期間: {start} 〜 {end}")

    log.info("日経平均データ取得中...")
    n225_df = fetch_n225(start, end)

    total_new = 0
    total_ok  = 0

    for batch_start in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[batch_start : batch_start + BATCH_SIZE]
        bn    = batch_start // BATCH_SIZE + 1
        bn_total = (len(tickers) - 1) // BATCH_SIZE + 1
        log.info(f"バッチ {bn}/{bn_total} ({len(batch)} 銘柄)...")

        try:
            batch_data = _download_batch(batch, start, end)
        except Exception as e:
            log.warning(f"バッチ {bn} 失敗: {e}")
            time.sleep(SLEEP_SEC)
            continue

        for ticker, raw_df in batch_data.items():
            try:
                added = process_ticker(conn, ticker, raw_df, n225_df)
                if added > 0:
                    total_new += added
                    total_ok  += 1
            except Exception as e:
                log.debug(f"{ticker} 処理失敗: {e}")

        conn.commit()
        time.sleep(SLEEP_SEC)

    log.info(f"更新完了: {total_ok} 銘柄 / {total_new} 件の新規データを追加")

    # 古いデータを削除して DB サイズを安定化
    log.info("古いデータをプルーニング中...")
    prune_old_data(conn)

    conn.close()


if __name__ == "__main__":
    main()
