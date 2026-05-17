"""
初回 GitHub アップロード前に DB を直近4ヶ月分に圧縮するスクリプト。
init_db.py で作成した2年分のデータを削除してファイルサイズを小さくする。

実行方法（GitHub にアップロードする前に一度だけ実行）:
    python prune_db.py
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH    = Path(__file__).parent / "screening.db"
KEEP_DAYS  = 120   # 直近 120 日（約4ヶ月）を保持


def main() -> None:
    if not DB_PATH.exists():
        print("エラー: screening.db が見つかりません。init_db.py を先に実行してください。")
        return

    size_before = DB_PATH.stat().st_size / 1024 / 1024
    cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")

    print(f"圧縮前のサイズ: {size_before:.0f} MB")
    print(f"削除対象: {cutoff} より前の prices_raw / indicators")
    print("（signals テーブルは全期間保持します）")
    print()

    conn = sqlite3.connect(DB_PATH)

    pr_before = conn.execute("SELECT COUNT(*) FROM prices_raw").fetchone()[0]
    in_before = conn.execute("SELECT COUNT(*) FROM indicators").fetchone()[0]

    conn.execute("DELETE FROM prices_raw WHERE date < ?",  (cutoff,))
    conn.execute("DELETE FROM indicators WHERE date < ?",  (cutoff,))
    conn.commit()

    pr_after = conn.execute("SELECT COUNT(*) FROM prices_raw").fetchone()[0]
    in_after = conn.execute("SELECT COUNT(*) FROM indicators").fetchone()[0]

    print("VACUUM でファイルサイズを縮小中（少し時間がかかります）...")
    conn.execute("VACUUM")
    conn.close()

    size_after = DB_PATH.stat().st_size / 1024 / 1024

    print()
    print("── 圧縮結果 ──────────────────────────────")
    print(f"prices_raw : {pr_before:,} → {pr_after:,} 件")
    print(f"indicators : {in_before:,} → {in_after:,} 件")
    print(f"ファイルサイズ: {size_before:.0f} MB → {size_after:.0f} MB")
    print()
    print("✅ 完了！この screening.db を GitHub にアップロードしてください。")


if __name__ == "__main__":
    main()
