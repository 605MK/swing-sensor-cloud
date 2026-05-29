"""
GitHub Actions のステップから LINE / メール通知を送る CLI スクリプト。
設定は環境変数から読み込む（GitHub Secrets で管理）。

環境変数:
    NOTIFY_LINE_TOKEN        LINE Notify トークン（設定すれば LINE 通知 ON）
    NOTIFY_EMAIL_ENABLED     "true" で有効（デフォルト false）
    NOTIFY_EMAIL_SMTP        SMTP サーバー（デフォルト smtp.gmail.com）
    NOTIFY_EMAIL_PORT        ポート番号（デフォルト 587）
    NOTIFY_EMAIL_USER        送信元メールアドレス
    NOTIFY_EMAIL_PASSWORD    メールパスワード（アプリパスワード）
    NOTIFY_EMAIL_TO          送信先メールアドレス
"""

import os
import sqlite3
import sys
from pathlib import Path

from notifier import notify

DB_PATH = Path(__file__).parent / "screening.db"


def main() -> None:
    if not DB_PATH.exists():
        print("DB が見つかりません。スキップします。")
        sys.exit(0)

    conn = sqlite3.connect(DB_PATH)
    # 最新スクリーニング日を基準にする（アプリの買い推奨タブと同じ）。
    # MAX(buy date) だと買い0件の日が飛ばされ過去日の内容を誤送信するため使わない。
    today = (
        conn.execute(
            "SELECT MAX(date) FROM indicators"
        ).fetchone()[0] or ""
    )

    buy_rows = conn.execute(
        """
        SELECT s.ticker, w.name, s.strategy
        FROM signals s
        LEFT JOIN watchlist w ON w.ticker = s.ticker
        WHERE s.date = ? AND s.signal_type = 'buy'
        """,
        (today,),
    ).fetchall()

    watch_count = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE date = ? AND signal_type = 'watch'",
        (today,),
    ).fetchone()[0]

    conn.close()

    cfg = {
        "notifications": {
            "email_enabled": os.getenv("NOTIFY_EMAIL_ENABLED", "false").lower() == "true",
            "email_smtp":    os.getenv("NOTIFY_EMAIL_SMTP", "smtp.gmail.com"),
            "email_port":    int(os.getenv("NOTIFY_EMAIL_PORT", "587")),
            "email_user":    os.getenv("NOTIFY_EMAIL_USER", ""),
            "email_password":os.getenv("NOTIFY_EMAIL_PASSWORD", ""),
            "email_to":      os.getenv("NOTIFY_EMAIL_TO", ""),
            "line_enabled":  bool(os.getenv("NOTIFY_LINE_TOKEN", "")),
            "line_token":    os.getenv("NOTIFY_LINE_TOKEN", ""),
        }
    }

    buy_list   = [{"ticker": r[0], "name": r[1], "strategy": r[2]} for r in buy_rows]
    watch_list = [{}] * watch_count

    if not cfg["notifications"]["email_enabled"] and not cfg["notifications"]["line_enabled"]:
        print("通知設定なし。スキップします。")
        sys.exit(0)

    result = notify(cfg, buy_list, watch_list, today)
    print(f"通知結果: email={result.get('email')}, line={result.get('line')}")


if __name__ == "__main__":
    main()
