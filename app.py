"""
スイングトレード自動スクリーニング - Streamlit ダッシュボード

実行方法:
    streamlit run app.py
"""

import calendar as _cal
import io
import json
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"


def _resolve_db_path() -> Path:
    """書き込み可能なDBパスを返す。Streamlit Cloud では /tmp を使用する。"""
    # Streamlit Cloud は /mount/src/ にコードをマウントする（大ファイル書き込み不可）
    if str(BASE_DIR).startswith("/mount/src"):
        return Path("/tmp/screening.db")
    default = BASE_DIR / "screening.db"
    try:
        test = default.parent / ".wtest"
        test.touch()
        test.unlink()
        return default
    except OSError:
        return Path("/tmp/screening.db")


DB_PATH = _resolve_db_path()

log = logging.getLogger(__name__)

st.set_page_config(
    page_title="スイングセンサー",
    page_icon="📈",
    layout="wide",
)

# ── デフォルト設定 ────────────────────────────────────────────────────────────

DEFAULT_CONFIG: dict = {
    "thresholds": {
        "trend_day0": {
            "body_pct_min": 4.0, "body_pct_max": 8.0,
            "upper_shadow_max": 2.0, "lower_shadow_min": 0.5,
            "volume_ratio_min": 1.5, "volume_ratio_max": 4.0,
            "kairi25_min": 2.0, "kairi25_max": 7.0,
            "rsi14_min": 52.0, "rsi14_max": 70.0,
        },
        "trend_day1": {"volume_ratio_min": 1.2, "upper_shadow_max": 3.0},
        "contra_day0": {
            "kairi25_max": -10.0, "rsi14_max": 40.0,
            "lower_shadow_min": 1.0, "volume_ratio_min": 1.2,
        },
    },
    "notifications": {
        "email_enabled": False, "email_smtp": "smtp.gmail.com",
        "email_port": 587, "email_user": "", "email_password": "",
        "email_to": "", "line_enabled": False, "line_token": "",
    },
    "schedule": {"enabled": False, "time": "16:00"},
}


# ── Config 読み書き ───────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return _deep_copy(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return _merge(DEFAULT_CONFIG, cfg)
    except Exception:
        return _deep_copy(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _deep_copy(d: dict) -> dict:
    return json.loads(json.dumps(d))


def _merge(base: dict, override: dict) -> dict:
    result = _deep_copy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


# ── GitHub 認証情報取得 ────────────────────────────────────────────────────────

def _gh_secret(key: str) -> str:
    """Streamlit Secrets → 環境変数の順で値を返す"""
    try:
        val = st.secrets.get(key, "")
        if val:
            return str(val).strip()
    except Exception:
        pass
    return os.environ.get(key, "").strip()


def _download_db_from_github() -> bool:
    """起動時に GitHub data ブランチから DB をダウンロードする"""
    repo = _gh_secret("GITHUB_REPO")
    if not repo:
        return False
    token = _gh_secret("GITHUB_TOKEN")
    try:
        owner, repo_name = repo.split("/", 1)
        url = (
            f"https://raw.githubusercontent.com/{owner}/{repo_name}"
            f"/data/screening.db"
        )
        headers = {"Authorization": f"token {token}"} if token else {}
        resp = requests.get(url, headers=headers, stream=True, timeout=180)
        if resp.status_code != 200:
            log.warning(f"DB ダウンロード失敗: HTTP {resp.status_code}")
            return False
        with open(DB_PATH, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        size_mb = DB_PATH.stat().st_size / 1024 / 1024
        log.info(f"DB ダウンロード完了: {size_mb:.1f} MB")
        return True
    except Exception as e:
        log.warning(f"DB ダウンロードエラー: {e}")
        return False


def _run_db_download_ui() -> None:
    """画面上でDBダウンロードを実行し、エラーも表示する"""
    repo = _gh_secret("GITHUB_REPO")
    token = _gh_secret("GITHUB_TOKEN")
    if not repo:
        st.error("GITHUB_REPO が設定されていません。Streamlit Secrets を確認してください。")
        return
    try:
        owner, repo_name = repo.split("/", 1)
        url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/data/screening.db"
        headers = {"Authorization": f"token {token}"} if token else {}

        with st.status("DBダウンロード中...", expanded=True) as dl_status:
            st.write(f"URL: `{url}`")
            resp = requests.get(url, headers=headers, timeout=300)
            st.write(f"HTTP応答: **{resp.status_code}**")

            if resp.status_code != 200:
                dl_status.update(label=f"❌ ダウンロード失敗 (HTTP {resp.status_code})", state="error")
                st.error(resp.text[:500])
                return

            size_mb = len(resp.content) / 1024 / 1024
            st.write(f"受信サイズ: {size_mb:.1f} MB")

            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            DB_PATH.write_bytes(resp.content)
            st.write(f"保存先: `{DB_PATH}`  ({DB_PATH.stat().st_size:,} bytes)")
            dl_status.update(label="✅ ダウンロード完了！", state="complete")

        st.cache_resource.clear()
        st.rerun()

    except Exception as e:
        import traceback
        st.error(f"エラー: {e}")
        st.code(traceback.format_exc())


def _push_db_to_github() -> tuple[bool | None, str]:
    """手動実行後に更新済み DB を GitHub へ保存する。
    戻り値: (成否, ログ文字列)  成否=None はGitHub設定なし"""
    token = _gh_secret("GITHUB_TOKEN")
    repo  = _gh_secret("GITHUB_REPO")
    if not token or not repo:
        return None, ""
    env = _subprocess_env()
    env["GITHUB_TOKEN"] = token
    env["GITHUB_REPO"]  = repo
    try:
        r = subprocess.run(
            [sys.executable, str(BASE_DIR / "push_db.py"), "--push"],
            capture_output=True, text=True, cwd=str(BASE_DIR),
            timeout=600, env=env,
        )
        output = (r.stdout + r.stderr).strip()
        # --timestamp は main ブランチを更新するため Streamlit Cloud の不要な
        # 再デプロイを引き起こす。/tmp/ が消えてデータが失われるので呼ばない。
        return (r.returncode == 0), output
    except Exception as e:
        return False, str(e)


# ── DB 接続 ───────────────────────────────────────────────────────────────────

@st.cache_resource
def get_conn():
    """DB接続を返す。DBがなければNoneを返す（ダウンロードは main() で行う）"""
    if not DB_PATH.exists():
        return None
    try:
        return sqlite3.connect(str(DB_PATH), check_same_thread=False)
    except Exception as e:
        log.error(f"sqlite3.connect failed: {e}")
        return None


def _diagnose() -> list[str]:
    """起動時の状態診断情報を返す（エラー時のデバッグ用）"""
    info = []
    info.append(f"**DBパス:** `{DB_PATH}`")
    info.append(f"**DBファイル存在:** {DB_PATH.exists()}")
    repo = _gh_secret("GITHUB_REPO")
    token = _gh_secret("GITHUB_TOKEN")
    info.append(f"**GITHUB_REPO 設定済み:** {bool(repo)} ({repo or '未設定'})")
    info.append(f"**GITHUB_TOKEN 設定済み:** {bool(token)}")
    if repo:
        try:
            owner, repo_name = repo.split("/", 1)
            url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/data/screening.db"
            info.append(f"**ダウンロードURL:** `{url}`")
            hdrs = {"Authorization": f"token {token}"} if token else {}
            resp = requests.head(url, headers=hdrs, timeout=15)
            info.append(f"**HTTP応答:** {resp.status_code}")
        except Exception as e:
            info.append(f"**接続確認エラー:** {e}")
    return info


# ── ヘルパー ──────────────────────────────────────────────────────────────────

def strategy_label(s: str) -> str:
    return {"trend": "⚡ 順張り", "contra": "🔄 逆張り"}.get(s, s)


def signal_label(s: str) -> str:
    return {"buy": "✅ 買い推奨", "watch": "👁 監視中"}.get(s, s)


def parse_notes(notes_json: str) -> dict:
    try:
        return json.loads(notes_json) if notes_json else {}
    except Exception:
        return {}


def fmt_pct(v, dec=2) -> str:
    if v is None or pd.isna(v):
        return "-"
    return f"{v:+.{dec}f}%"


def fmt_num(v, dec=2) -> str:
    if v is None or pd.isna(v):
        return "-"
    return f"{v:.{dec}f}"


def fmt_price(v) -> str:
    if v is None or pd.isna(v):
        return "-"
    return f"¥{int(v):,}"


def ticker_to_kabutan_url(ticker: str) -> str:
    code = ticker.replace(".T", "")
    return f"https://kabutan.jp/stock/?code={code}"


def nth_trading_day_after(conn, ticker: str, from_date: str, n: int) -> str | None:
    """prices_raw でfrom_dateの翌n営業日の日付を返す"""
    rows = conn.execute(
        "SELECT date FROM prices_raw WHERE ticker=? AND date > ? ORDER BY date LIMIT ?",
        (ticker, from_date, n),
    ).fetchall()
    return rows[n - 1][0] if len(rows) >= n else None


def _n225_close_at(conn, date: str) -> float | None:
    """指定日の日経平均終値を indicators から取得する"""
    row = conn.execute(
        "SELECT n225_close FROM indicators WHERE date=? LIMIT 1", (date,)
    ).fetchone()
    return row[0] if row else None


# ── スケジューラ ──────────────────────────────────────────────────────────────

@st.cache_resource
def _get_scheduler():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
        scheduler.start()
        return scheduler
    except Exception:
        return None


def _subprocess_env() -> dict:
    """サブプロセス用の環境変数（DBパスを統一して渡す）"""
    env = os.environ.copy()
    env["SCREENING_DB_PATH"] = str(DB_PATH)
    return env


def _run_pipeline_job():
    """APScheduler から呼ばれる定期実行ジョブ"""
    log.info("自動スクリーニング開始")
    _env = _subprocess_env()
    r1 = subprocess.run(
        [sys.executable, str(BASE_DIR / "daily_update.py")],
        capture_output=True, text=True, cwd=str(BASE_DIR), env=_env,
    )
    if r1.returncode != 0:
        log.error(f"DB更新失敗:\n{r1.stderr}")
        return
    r2 = subprocess.run(
        [sys.executable, str(BASE_DIR / "screening.py")],
        capture_output=True, text=True, cwd=str(BASE_DIR), env=_env,
    )
    if r2.returncode != 0:
        log.error(f"スクリーニング失敗:\n{r2.stderr}")
        return

    # 通知
    try:
        from notifier import notify
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        today = conn.execute(
            "SELECT MAX(date) FROM signals WHERE signal_type='buy'"
        ).fetchone()[0] or ""
        buy_rows = conn.execute(
            """SELECT s.ticker, w.name, s.strategy
               FROM signals s LEFT JOIN watchlist w ON w.ticker=s.ticker
               WHERE s.date=? AND s.signal_type='buy'""",
            (today,),
        ).fetchall()
        watch_rows = conn.execute(
            "SELECT ticker FROM signals WHERE date=? AND signal_type='watch'",
            (today,),
        ).fetchall()
        conn.close()

        buy_list   = [{"ticker": r[0], "name": r[1], "strategy": r[2]} for r in buy_rows]
        watch_list = [{"ticker": r[0]} for r in watch_rows]
        cfg = load_config()
        notify(cfg, buy_list, watch_list, today)
    except Exception as e:
        log.error(f"通知失敗: {e}")

    log.info("自動スクリーニング完了")


def apply_schedule(cfg: dict) -> None:
    """スケジュール設定をAPSchedulerに反映する"""
    scheduler = _get_scheduler()
    if scheduler is None:
        return
    try:
        scheduler.remove_all_jobs()
        sched = cfg.get("schedule", {})
        if sched.get("enabled") and sched.get("time"):
            h, m = sched["time"].split(":")
            scheduler.add_job(
                _run_pipeline_job,
                trigger="cron",
                hour=int(h), minute=int(m),
                id="daily_pipeline",
                replace_existing=True,
            )
            log.info(f"スケジュール設定: 毎日 {sched['time']}")
    except Exception as e:
        log.error(f"スケジュール設定失敗: {e}")


# ── 手動実行 ──────────────────────────────────────────────────────────────────

def run_pipeline_manual(placeholder):
    """手動実行ボタン用。placeholderにst.statusを使って進捗を表示する。"""
    with placeholder.container():
        with st.status("パイプライン実行中...", expanded=True) as status:
            prog = st.progress(0, text="⏳ DB更新を開始しています...")

            _env = _subprocess_env()

            st.write("**STEP 1/2** — 株価データ・指標を更新中 (daily_update.py)")
            r1 = subprocess.run(
                [sys.executable, str(BASE_DIR / "daily_update.py")],
                capture_output=True, text=True, cwd=str(BASE_DIR), env=_env,
            )
            prog.progress(50, text="⏳ スクリーニング実行中...")

            if r1.returncode != 0:
                status.update(label="❌ DB更新でエラーが発生しました", state="error")
                st.error(r1.stderr[-2000:] if r1.stderr else "不明なエラー")
                return

            st.write("**STEP 2/2** — スクリーニング条件を判定中 (screening.py)")
            r2 = subprocess.run(
                [sys.executable, str(BASE_DIR / "screening.py")],
                capture_output=True, text=True, cwd=str(BASE_DIR), env=_env,
            )
            prog.progress(80, text="⏳ 通知を送信中...")

            if r2.returncode != 0:
                status.update(label="❌ スクリーニングでエラーが発生しました", state="error")
                st.error(r2.stderr[-2000:] if r2.stderr else "不明なエラー")
                return

            # logging はデフォルトで stderr に出力されるため stdout+stderr を結合
            out1 = (r1.stdout + r1.stderr).strip()
            out2 = (r2.stdout + r2.stderr).strip()
            key_lines1 = [l for l in out1.splitlines()
                          if l.strip() and ("更新完了" in l or "新規データ" in l)]
            key_lines2 = [l for l in out2.splitlines()
                          if l.strip() and ("スクリーニング対象日" in l
                                            or "buy 推奨" in l or "watch 生成" in l)]
            st.session_state["_pipeline_log"] = {
                "key1": key_lines1,
                "key2": key_lines2,
                "full1": out1[-3000:],
                "full2": out2[-2000:],
            }

            # 通知
            try:
                from notifier import notify
                conn_tmp = sqlite3.connect(str(DB_PATH), check_same_thread=False)
                today = conn_tmp.execute(
                    "SELECT MAX(date) FROM signals WHERE signal_type='buy'"
                ).fetchone()[0] or ""
                buy_rows = conn_tmp.execute(
                    """SELECT s.ticker, w.name, s.strategy
                       FROM signals s LEFT JOIN watchlist w ON w.ticker=s.ticker
                       WHERE s.date=? AND s.signal_type='buy'""",
                    (today,),
                ).fetchall()
                watch_rows = conn_tmp.execute(
                    "SELECT ticker FROM signals WHERE date=? AND signal_type='watch'",
                    (today,),
                ).fetchall()
                conn_tmp.close()

                buy_list   = [{"ticker": r[0], "name": r[1], "strategy": r[2]} for r in buy_rows]
                watch_list = [{"ticker": r[0]} for r in watch_rows]
                cfg = load_config()
                n_result = notify(cfg, buy_list, watch_list, today)
                notif_msg = []
                if n_result.get("email"):
                    notif_msg.append("📧 メール送信済み")
                if n_result.get("line"):
                    notif_msg.append("💬 LINE送信済み")
                if notif_msg:
                    st.info("  ".join(notif_msg))
            except Exception as e:
                st.caption(f"通知スキップ: {e}")

            # GitHub へ DB を保存（クラウド環境）
            prog.progress(90, text="⏳ GitHub へ DB を保存中...")
            push_result, push_log = _push_db_to_github()

            # 結果をsession_stateに保存し、rerun後に表示する
            if push_result is True:
                st.session_state["_pipeline_msg"] = ("success", "✅ スクリーニング完了・GitHub へ保存しました。")
            elif push_result is False:
                st.session_state["_pipeline_msg"] = ("warning", "⚠️ スクリーニング完了しましたが、GitHub への保存に失敗しました。")
                if push_log:
                    st.session_state["_pipeline_push_log"] = push_log
            else:
                st.session_state["_pipeline_msg"] = ("success", "✅ スクリーニングが完了しました。")

            prog.progress(100, text="✅ 完了！")
            status.update(label="✅ 実行完了！", state="complete")
            st.session_state["_pipeline_done"] = True


# ── Excel エクスポート ────────────────────────────────────────────────────────

def build_excel(conn, date_from: str, date_to: str) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # シグナル
        df_sig = pd.read_sql_query(
            """
            SELECT s.date, s.ticker, w.name, s.strategy, s.signal_type,
                   s.entry_date, s.entry_price,
                   p.open, p.high, p.low, p.close, p.volume,
                   i.rsi14, i.kairi25, i.volume_ratio25, i.body_pct,
                   i.upper_shadow, i.lower_shadow, i.macd_hist,
                   i.ma5, i.ma25, i.ma75
            FROM signals s
            LEFT JOIN watchlist w ON w.ticker = s.ticker
            LEFT JOIN prices_raw p ON p.ticker = s.ticker AND p.date = s.date
            LEFT JOIN indicators i ON i.ticker = s.ticker AND i.date = s.date
            WHERE s.date BETWEEN ? AND ?
            ORDER BY s.date DESC, s.signal_type, s.ticker
            """,
            conn, params=(date_from, date_to),
        )
        df_sig.to_excel(writer, sheet_name="シグナル", index=False)

        # prices_raw
        df_pr = pd.read_sql_query(
            "SELECT * FROM prices_raw WHERE date BETWEEN ? AND ? ORDER BY date DESC, ticker",
            conn, params=(date_from, date_to),
        )
        df_pr.to_excel(writer, sheet_name="株価データ", index=False)

        # watchlist
        df_wl = pd.read_sql_query("SELECT * FROM watchlist ORDER BY ticker", conn)
        df_wl.to_excel(writer, sheet_name="銘柄リスト", index=False)

    return buf.getvalue()


# ── パフォーマンス計算 ────────────────────────────────────────────────────────

def calc_performance(conn, year: int, month: int, strategy_filter: str) -> pd.DataFrame:
    """買いシグナルの 3/5/10 日後リターンと日経比較を計算して返す"""
    if month == 0:
        date_cond = f"strftime('%Y', s.date) = '{year}'"
    else:
        date_cond = f"strftime('%Y-%m', s.date) = '{year}-{month:02d}'"

    strat_cond = ""
    if strategy_filter == "順張り":
        strat_cond = "AND s.strategy='trend'"
    elif strategy_filter == "逆張り":
        strat_cond = "AND s.strategy='contra'"

    rows = conn.execute(
        f"""
        SELECT s.ticker, w.name, s.strategy, s.date, s.entry_date,
               s.entry_price,
               p_e.open  AS entry_open,
               p_e.close AS entry_close
        FROM signals s
        LEFT JOIN watchlist w ON w.ticker = s.ticker
        LEFT JOIN prices_raw p_e ON p_e.ticker = s.ticker AND p_e.date = s.entry_date
        WHERE s.signal_type = 'buy' AND {date_cond} {strat_cond}
        ORDER BY s.date DESC
        """,
    ).fetchall()

    cols = ["ticker", "name", "strategy", "date", "entry_date",
            "entry_price", "entry_open", "entry_close"]
    records = []

    for row in rows:
        d = dict(zip(cols, row))
        buy_price = d["entry_price"] or d["entry_open"] or d["entry_close"]
        entry_d   = d["entry_date"] or d["date"]
        n225_entry = _n225_close_at(conn, entry_d) if buy_price else None

        def _ret_at(n: int):
            if not buy_price:
                return None, None
            dn = nth_trading_day_after(conn, d["ticker"], entry_d, n)
            if not dn:
                return None, None
            pr = conn.execute(
                "SELECT close FROM prices_raw WHERE ticker=? AND date=?",
                (d["ticker"], dn),
            ).fetchone()
            price_n  = pr[0] if pr else None
            ret_n    = (price_n - buy_price) / buy_price * 100 if price_n else None
            n225_n   = _n225_close_at(conn, dn)
            n225_ret = (n225_n - n225_entry) / n225_entry * 100 if (n225_entry and n225_n) else None
            return ret_n, n225_ret

        r3,  n3  = _ret_at(3)
        r5,  n5  = _ret_at(5)
        r10, n10 = _ret_at(10)
        win = (r5 > 0) if r5 is not None else None

        records.append({
            "シグナル日":    d["date"],
            "ティッカー":    d["ticker"],
            "kabutan_url":   ticker_to_kabutan_url(d["ticker"]),
            "銘柄名":        d["name"] or "-",
            "戦略":          strategy_label(d["strategy"]),
            "エントリー日":  entry_d or "-",
            "エントリー価格": fmt_price(buy_price) if buy_price else "未確定",
            "3日後%":        round(r3,  2) if r3  is not None else None,
            "日経3日後%":    round(n3,  2) if n3  is not None else None,
            "5日後%":        round(r5,  2) if r5  is not None else None,
            "日経5日後%":    round(n5,  2) if n5  is not None else None,
            "10日後%":       round(r10, 2) if r10 is not None else None,
            "日経10日後%":   round(n10, 2) if n10 is not None else None,
            "_win":          win,
            "_ret3":         r3,
            "_ret5":         r5,
            "_ret10":        r10,
        })

    return pd.DataFrame(records)


# ── ページ描画 ────────────────────────────────────────────────────────────────

def render_header(conn):
    col_title, col_date, col_buy, col_watch, col_run = st.columns([3, 1.2, 1, 1, 1.5])

    with col_title:
        st.title("📈 スイングセンサー")

    latest_row = conn.execute("SELECT MAX(date) FROM indicators").fetchone()
    latest_date = latest_row[0] if latest_row and latest_row[0] else "-"

    buy_count = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE date=? AND signal_type='buy'",
        (latest_date,),
    ).fetchone()[0]
    watch_count = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE date=? AND signal_type='watch'",
        (latest_date,),
    ).fetchone()[0]

    with col_date:
        st.metric("最終更新日", latest_date)
    with col_buy:
        st.metric("買い推奨", f"{buy_count} 件")
    with col_watch:
        st.metric("監視中", f"{watch_count} 件")
    with col_run:
        st.write("")
        if st.button("🔄 手動実行", type="primary", use_container_width=True):
            st.session_state["run_pipeline"] = True

    # 自動実行スケジュールの状態表示
    cfg = load_config()
    if cfg["schedule"]["enabled"]:
        st.caption(f"🕐 自動実行: 毎日 {cfg['schedule']['time']} に実行予定")


def _show_signal_df(df: pd.DataFrame, strategy_filter: str, key_suffix: str) -> pd.DataFrame:
    """戦略フィルタ + 株探リンク付き dataframe を表示して、フィルタ後の df を返す"""
    if strategy_filter == "順張り":
        df = df[df["_strategy"] == "trend"]
    elif strategy_filter == "逆張り":
        df = df[df["_strategy"] == "contra"]

    display = df.drop(columns=["_strategy"])
    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "kabutan_url": st.column_config.LinkColumn(
                "株探",
                display_text="株探↗",
                help="株探の銘柄ページを開く",
            ),
        },
    )
    return df


def render_buy_tab(conn):
    st.subheader("📋 本日の買い推奨（翌営業日 寄付きエントリー）")

    # 最新スクリーニング日 = indicators の最終日（buyゼロ日でも正しく取得）
    latest_date = conn.execute("SELECT MAX(date) FROM indicators").fetchone()[0]
    if not latest_date:
        st.info("データがありません。「手動実行」ボタンでスクリーニングを実行してください。")
        return

    col_f1, col_f2 = st.columns([1, 3])
    with col_f1:
        strategy_filter = st.selectbox("戦略", ["全て", "順張り", "逆張り"], key="buy_strategy")

    rows = conn.execute(
        """
        SELECT s.ticker, w.name, s.strategy, s.entry_date,
               p.close, i.rsi14, i.kairi25, i.volume_ratio25,
               i.body_pct, i.macd_hist
        FROM signals s
        LEFT JOIN watchlist w ON w.ticker = s.ticker
        LEFT JOIN indicators i ON i.ticker = s.ticker AND i.date = s.date
        LEFT JOIN prices_raw p ON p.ticker = s.ticker AND p.date = s.date
        WHERE s.date=? AND s.signal_type='buy'
        ORDER BY s.strategy, s.ticker
        """,
        (latest_date,),
    ).fetchall()

    if not rows:
        st.info(f"{latest_date} の買い推奨はありません（Day1 条件を満たした銘柄なし）。")
        return

    records = []
    for ticker, name, strategy, entry_date, close, rsi14, kairi25, vol_ratio, body_pct, macd_hist in rows:
        records.append({
            "kabutan_url":  ticker_to_kabutan_url(ticker),
            "ティッカー":   ticker,
            "銘柄名":       name or "-",
            "戦略":         strategy_label(strategy),
            "エントリー日": entry_date or "-",
            "終値(参考)":   fmt_price(close),
            "RSI14":        fmt_num(rsi14, 1),
            "25MA乖離%":    fmt_pct(kairi25, 1),
            "出来高比":     fmt_num(vol_ratio, 2),
            "実体%":        fmt_pct(body_pct, 1),
            "MACDヒスト":   fmt_num(macd_hist, 4),
            "_strategy":    strategy,
        })

    df = pd.DataFrame(records)
    df = _show_signal_df(df, strategy_filter, "buy")
    st.caption(f"シグナル日: {latest_date}  |  件数: {len(df)}")


def render_watch_tab(conn):
    st.subheader("👁 監視中（Day0 通過 → 翌日 Day1 確認待ち）")

    today_str = datetime.now().strftime("%Y-%m-%d")

    col_f1, _ = st.columns([1, 3])
    with col_f1:
        strategy_filter = st.selectbox("戦略", ["全て", "順張り", "逆張り"], key="watch_strategy")

    # entry_date > today のもののみ表示（Day1チェック済み・期限切れを除外）
    # さらに buy 昇格済みも除外
    rows = conn.execute(
        """
        SELECT s.ticker, w.name, s.strategy, s.date,
               p.close, i.rsi14, i.kairi25, i.volume_ratio25,
               i.body_pct, i.lower_shadow, i.macd_hist
        FROM signals s
        LEFT JOIN watchlist w ON w.ticker = s.ticker
        LEFT JOIN indicators i ON i.ticker = s.ticker AND i.date = s.date
        LEFT JOIN prices_raw p ON p.ticker = s.ticker AND p.date = s.date
        WHERE s.signal_type='watch'
          AND s.entry_date > ?
          AND NOT EXISTS (
              SELECT 1 FROM signals s2
              WHERE s2.ticker = s.ticker
                AND s2.strategy = s.strategy
                AND s2.signal_type = 'buy'
                AND s2.date > s.date
          )
        ORDER BY s.date DESC, s.strategy, s.ticker
        """,
        (today_str,),
    ).fetchall()

    if not rows:
        st.info("現在 Day1 確認待ちの銘柄はありません。")
        return

    records = []
    for ticker, name, strategy, date, close, rsi14, kairi25, vol_ratio, body_pct, lower_shadow, macd_hist in rows:
        records.append({
            "kabutan_url":  ticker_to_kabutan_url(ticker),
            "ティッカー":   ticker,
            "銘柄名":       name or "-",
            "戦略":         strategy_label(strategy),
            "シグナル日":   date,
            "終値(参考)":   fmt_price(close),
            "RSI14":        fmt_num(rsi14, 1),
            "25MA乖離%":    fmt_pct(kairi25, 1),
            "出来高比":     fmt_num(vol_ratio, 2),
            "実体%":        fmt_pct(body_pct, 1),
            "下髭%":        fmt_pct(lower_shadow, 1),
            "MACDヒスト":   fmt_num(macd_hist, 4),
            "_strategy":    strategy,
        })

    df = pd.DataFrame(records)
    df = _show_signal_df(df, strategy_filter, "watch")
    st.caption(
        f"件数: {len(df)}  |  翌営業日の値動きを確認後、Day1条件を満たせば買い推奨に昇格します"
    )


def _render_day_detail(conn, date_str: str) -> None:
    """カレンダーで選択した日のシグナル詳細を表示する"""
    st.markdown(f"#### 📋 {date_str} のシグナル詳細")

    ret_col_cfg = {
        "kabutan_url": st.column_config.LinkColumn("株探", display_text="↗"),
        "3日後%":   st.column_config.NumberColumn("3日後%",  format="%.2f%%"),
        "5日後%":   st.column_config.NumberColumn("5日後%",  format="%.2f%%"),
        "10日後%":  st.column_config.NumberColumn("10日後%", format="%.2f%%"),
        "日経3日後%":  st.column_config.NumberColumn("日経3日後%",  format="%.2f%%"),
        "日経5日後%":  st.column_config.NumberColumn("日経5日後%",  format="%.2f%%"),
        "日経10日後%": st.column_config.NumberColumn("日経10日後%", format="%.2f%%"),
    }

    for sig_type, section_label in [("buy", "買い推奨"), ("watch", "監視")]:
        rows = conn.execute(
            """
            SELECT s.ticker, w.name, s.strategy, s.entry_date,
                   p_e.open AS eo, p_e.close AS ec, s.entry_price
            FROM signals s
            LEFT JOIN watchlist w ON w.ticker = s.ticker
            LEFT JOIN prices_raw p_e
                   ON p_e.ticker = s.ticker AND p_e.date = s.entry_date
            WHERE s.date=? AND s.signal_type=?
            ORDER BY s.strategy, s.ticker
            """,
            (date_str, sig_type),
        ).fetchall()
        if not rows:
            continue

        st.markdown(f"**{section_label}**（{len(rows)} 件）")
        records = []
        for ticker, name, strategy, entry_date, eo, ec, ep in rows:
            entry_d   = entry_date or date_str
            buy_price = ep or eo or ec
            rec = {
                "kabutan_url": ticker_to_kabutan_url(ticker),
                "銘柄名":      name or "-",
                "戦略":        strategy_label(strategy),
                "エントリー日": entry_d,
                "価格":        fmt_price(buy_price) if buy_price else "未確定",
            }
            if sig_type == "buy" and buy_price:
                n225_e = _n225_close_at(conn, entry_d)
                for n in [3, 5, 10]:
                    dn = nth_trading_day_after(conn, ticker, entry_d, n)
                    ret_n = n225_ret_n = None
                    if dn:
                        pr = conn.execute(
                            "SELECT close FROM prices_raw WHERE ticker=? AND date=?",
                            (ticker, dn),
                        ).fetchone()
                        pn = pr[0] if pr else None
                        ret_n = (pn - buy_price) / buy_price * 100 if pn else None
                        n225_n = _n225_close_at(conn, dn)
                        n225_ret_n = (n225_n - n225_e) / n225_e * 100 if (n225_e and n225_n) else None
                    rec[f"{n}日後%"]    = ret_n
                    rec[f"日経{n}日後%"] = n225_ret_n
            records.append(rec)

        st.dataframe(
            pd.DataFrame(records),
            use_container_width=True,
            hide_index=True,
            column_config=ret_col_cfg,
        )


def _perf_metrics_block(df: pd.DataFrame, label: str) -> None:
    """通算成績メトリクスを1ブロック分表示する"""
    if df.empty:
        st.caption(f"{label}：シグナルなし")
        return
    n_total = len(df)
    v5  = df.dropna(subset=["_ret5"])
    v3  = df.dropna(subset=["_ret3"])
    v10 = df.dropna(subset=["_ret10"])
    n5  = len(v5)
    win_rate = len(v5[v5["_win"] == True]) / n5 * 100 if n5 > 0 else None
    st.markdown(f"**{label}**")
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1: st.metric("シグナル数", f"{n_total} 件")
    with c2: st.metric("勝率(5日後)", f"{win_rate:.1f}%" if win_rate is not None else "-")
    for col, vn, n in [(c3, v3, 3), (c4, v5, 5), (c5, v10, 10)]:
        avg = vn[f"_ret{n}"].mean()   if len(vn) > 0 else None
        med = vn[f"_ret{n}"].median() if len(vn) > 0 else None
        with col:
            st.metric(
                f"{n}日後 平均/中央",
                f"{fmt_pct(avg,1)} / {fmt_pct(med,1)}" if avg is not None else "-",
            )


def _signal_list_expander(conn, year: int, month: int,
                           strat_filter: str, label: str) -> None:
    """期間内の全シグナル（buy＋watch）を折りたたみ表示する"""
    strat_sql = ""
    if strat_filter == "順張り":
        strat_sql = "AND s.strategy='trend'"
    elif strat_filter == "逆張り":
        strat_sql = "AND s.strategy='contra'"

    date_cond = (
        f"strftime('%Y',s.date)='{year}'" if month == 0
        else f"strftime('%Y-%m',s.date)='{year}-{month:02d}'"
    )

    rows = conn.execute(
        f"""
        SELECT s.date, s.ticker, w.name, s.strategy, s.signal_type,
               s.entry_date, p.close
        FROM signals s
        LEFT JOIN watchlist w ON w.ticker = s.ticker
        LEFT JOIN prices_raw p ON p.ticker = s.ticker AND p.date = s.date
        WHERE {date_cond} {strat_sql}
        ORDER BY s.date DESC, s.signal_type DESC, s.strategy, s.ticker
        """,
    ).fetchall()

    with st.expander(f"📋 {label} シグナル一覧（{len(rows)} 件）"):
        if not rows:
            st.info("該当するシグナルはありません。")
            return
        records = []
        for date, ticker, name, strategy, stype, entry_date, close in rows:
            records.append({
                "kabutan_url":  ticker_to_kabutan_url(ticker),
                "銘柄名":       name or "-",
                "戦略":         strategy_label(strategy),
                "種別":         "買い推奨" if stype == "buy" else "監視",
                "シグナル日":   date,
                "エントリー日": entry_date or "-",
                "終値(参考)":   fmt_price(close),
            })
        st.dataframe(
            pd.DataFrame(records),
            use_container_width=True,
            hide_index=True,
            column_config={
                "kabutan_url": st.column_config.LinkColumn("株探", display_text="↗")
            },
        )


def render_history_tab(conn):
    st.subheader("📊 スクリーニング履歴")

    # ── 年月ナビゲーション ──────────────────────────────────────────────────────
    today = datetime.now()
    if "hist_year"  not in st.session_state:
        st.session_state["hist_year"]  = today.year
    if "hist_month" not in st.session_state:
        st.session_state["hist_month"] = today.month

    c_prev, c_mid, c_next, c_filt = st.columns([1, 2, 1, 2])
    with c_prev:
        if st.button("◀ 前月", key="cal_prev"):
            m, y = st.session_state["hist_month"] - 1, st.session_state["hist_year"]
            if m < 1: m, y = 12, y - 1
            st.session_state.update(hist_month=m, hist_year=y, hist_selected_date=None)
            st.rerun()
    with c_mid:
        st.markdown(
            f"<h3 style='text-align:center;margin:4px 0'>"
            f"{st.session_state['hist_year']}年 {st.session_state['hist_month']}月</h3>",
            unsafe_allow_html=True,
        )
    with c_next:
        if st.button("翌月 ▶", key="cal_next"):
            m, y = st.session_state["hist_month"] + 1, st.session_state["hist_year"]
            if m > 12: m, y = 1, y + 1
            st.session_state.update(hist_month=m, hist_year=y, hist_selected_date=None)
            st.rerun()
    with c_filt:
        strat_filter = st.selectbox("戦略", ["全て", "順張り", "逆張り"], key="hist_strategy")

    year  = st.session_state["hist_year"]
    month = st.session_state["hist_month"]
    ym    = f"{year}-{month:02d}"

    # ── 通算成績（月間 ＋ 年間）────────────────────────────────────────────────
    with st.spinner("集計中..."):
        monthly_df = calc_performance(conn, year, month, strat_filter)
        yearly_df  = calc_performance(conn, year, 0,     strat_filter)

    has_any_buy = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE signal_type='buy'"
    ).fetchone()[0]

    st.markdown("#### 通算成績")
    col_m, col_y = st.columns(2)
    with col_m:
        if not monthly_df.empty:
            _perf_metrics_block(monthly_df, f"{year}年{month}月（月間）")
        elif has_any_buy == 0:
            st.info("まだ買い推奨シグナルがありません。手動実行後に結果が表示されます。")
        else:
            st.caption(f"{year}年{month}月：シグナルなし")
    with col_y:
        _perf_metrics_block(yearly_df, f"{year}年（年間）")

    st.divider()

    # ── カレンダーデータ準備 ───────────────────────────────────────────────────
    ind_dates = set(r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM indicators WHERE strftime('%Y-%m',date)=?", (ym,)
    ).fetchall())

    # 戦略フィルタをカレンダー件数にも反映
    strat_sql_cal = ""
    if strat_filter == "順張り":
        strat_sql_cal = "AND strategy='trend'"
    elif strat_filter == "逆張り":
        strat_sql_cal = "AND strategy='contra'"

    sig_rows = conn.execute(
        f"SELECT date, signal_type, COUNT(*) FROM signals "
        f"WHERE strftime('%Y-%m',date)=? {strat_sql_cal} GROUP BY date, signal_type",
        (ym,),
    ).fetchall()
    sigs: dict[str, dict] = {}
    for d, stype, cnt in sig_rows:
        if d not in sigs:
            sigs[d] = {"buy": 0, "watch": 0}
        sigs[d][stype] = cnt

    min_ind = conn.execute("SELECT MIN(date) FROM indicators").fetchone()[0] or ""
    max_ind = conn.execute("SELECT MAX(date) FROM indicators").fetchone()[0] or ""
    _, last_day = _cal.monthrange(year, month)
    holidays: set[str] = set()
    for day in range(1, last_day + 1):
        dt = datetime(year, month, day)
        ds = dt.strftime("%Y-%m-%d")
        if dt.weekday() < 5 and min_ind <= ds <= max_ind and ds not in ind_dates:
            holidays.add(ds)

    # ── カレンダー描画 ────────────────────────────────────────────────────────
    st.markdown("""
    <style>
    div[data-testid="stButton"]>button {
        font-size:0.7rem; padding:2px 2px; min-height:50px;
        white-space:pre-wrap; line-height:1.3;
    }
    </style>""", unsafe_allow_html=True)

    col_w    = [0.7, 1.0, 1.0, 1.0, 1.0, 1.0, 0.7]
    headers  = ["日", "月", "火", "水", "木", "金", "土"]
    h_colors = ["#cc0000","#333","#333","#333","#333","#333","#0066cc"]

    hcols = st.columns(col_w)
    for c, h, color in zip(hcols, headers, h_colors):
        with c:
            st.markdown(
                f"<div style='text-align:center;font-weight:bold;color:{color};"
                f"border-bottom:2px solid #ccc;padding:3px'>{h}</div>",
                unsafe_allow_html=True,
            )

    sel = st.session_state.get("hist_selected_date")
    month_cal = _cal.monthcalendar(year, month)

    for week in month_cal:
        sun_first = [week[6]] + week[:6]
        wcols = st.columns(col_w)
        for ci, (c, day) in enumerate(zip(wcols, sun_first)):
            with c:
                if day == 0:
                    st.write(" ")
                    continue
                ds        = f"{year}-{month:02d}-{day:02d}"
                day_color = h_colors[ci]
                bg_sel    = "background:#ddeeff;border:2px solid #1A3568;" if ds == sel else "border:1px solid #e0e0e0;"

                if ds in sigs:
                    b = sigs[ds].get("buy", 0)
                    w = sigs[ds].get("watch", 0)
                    if st.button(f"{day}日\n買{b} 監{w}", key=f"c_{ds}",
                                 use_container_width=True):
                        st.session_state["hist_selected_date"] = ds
                        st.rerun()
                elif ds in holidays:
                    st.markdown(
                        f"<div style='text-align:center;{bg_sel}border-radius:4px;"
                        f"padding:4px;font-size:0.75rem'>"
                        f"<b style='color:{day_color}'>{day}</b>"
                        f"<br><span style='color:#bbb'>休場</span></div>",
                        unsafe_allow_html=True,
                    )
                elif ds in ind_dates:
                    st.markdown(
                        f"<div style='text-align:center;background:#f7f7f7;{bg_sel}"
                        f"border-radius:4px;padding:4px;font-size:0.75rem'>"
                        f"<b style='color:{day_color}'>{day}</b>"
                        f"<br><span style='color:#bbb'>未実行</span></div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"<div style='text-align:center;padding:4px;"
                        f"font-size:0.75rem;color:{day_color}'><b>{day}</b></div>",
                        unsafe_allow_html=True,
                    )

    # ── 日付クリック時の詳細 ──────────────────────────────────────────────────
    if sel and sel in sigs:
        st.divider()
        _render_day_detail(conn, sel)
    elif sel and sel not in sigs:
        st.session_state.pop("hist_selected_date", None)

    # ── 期間別シグナル一覧 ────────────────────────────────────────────────────
    st.divider()
    _signal_list_expander(conn, year, month, strat_filter,
                          f"{year}年{month}月")
    _signal_list_expander(conn, year, 0, strat_filter,
                          f"{year}年（年間）")


def render_settings_tab(conn):
    st.subheader("⚙️ 設定")
    cfg = load_config()

    # ── 1. スクリーニング条件閾値 ─────────────────────────────────────────────
    with st.expander("📐 スクリーニング条件の閾値", expanded=True):
        st.markdown("**順張り戦略 Day0 条件**")
        t0 = cfg["thresholds"]["trend_day0"]
        c1, c2, c3, c4 = st.columns(4)
        t0["body_pct_min"]    = c1.number_input("実体% 下限", value=float(t0["body_pct_min"]),    step=0.5, key="t0_bp_min")
        t0["body_pct_max"]    = c2.number_input("実体% 上限", value=float(t0["body_pct_max"]),    step=0.5, key="t0_bp_max")
        t0["upper_shadow_max"]= c3.number_input("上髭% 上限", value=float(t0["upper_shadow_max"]), step=0.5, key="t0_us_max")
        t0["lower_shadow_min"]= c4.number_input("下髭% 下限", value=float(t0["lower_shadow_min"]), step=0.5, key="t0_ls_min")
        c5, c6, c7, c8 = st.columns(4)
        t0["volume_ratio_min"]= c5.number_input("出来高比 下限", value=float(t0["volume_ratio_min"]), step=0.1, key="t0_vr_min")
        t0["volume_ratio_max"]= c6.number_input("出来高比 上限", value=float(t0["volume_ratio_max"]), step=0.1, key="t0_vr_max")
        t0["kairi25_min"]     = c7.number_input("25MA乖離% 下限", value=float(t0["kairi25_min"]),  step=0.5, key="t0_k_min")
        t0["kairi25_max"]     = c8.number_input("25MA乖離% 上限", value=float(t0["kairi25_max"]),  step=0.5, key="t0_k_max")
        c9, c10 = st.columns([1, 3])
        t0["rsi14_min"]       = c9.number_input("RSI14 下限",  value=float(t0["rsi14_min"]),  step=1.0, key="t0_r_min")
        t0["rsi14_max"]       = c10.number_input("RSI14 上限", value=float(t0["rsi14_max"]),  step=1.0, key="t0_r_max")

        st.markdown("**順張り戦略 Day1 条件**")
        t1 = cfg["thresholds"]["trend_day1"]
        d1c1, d1c2 = st.columns(2)
        t1["volume_ratio_min"] = d1c1.number_input("出来高比 下限", value=float(t1["volume_ratio_min"]), step=0.1, key="t1_vr_min")
        t1["upper_shadow_max"] = d1c2.number_input("上髭% 上限",   value=float(t1["upper_shadow_max"]),  step=0.5, key="t1_us_max")

        st.markdown("**逆張り戦略 Day0 条件**")
        c0 = cfg["thresholds"]["contra_day0"]
        cc1, cc2, cc3, cc4 = st.columns(4)
        c0["kairi25_max"]      = cc1.number_input("25MA乖離% 上限", value=float(c0["kairi25_max"]),     step=1.0, key="c0_k_max")
        c0["rsi14_max"]        = cc2.number_input("RSI14 上限",     value=float(c0["rsi14_max"]),       step=1.0, key="c0_r_max")
        c0["lower_shadow_min"] = cc3.number_input("下髭% 下限",     value=float(c0["lower_shadow_min"]), step=0.5, key="c0_ls_min")
        c0["volume_ratio_min"] = cc4.number_input("出来高比 下限",  value=float(c0["volume_ratio_min"]), step=0.1, key="c0_vr_min")

        cfg["thresholds"]["trend_day0"]  = t0
        cfg["thresholds"]["trend_day1"]  = t1
        cfg["thresholds"]["contra_day0"] = c0

        if st.button("💾 閾値を保存", key="save_thresholds"):
            save_config(cfg)
            st.success("閾値を保存しました。次回のスクリーニングから反映されます。")

    # ── 2. 自動実行スケジュール ───────────────────────────────────────────────
    with st.expander("🕐 自動実行スケジュール"):
        sched = cfg["schedule"]
        col_s1, col_s2 = st.columns([1, 2])
        sched["enabled"] = col_s1.toggle("自動実行 ON", value=bool(sched["enabled"]))
        sched["time"]    = col_s2.text_input("実行時刻（24時間 HH:MM）", value=sched["time"])
        cfg["schedule"]  = sched

        if st.button("💾 スケジュール保存", key="save_schedule"):
            save_config(cfg)
            apply_schedule(cfg)
            if sched["enabled"]:
                st.success(f"毎日 {sched['time']} に自動実行します（アプリ起動中のみ）。")
                st.info(
                    "💡 アプリを閉じている間も自動実行するには、"
                    "Windowsタスクスケジューラーで `python daily_update.py && python screening.py` "
                    "を設定してください。"
                )
            else:
                st.success("自動実行を無効にしました。")

    # ── 3. 通知設定 ──────────────────────────────────────────────────────────
    with st.expander("📬 通知設定（メール・LINE）"):
        n = cfg["notifications"]
        st.markdown("**メール通知（Gmail推奨）**")
        n["email_enabled"] = st.toggle("メール通知 ON", value=bool(n["email_enabled"]))
        nc1, nc2 = st.columns(2)
        n["email_smtp"]     = nc1.text_input("SMTPサーバー", value=n["email_smtp"])
        n["email_port"]     = int(nc2.number_input("ポート", value=int(n["email_port"]), step=1))
        n["email_user"]     = st.text_input("送信元メールアドレス", value=n["email_user"])
        n["email_password"] = st.text_input("パスワード（アプリパスワード）",
                                             value=n["email_password"], type="password")
        n["email_to"]       = st.text_input("送信先メールアドレス", value=n["email_to"])

        st.markdown("**LINE通知（LINE Notify）**")
        st.caption("LINE Notify のトークンは https://notify-bot.line.me/ で取得してください。")
        n["line_enabled"] = st.toggle("LINE通知 ON", value=bool(n["line_enabled"]))
        n["line_token"]   = st.text_input("LINE Notifyトークン", value=n["line_token"], type="password")
        cfg["notifications"] = n

        col_save, col_test = st.columns(2)
        with col_save:
            if st.button("💾 通知設定を保存", key="save_notif"):
                save_config(cfg)
                st.success("保存しました。")
        with col_test:
            if st.button("📤 テスト送信", key="test_notif"):
                save_config(cfg)
                from notifier import notify
                result = notify(cfg, [{"ticker": "7203.T", "name": "トヨタ自動車", "strategy": "trend"}],
                                [], "テスト")
                msgs = []
                if result.get("email"):
                    msgs.append("📧 メール送信成功")
                elif cfg["notifications"]["email_enabled"]:
                    msgs.append("❌ メール送信失敗（設定を確認してください）")
                if result.get("line"):
                    msgs.append("💬 LINE送信成功")
                elif cfg["notifications"]["line_enabled"]:
                    msgs.append("❌ LINE送信失敗（トークンを確認してください）")
                if msgs:
                    st.info("  ".join(msgs))
                else:
                    st.warning("通知が無効です。ONにしてから再試行してください。")

    # ── 4. データ出力（Excel）────────────────────────────────────────────────
    with st.expander("📥 Excelデータ出力"):
        st.markdown("シグナル・株価データをExcelファイルとしてダウンロードします。")

        min_date_row = conn.execute("SELECT MIN(date) FROM signals").fetchone()
        max_date_row = conn.execute("SELECT MAX(date) FROM signals").fetchone()
        min_date = min_date_row[0] if min_date_row and min_date_row[0] else "2024-01-01"
        max_date = max_date_row[0] if max_date_row and max_date_row[0] else datetime.now().strftime("%Y-%m-%d")

        export_mode = st.radio("出力範囲", ["全期間", "期間指定"], horizontal=True)
        if export_mode == "期間指定":
            ec1, ec2 = st.columns(2)
            date_from = ec1.date_input("開始日", value=datetime.strptime(min_date, "%Y-%m-%d")).strftime("%Y-%m-%d")
            date_to   = ec2.date_input("終了日", value=datetime.strptime(max_date, "%Y-%m-%d")).strftime("%Y-%m-%d")
        else:
            date_from, date_to = min_date, max_date

        if st.button("📊 Excelを生成", key="gen_excel"):
            with st.spinner("Excel生成中..."):
                excel_bytes = build_excel(conn, date_from, date_to)
            fname = f"swing_screening_{date_from}_{date_to}.xlsx"
            st.download_button(
                label="⬇️ ダウンロード",
                data=excel_bytes,
                file_name=fname,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    # ── 5. DB 状態 ────────────────────────────────────────────────────────────
    with st.expander("🗄️ データベース状態"):
        n_watch  = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
        n_prices = conn.execute("SELECT COUNT(*) FROM prices_raw").fetchone()[0]
        n_ind    = conn.execute("SELECT COUNT(*) FROM indicators").fetchone()[0]
        n_sig    = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        db_cols  = st.columns(4)
        db_cols[0].metric("watchlist", f"{n_watch:,} 銘柄")
        db_cols[1].metric("prices_raw", f"{n_prices:,} 件")
        db_cols[2].metric("indicators", f"{n_ind:,} 件")
        db_cols[3].metric("signals", f"{n_sig:,} 件")

        st.caption(f"DBパス: {DB_PATH}")


# ── メイン ────────────────────────────────────────────────────────────────────

def main():
    # DB が存在しない場合は GitHub から自動ダウンロードを試みる
    # （Streamlit Cloud の /tmp/ リセット後や初回アクセス時に対応）
    if not DB_PATH.exists() and _gh_secret("GITHUB_REPO"):
        with st.spinner("データベースを GitHub から自動取得中..."):
            auto_ok = _download_db_from_github()
        if auto_ok:
            st.cache_resource.clear()
            st.rerun()

    conn = get_conn()

    if conn is None:
        st.title("📈 スイングセンサー")
        st.error("⚠️ データベースが見つかりません。以下のボタンでダウンロードしてください。")

        if st.button("📥 GitHubからDBをダウンロード", type="primary"):
            _run_db_download_ui()
            return

        with st.expander("🔍 診断情報", expanded=False):
            for line in _diagnose():
                st.markdown(line)
        return

    # スケジューラ初期化（初回のみ）
    if "scheduler_initialized" not in st.session_state:
        cfg = load_config()
        apply_schedule(cfg)
        st.session_state["scheduler_initialized"] = True

    # パイプライン完了後の結果メッセージ＋ログを表示
    if "_pipeline_msg" in st.session_state:
        msg_type, msg_text = st.session_state.pop("_pipeline_msg")
        if msg_type == "success":
            st.success(msg_text)
        elif msg_type == "warning":
            st.warning(msg_text)
        if "_pipeline_push_log" in st.session_state:
            push_log = st.session_state.pop("_pipeline_push_log")
            with st.expander("GitHub保存エラー詳細"):
                st.code(push_log)
        if "_pipeline_log" in st.session_state:
            log_data = st.session_state.pop("_pipeline_log")
            for line in log_data.get("key1", []):
                st.info(line)
            for line in log_data.get("key2", []):
                st.info(line)
            with st.expander("daily_update.py 詳細ログ"):
                st.code(log_data.get("full1") or "(出力なし)")
            with st.expander("screening.py 詳細ログ"):
                st.code(log_data.get("full2") or "(出力なし)")

    render_header(conn)
    st.divider()

    # 手動実行処理
    run_placeholder = st.empty()
    if st.session_state.get("run_pipeline"):
        st.session_state["run_pipeline"] = False
        run_pipeline_manual(run_placeholder)

    # パイプライン完了後にキャッシュをクリアして再描画（最終更新日を更新するため）
    if st.session_state.pop("_pipeline_done", False):
        st.cache_resource.clear()
        st.rerun()

    tab_buy, tab_watch, tab_history, tab_settings = st.tabs(
        ["📋 買い推奨", "👁 監視中", "📊 履歴 & 成績", "⚙️ 設定"]
    )

    with tab_buy:
        render_buy_tab(conn)
    with tab_watch:
        render_watch_tab(conn)
    with tab_history:
        render_history_tab(conn)
    with tab_settings:
        render_settings_tab(conn)

    st.divider()
    st.caption(
        "⚠️ 本システムはバックテスト検証済みの条件に基づく参考情報です。"
        "実際の売買判断はご自身の責任で行ってください。"
        "  |  推奨損切り: 順張り −5〜6% / 逆張り −7%"
    )


if __name__ == "__main__":
    main()
