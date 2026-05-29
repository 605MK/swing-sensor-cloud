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

def _resolve_db_path() -> Path:
    base = Path(__file__).parent
    if str(base).startswith("/mount/src"):
        return Path("/tmp/screening.db")
    default = base / "screening.db"
    try:
        test = default.parent / ".wtest"
        test.touch()
        test.unlink()
        return default
    except OSError:
        return Path("/tmp/screening.db")


DB_PATH     = Path(os.environ["SCREENING_DB_PATH"]) if os.environ.get("SCREENING_DB_PATH") else _resolve_db_path()
CONFIG_PATH = Path(__file__).parent / "config.json"

class _JSTFormatter(logging.Formatter):
    _JST = __import__("datetime").timezone(__import__("datetime").timedelta(hours=9))
    def formatTime(self, record, datefmt=None):
        from datetime import datetime
        dt = datetime.fromtimestamp(record.created, tz=self._JST)
        return dt.strftime(datefmt or "%H:%M:%S")

_handler = logging.StreamHandler()
_handler.setFormatter(_JSTFormatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
logging.basicConfig(handlers=[_handler], level=logging.INFO)
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

def ensure_screened_dates_table(conn: sqlite3.Connection) -> None:
    """screened_dates テーブルを用意し、既存DBを一度だけマイグレーションする。

    旧仕様では「signals に行がない日」を未処理と判定していたが、シグナル0件の
    日は signals に行が残らないため、毎回最古の0件日から再スクリーニングされて
    いた。スクリーニング済み日を独立に記録することでこれを解消する。"""
    conn.execute("CREATE TABLE IF NOT EXISTS screened_dates (date TEXT PRIMARY KEY)")
    # 空＝初回マイグレーション。既存シグナルの最終日までは処理済みとみなす
    # （バックフィルは常に最古ギャップ→最新を連続処理するため、この仮定は成立する）
    already = conn.execute("SELECT COUNT(*) FROM screened_dates").fetchone()[0]
    if already == 0:
        max_sig = conn.execute("SELECT MAX(date) FROM signals").fetchone()[0]
        if max_sig:
            conn.execute(
                "INSERT OR IGNORE INTO screened_dates (date) "
                "SELECT DISTINCT date FROM indicators WHERE date <= ?",
                (max_sig,),
            )
            conn.commit()


def get_dates_to_screen(conn: sqlite3.Connection) -> tuple[list[str], str | None]:
    """未スクリーニング日を含む処理対象日付リストを返す。
    indicators に存在するが screened_dates にない日があれば、その最古日から
    最新 indicators 日まで全て処理（INSERT OR IGNORE で重複は安全に無視）。"""
    # indicators にあるが screened_dates にない最古の日付を探す
    first_gap_row = conn.execute("""
        SELECT MIN(i.date) FROM
        (SELECT DISTINCT date FROM indicators) i
        LEFT JOIN screened_dates s ON s.date = i.date
        WHERE s.date IS NULL
    """).fetchone()
    first_gap = first_gap_row[0] if first_gap_row and first_gap_row[0] else None

    if not first_gap:
        return [], conn.execute("SELECT MAX(date) FROM screened_dates").fetchone()[0]

    # first_gap より前の最新スクリーニング済み日（Day1 コンテキスト用）
    last_before = conn.execute(
        "SELECT MAX(date) FROM screened_dates WHERE date < ?", (first_gap,)
    ).fetchone()[0]

    # first_gap 以降の全 indicators 日（処理済みも含む: Day1 再チェックのため）
    rows = conn.execute(
        "SELECT DISTINCT date FROM indicators WHERE date >= ? ORDER BY date ASC",
        (first_gap,),
    ).fetchall()
    return [r[0] for r in rows], last_before


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
    """未スクリーニング日を全て処理し、生成件数を返す（バックフィル対応）"""
    thresholds = load_thresholds()
    th_t0 = thresholds["trend_day0"]
    th_t1 = thresholds["trend_day1"]
    th_c0 = thresholds["contra_day0"]

    ensure_screened_dates_table(conn)
    dates_to_process, last_screened = get_dates_to_screen(conn)

    if not dates_to_process:
        log.info("新しいスクリーニング対象日はありません。")
        return {"watch_trend": 0, "watch_contra": 0,
                "buy_trend": 0, "buy_contra": 0, "date": last_screened or ""}

    total_watch_trend = total_watch_contra = total_buy_trend = total_buy_contra = 0

    for i, today in enumerate(dates_to_process):
        # prevは同ループの前日 or 前回実行の最終日
        prev = dates_to_process[i - 1] if i > 0 else last_screened

        log.info(f"スクリーニング対象日: {today}  前日: {prev or 'なし'}")

        today_data = load_indicators_for_date(conn, today)
        prev_data  = load_indicators_for_date(conn, prev) if prev else {}

        watch_trend = watch_contra = 0

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

        buy_trend = buy_contra = 0

        # Day1 チェック
        if prev is None:
            log.info("前日データなし。Day1 チェックをスキップ。")
        else:
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

        # この日はシグナル0件でも「スクリーニング済み」として記録する
        conn.execute(
            "INSERT OR IGNORE INTO screened_dates (date) VALUES (?)", (today,)
        )
        conn.commit()

        total_watch_trend  += watch_trend
        total_watch_contra += watch_contra
        total_buy_trend    += buy_trend
        total_buy_contra   += buy_contra

    latest_date = dates_to_process[-1]
    log.info(f"本日の buy 推奨合計: {total_buy_trend + total_buy_contra} 件")

    return {
        "watch_trend": total_watch_trend, "watch_contra": total_watch_contra,
        "buy_trend": total_buy_trend, "buy_contra": total_buy_contra, "date": latest_date,
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
