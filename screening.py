"""
スクリーニング判定スクリプト
daily_update.py 実行後に続けて実行する。
両戦略の条件を評価し、signals テーブルに watch / buy を書き込む。

実行方法:
    python screening.py
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

DB_PATH     = Path(os.environ.get("SCREENING_DB_PATH", str(Path(__file__).parent / "screening.db")))
CONFIG_PATH = Path(__file__).parent / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── デフォルト閾値 ────────────────────────────────────────────────────────────

DEFAULT_THRESHOLDS: dict = {
    "trend_day0": {
        "body_pct_min": 4.0,
        "body_pct_max": 8.0,
        "upper_shadow_max": 2.0,
        "lower_shadow_min": 0.5,
        "volume_ratio_min": 1.5,
        "volume_ratio_max": 4.0,
        "kairi25_min": 2.0,
        "kairi25_max": 7.0,
        "rsi14_min": 52.0,
        "rsi14_max": 70.0,
    },
    "trend_day1": {
        "volume_ratio_min": 1.2,
        "upper_shadow_max": 3.0,
    },
    "contra_day0": {
        "kairi25_max": -10.0,
        "rsi14_max": 40.0,
        "lower_shadow_min": 1.0,
        "volume_ratio_min": 1.2,
    },
}


def load_thresholds() -> dict:
    """config.json から閾値を読み込み、デフォルト値とマージして返す"""
    result: dict = {}
    for key, defaults in DEFAULT_THRESHOLDS.items():
        result[key] = defaults.copy()

    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for key, vals in cfg.get("thresholds", {}).items():
                if key in result:
                    result[key].update(vals)
    except Exception as e:
        log.warning(f"config.json 読み込み失敗（デフォルト使用）: {e}")

    return result


# ── 条件チェック関数 ──────────────────────────────────────────────────────────

def check_trend_day0(r: dict, r_prev: dict | None, th: dict) -> bool:
    if r_prev is None:
        return False
    if not (r["close"] > r["ma5"] > r["ma25"] > r["ma75"]):
        return False
    if not (th["body_pct_min"] <= r["body_pct"] <= th["body_pct_max"]):
        return False
    if r["upper_shadow"] >= th["upper_shadow_max"]:
        return False
    if r["lower_shadow"] <= th["lower_shadow_min"]:
        return False
    if not (th["volume_ratio_min"] <= r["volume_ratio25"] <= th["volume_ratio_max"]):
        return False
    if not (th["kairi25_min"] <= r["kairi25"] <= th["kairi25_max"]):
        return False
    if not (th["rsi14_min"] <= r["rsi14"] <= th["rsi14_max"]):
        return False
    if r["macd_hist"] <= 0:
        return False
    if r["macd_hist"] <= r_prev["macd_hist"]:
        return False
    return True


def check_trend_day1(r1: dict, r0_low: float, th: dict) -> bool:
    if r1["low"] < r0_low:
        return False
    if r1["volume_ratio25"] <= th["volume_ratio_min"]:
        return False
    if r1["upper_shadow"] >= th["upper_shadow_max"]:
        return False
    return True


def check_contra_day0(r: dict, r_prev: dict | None, th: dict) -> bool:
    if r_prev is None:
        return False
    if r["kairi25"] >= th["kairi25_max"]:
        return False
    if r["rsi14"] >= th["rsi14_max"]:
        return False
    if r["lower_shadow"] <= th["lower_shadow_min"]:
        return False
    if r["volume_ratio25"] <= th["volume_ratio_min"]:
        return False
    if r["macd_hist"] <= r_prev["macd_hist"]:
        return False
    if pd.isna(r["n225_chg"]) or r["n225_chg"] >= 0:
        return False
    return True


def check_contra_day1(r1: dict) -> bool:
    return r1["body_pct"] > 0


# ── メイン処理 ────────────────────────────────────────────────────────────────

def get_latest_dates(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT date FROM indicators ORDER BY date DESC LIMIT 2"
    ).fetchall()
    return [r[0] for r in rows]


def load_indicators_for_date(conn: sqlite3.Connection, date: str) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT i.ticker, i.date,
               i.ma5, i.ma25, i.ma75,
               i.rsi14, i.macd, i.macd_signal, i.macd_hist,
               i.volume_ratio25, i.kairi25,
               i.body_pct, i.upper_shadow, i.lower_shadow,
               i.n225_close, i.n225_chg,
               p.open, p.high, p.low, p.close, p.volume
        FROM indicators i
        JOIN prices_raw p ON p.ticker = i.ticker AND p.date = i.date
        WHERE i.date = ?
        """,
        (date,),
    ).fetchall()

    cols = [
        "ticker","date","ma5","ma25","ma75","rsi14","macd","macd_signal","macd_hist",
        "volume_ratio25","kairi25","body_pct","upper_shadow","lower_shadow",
        "n225_close","n225_chg","open","high","low","close","volume",
    ]
    return {r[0]: dict(zip(cols, r)) for r in rows}


def next_business_day(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def insert_signal(
    conn: sqlite3.Connection,
    ticker: str,
    date: str,
    strategy: str,
    signal_type: str,
    entry_date: str,
    notes: dict,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO signals "
        "(ticker, date, strategy, signal_type, entry_date, notes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ticker, date, strategy, signal_type, entry_date,
         json.dumps(notes, ensure_ascii=False)),
    )


def load_watch_signals(conn: sqlite3.Connection, date: str) -> list[dict]:
    rows = conn.execute(
        "SELECT ticker, strategy, notes FROM signals "
        "WHERE date=? AND signal_type='watch'",
        (date,),
    ).fetchall()
    result = []
    for ticker, strategy, notes_json in rows:
        notes = json.loads(notes_json) if notes_json else {}
        result.append({"ticker": ticker, "strategy": strategy, "notes": notes})
    return result


def run_screening(conn: sqlite3.Connection) -> dict:
    """スクリーニングを実行し、生成件数を返す"""
    thresholds = load_thresholds()
    th_t0 = thresholds["trend_day0"]
    th_t1 = thresholds["trend_day1"]
    th_c0 = thresholds["contra_day0"]

    dates = get_latest_dates(conn)
    if not dates:
        log.error("indicators テーブルにデータがありません。daily_update.py を先に実行してください。")
        return {}

    today = dates[0]
    prev  = dates[1] if len(dates) > 1 else None

    log.info(f"スクリーニング対象日: {today}  前日: {prev}")

    today_data = load_indicators_for_date(conn, today)
    prev_data  = load_indicators_for_date(conn, prev) if prev else {}

    watch_trend = watch_contra = buy_trend = buy_contra = 0

    # Day0 チェック
    for ticker, r in today_data.items():
        r_prev = prev_data.get(ticker)

        if check_trend_day0(r, r_prev, th_t0):
            notes = {
                "body_pct":      round(r["body_pct"], 2),
                "upper_shadow":  round(r["upper_shadow"], 2),
                "lower_shadow":  round(r["lower_shadow"], 2),
                "volume_ratio25":round(r["volume_ratio25"], 2),
                "kairi25":       round(r["kairi25"], 2),
                "rsi14":         round(r["rsi14"], 2),
                "macd_hist":     round(r["macd_hist"], 4),
                "close":         round(r["close"], 0),
            }
            insert_signal(conn, ticker, today, "trend", "watch",
                          next_business_day(today), notes)
            watch_trend += 1

        if check_contra_day0(r, r_prev, th_c0):
            notes = {
                "kairi25":        round(r["kairi25"], 2),
                "rsi14":          round(r["rsi14"], 2),
                "lower_shadow":   round(r["lower_shadow"], 2),
                "volume_ratio25": round(r["volume_ratio25"], 2),
                "macd_hist":      round(r["macd_hist"], 4),
                "n225_chg":       round(r["n225_chg"], 2) if not pd.isna(r["n225_chg"]) else None,
                "close":          round(r["close"], 0),
            }
            insert_signal(conn, ticker, today, "contra", "watch",
                          next_business_day(today), notes)
            watch_contra += 1

    conn.commit()
    log.info(f"Day0 watch 生成: 順張り {watch_trend} / 逆張り {watch_contra}")

    # Day1 チェック
    if prev is None:
        log.info("前日データなし。Day1 チェックをスキップ。")
        return {"watch_trend": watch_trend, "watch_contra": watch_contra,
                "buy_trend": 0, "buy_contra": 0, "date": today}

    prev_watches = load_watch_signals(conn, prev)

    for w in prev_watches:
        ticker   = w["ticker"]
        strategy = w["strategy"]
        r1       = today_data.get(ticker)
        if r1 is None:
            continue

        prev_prices = conn.execute(
            "SELECT low FROM prices_raw WHERE ticker=? AND date=?", (ticker, prev)
        ).fetchone()
        r0_low = prev_prices[0] if prev_prices else None

        promoted = False
        if strategy == "trend" and r0_low is not None:
            promoted = check_trend_day1(r1, r0_low, th_t1)
        elif strategy == "contra":
            promoted = check_contra_day1(r1)

        if promoted:
            entry_date = next_business_day(today)
            notes = {
                "day1_body_pct":      round(r1["body_pct"], 2),
                "day1_upper_shadow":  round(r1["upper_shadow"], 2),
                "day1_volume_ratio25":round(r1["volume_ratio25"], 2),
                "day1_close":         round(r1["close"], 0),
                "day0_notes":         w["notes"],
            }
            insert_signal(conn, ticker, today, strategy, "buy", entry_date, notes)
            if strategy == "trend":
                buy_trend += 1
            else:
                buy_contra += 1

    conn.commit()
    log.info(f"Day1 buy 昇格: 順張り {buy_trend} / 逆張り {buy_contra}")
    log.info(f"本日の buy 推奨合計: {buy_trend + buy_contra} 件")

    return {
        "watch_trend": watch_trend, "watch_contra": watch_contra,
        "buy_trend": buy_trend, "buy_contra": buy_contra, "date": today,
    }


def main() -> None:
    if not DB_PATH.exists():
        log.error("screening.db が見つかりません。先に init_db.py を実行してください。")
        return

    conn = sqlite3.connect(DB_PATH)
    run_screening(conn)
    conn.close()
    log.info("スクリーニング完了")


if __name__ == "__main__":
    main()
