"""
スクリーニングDB 初期化スクリプト（初回のみ実行）
東証プライム全銘柄の約2年分データを取得し、screening.db を構築する。

実行方法:
    python init_db.py

銘柄リスト (tickers.csv) がなければ JPX サイトから自動取得を試みる。
手動で用意する場合は tickers.csv を以下の形式で配置:
    ticker,name
    1301,極洋
    1305,iFreeETF TOPIX
    ...
（.T サフィックスは不要。スクリプトが自動付与する）
"""

import io
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from indicators import INDICATOR_COLS, calc_indicators

DB_PATH      = Path(__file__).parent / "screening.db"
TICKERS_CSV  = Path(__file__).parent / "tickers.csv"
BATCH_SIZE   = 100
SLEEP_SEC    = 1.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── DB スキーマ ─────────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS watchlist (
    ticker       TEXT PRIMARY KEY,
    name         TEXT,
    market_code  TEXT
);

CREATE TABLE IF NOT EXISTS prices_raw (
    ticker  TEXT NOT NULL,
    date    TEXT NOT NULL,
    open    REAL,
    high    REAL,
    low     REAL,
    close   REAL,
    volume  INTEGER,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS indicators (
    ticker         TEXT NOT NULL,
    date           TEXT NOT NULL,
    ma5            REAL,
    ma25           REAL,
    ma75           REAL,
    rsi14          REAL,
    macd           REAL,
    macd_signal    REAL,
    macd_hist      REAL,
    volume_ratio25 REAL,
    kairi25        REAL,
    body_pct       REAL,
    upper_shadow   REAL,
    lower_shadow   REAL,
    n225_close     REAL,
    n225_chg       REAL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT    NOT NULL,
    date         TEXT    NOT NULL,
    strategy     TEXT    NOT NULL,
    signal_type  TEXT    NOT NULL,
    entry_date   TEXT,
    entry_price  REAL,
    notes        TEXT,
    created_at   TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_prices_ticker_date
    ON prices_raw(ticker, date DESC);
CREATE INDEX IF NOT EXISTS idx_indicators_ticker_date
    ON indicators(ticker, date DESC);
CREATE INDEX IF NOT EXISTS idx_signals_date
    ON signals(date DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_unique
    ON signals(ticker, date, strategy, signal_type);
"""


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.commit()
    log.info("DB スキーマ作成完了")


# ── 銘柄リスト取得 ──────────────────────────────────────────────────────────

def load_tickers() -> pd.DataFrame:
    if TICKERS_CSV.exists():
        df = pd.read_csv(TICKERS_CSV, dtype=str)
        log.info(f"tickers.csv から {len(df)} 銘柄を読み込み")
    else:
        log.info("tickers.csv が見つかりません。JPX から取得を試みます...")
        df = _fetch_from_jpx()

    if "ticker" not in df.columns:
        raise ValueError("tickers.csv には 'ticker' 列が必要です")

    # .T サフィックス付与（なければ）
    df["ticker"] = (
        df["ticker"].astype(str).str.strip()
        .str.replace(r"\.T$", "", regex=True)
        .str.zfill(4) + ".T"
    )
    if "name" not in df.columns:
        df["name"] = ""
    if "market_code" not in df.columns:
        df["market_code"] = "prime"

    return df[["ticker", "name", "market_code"]].drop_duplicates("ticker")


def _fetch_from_jpx() -> pd.DataFrame:
    import requests

    url = (
        "https://www.jpx.co.jp/markets/statistics-equities/misc/"
        "tvdivq0000001vg2-att/data_j.xls"
    )
    headers = {"User-Agent": "Mozilla/5.0 (compatible; screening-bot/1.0)"}

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        df = pd.read_excel(io.BytesIO(resp.content), dtype=str)
        df.columns = df.columns.str.strip()

        # 市場区分列を探す
        market_col = next(
            (c for c in df.columns if "市場" in c or "market" in c.lower()), None
        )
        if market_col:
            df = df[df[market_col].str.contains("プライム", na=False)]

        # コード列・銘柄名列を探す
        code_col = next(
            (c for c in df.columns if "コード" in c or "code" in c.lower()), None
        )
        name_col = next(
            (c for c in df.columns if "銘柄名" in c or "会社名" in c or "name" in c.lower()), None
        )

        if not code_col:
            raise ValueError("コード列が見つかりません。tickers.csv を手動で用意してください。")

        result = df[[code_col]].copy()
        result.columns = ["ticker"]
        if name_col:
            result["name"] = df[name_col]
        result = result.dropna(subset=["ticker"])

        # キャッシュ保存
        result.to_csv(TICKERS_CSV, index=False, encoding="utf-8-sig")
        log.info(f"JPX から {len(result)} 銘柄取得 → tickers.csv に保存")
        return result

    except Exception as e:
        raise RuntimeError(
            f"JPX からの取得に失敗しました: {e}\n"
            "tickers.csv を手動で作成してください:\n"
            "  ticker,name\n"
            "  1301,極洋\n"
            "  7203,トヨタ自動車\n"
            "  ..."
        ) from e


# ── 日経平均取得 ─────────────────────────────────────────────────────────────

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


# ── バッチダウンロード ───────────────────────────────────────────────────────

def _download_batch(batch: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """複数銘柄を一括ダウンロードし {ticker: DataFrame} で返す"""
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
        # 銘柄が1件のとき MultiIndex にならない
        if len(batch) == 1:
            df = raw.copy().dropna(how="all")
            if not df.empty:
                df.columns = [c.lower().replace(" ", "_") for c in df.columns]
                results[batch[0]] = df

    return results


# ── メイン処理 ───────────────────────────────────────────────────────────────

def fetch_and_store(
    conn: sqlite3.Connection,
    tickers_df: pd.DataFrame,
    start: str,
    end: str,
    n225_df: pd.DataFrame,
) -> None:
    ticker_list = tickers_df["ticker"].tolist()
    name_map    = dict(zip(tickers_df["ticker"], tickers_df["name"]))
    total       = len(ticker_list)
    ok_count    = 0

    for batch_start in range(0, total, BATCH_SIZE):
        batch = ticker_list[batch_start : batch_start + BATCH_SIZE]
        bn    = batch_start // BATCH_SIZE + 1
        bn_total = (total - 1) // BATCH_SIZE + 1
        log.info(f"バッチ {bn}/{bn_total} ({len(batch)} 銘柄) 取得中...")

        try:
            batch_data = _download_batch(batch, start, end)
        except Exception as e:
            log.warning(f"バッチ {bn} ダウンロード失敗: {e}")
            time.sleep(SLEEP_SEC)
            continue

        for ticker, raw_df in batch_data.items():
            try:
                raw_df = raw_df.reset_index()
                # Date 列の正規化
                date_col = next(c for c in raw_df.columns if "date" in c.lower())
                raw_df["date"]   = pd.to_datetime(raw_df[date_col]).dt.strftime("%Y-%m-%d")
                raw_df["ticker"] = ticker

                for col in ["open", "high", "low", "close", "volume"]:
                    if col not in raw_df.columns:
                        raw_df[col] = np.nan
                raw_df["close"]  = pd.to_numeric(raw_df["close"],  errors="coerce")
                raw_df["volume"] = pd.to_numeric(raw_df["volume"], errors="coerce")

                raw_df = raw_df.dropna(subset=["close"])
                if raw_df.empty:
                    continue

                # prices_raw へ保存
                conn.executemany(
                    "INSERT OR REPLACE INTO prices_raw "
                    "(ticker, date, open, high, low, close, volume) "
                    "VALUES (:ticker,:date,:open,:high,:low,:close,:volume)",
                    raw_df[["ticker","date","open","high","low","close","volume"]].to_dict("records"),
                )

                # 指標計算
                ind_df = calc_indicators(raw_df[["date","open","high","low","close","volume"]], n225_df)
                ind_df["ticker"] = ticker
                ind_df = ind_df.dropna(subset=["ma75"])  # MA75 が計算できる行のみ

                if ind_df.empty:
                    continue

                conn.executemany(
                    "INSERT OR REPLACE INTO indicators "
                    "(ticker,date,ma5,ma25,ma75,rsi14,macd,macd_signal,macd_hist,"
                    " volume_ratio25,kairi25,body_pct,upper_shadow,lower_shadow,n225_close,n225_chg) "
                    "VALUES (:ticker,:date,:ma5,:ma25,:ma75,:rsi14,:macd,:macd_signal,:macd_hist,"
                    "        :volume_ratio25,:kairi25,:body_pct,:upper_shadow,:lower_shadow,:n225_close,:n225_chg)",
                    ind_df[INDICATOR_COLS].to_dict("records"),
                )

                ok_count += 1

            except Exception as e:
                log.debug(f"{ticker} 処理失敗: {e}")

        conn.commit()
        time.sleep(SLEEP_SEC)

    log.info(f"完了: {ok_count} 銘柄のデータを保存")


def main() -> None:
    log.info("=" * 50)
    log.info("スクリーニングDB 初期化開始")
    log.info("=" * 50)

    tickers_df = load_tickers()
    log.info(f"対象銘柄数: {len(tickers_df)}")

    conn = sqlite3.connect(DB_PATH)
    create_schema(conn)

    # watchlist 登録
    conn.executemany(
        "INSERT OR REPLACE INTO watchlist (ticker, name, market_code) VALUES (:ticker,:name,:market_code)",
        tickers_df.to_dict("records"),
    )
    conn.commit()
    log.info(f"watchlist: {len(tickers_df)} 件登録")

    end   = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
    log.info(f"取得期間: {start} 〜 {end}")

    log.info("日経平均データ取得中...")
    n225_df = fetch_n225(start, end)
    log.info(f"日経平均: {len(n225_df)} 件取得")

    log.info("株価・指標データ取得開始 (数十分かかります)...")
    fetch_and_store(conn, tickers_df, start, end, n225_df)

    conn.close()
    log.info("=" * 50)
    log.info("初期化完了")
    log.info("=" * 50)


if __name__ == "__main__":
    main()
