"""
GitHub の data ブランチへ screening.db を保存・取得するユーティリティ。

【仕組み】
  Git Blobs API で orphan commit（親なし）を force-push する。
  ブランチ履歴が常に1コミットになるため、リポジトリサイズが膨らまない。

使い方:
    python push_db.py --push   # DB を GitHub へ保存
    python push_db.py --pull   # DB を GitHub から取得
"""

import argparse
import base64
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

BASE_DIR    = Path(__file__).parent
DB_PATH     = BASE_DIR / "screening.db"
DB_BRANCH   = "data"
DB_FILENAME = "screening.db"
TIMESTAMP_FILE = ".last_screening"


# ── 認証情報取得 ──────────────────────────────────────────────────────────────

def get_credentials() -> tuple[str, str]:
    """トークンとリポジトリ名を環境変数 / Streamlit secrets から取得"""
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    repo  = os.getenv("GITHUB_REPO", "")

    if not token or not repo:
        try:
            import streamlit as st
            token = token or st.secrets.get("GITHUB_TOKEN", "")
            repo  = repo  or st.secrets.get("GITHUB_REPO", "")
        except Exception:
            pass

    return token.strip(), repo.strip()


# ── DB ダウンロード ───────────────────────────────────────────────────────────

def pull_db(token: str, repo: str, dest: Path = DB_PATH) -> bool:
    """GitHub の data ブランチから DB をダウンロードする"""
    owner, repo_name = repo.split("/", 1)
    url = (
        f"https://raw.githubusercontent.com/{owner}/{repo_name}"
        f"/{DB_BRANCH}/{DB_FILENAME}"
    )
    headers = {"Authorization": f"token {token}"} if token else {}

    print(f"[pull] ダウンロード中: {url}")
    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=180)
        if resp.status_code == 404:
            print("[pull] data ブランチに DB がありません（初回実行の場合は正常）")
            return False
        if resp.status_code != 200:
            print(f"[pull] 失敗: HTTP {resp.status_code}")
            return False

        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)

        size_mb = dest.stat().st_size / 1024 / 1024
        print(f"[pull] 完了: {size_mb:.1f} MB")
        return True
    except Exception as e:
        print(f"[pull] エラー: {e}")
        return False


# ── DB アップロード ───────────────────────────────────────────────────────────

def push_db(token: str, repo: str, src: Path = DB_PATH) -> bool:
    """DB を GitHub の data ブランチへ orphan commit で force-push する"""
    owner, repo_name = repo.split("/", 1)
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    base_url = f"https://api.github.com/repos/{owner}/{repo_name}"
    size_mb  = src.stat().st_size / 1024 / 1024
    print(f"[push] DB を GitHub へ保存中 ({size_mb:.1f} MB)...")

    # ── Step 1: blob 作成 ────────────────────────────────────────────────────
    print("  [1/4] blob 作成中（大きいファイルは数分かかります）...")
    with open(src, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()

    for attempt in range(3):
        try:
            r = requests.post(
                f"{base_url}/git/blobs",
                headers=headers,
                json={"content": content_b64, "encoding": "base64"},
                timeout=600,
            )
            if r.status_code in (200, 201):
                break
            print(f"  blob 作成リトライ {attempt+1}/3: HTTP {r.status_code}")
            time.sleep(5)
        except requests.Timeout:
            print(f"  blob 作成タイムアウト、リトライ {attempt+1}/3...")
            time.sleep(10)
    else:
        print("[push] blob 作成に失敗しました")
        return False

    blob_sha = r.json()["sha"]

    # ── Step 2: tree 作成（DB ファイル1つだけ）───────────────────────────────
    print("  [2/4] tree 作成中...")
    r = requests.post(
        f"{base_url}/git/trees",
        headers=headers,
        json={"tree": [{"path": DB_FILENAME, "mode": "100644",
                         "type": "blob", "sha": blob_sha}]},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        print(f"[push] tree 作成失敗: HTTP {r.status_code}")
        return False
    tree_sha = r.json()["sha"]

    # ── Step 3: orphan commit 作成（parents=[] で履歴なし）───────────────────
    print("  [3/4] commit 作成中...")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M JST")
    r = requests.post(
        f"{base_url}/git/commits",
        headers=headers,
        json={
            "message": f"DB update {now_str}",
            "tree": tree_sha,
            "parents": [],  # 履歴なし → ブランチ常に1コミット
        },
        timeout=30,
    )
    if r.status_code not in (200, 201):
        print(f"[push] commit 作成失敗: HTTP {r.status_code}")
        return False
    commit_sha = r.json()["sha"]

    # ── Step 4: data ブランチを force-update ─────────────────────────────────
    print("  [4/4] data ブランチを更新中...")
    ref_check = requests.get(
        f"{base_url}/git/ref/heads/{DB_BRANCH}", headers=headers, timeout=10
    )
    if ref_check.status_code == 200:
        r = requests.patch(
            f"{base_url}/git/refs/heads/{DB_BRANCH}",
            headers=headers,
            json={"sha": commit_sha, "force": True},
            timeout=30,
        )
    else:
        r = requests.post(
            f"{base_url}/git/refs",
            headers=headers,
            json={"ref": f"refs/heads/{DB_BRANCH}", "sha": commit_sha},
            timeout=30,
        )

    if r.status_code not in (200, 201):
        print(f"[push] ref 更新失敗: HTTP {r.status_code}")
        return False

    print(f"[push] 完了 ({now_str})")
    return True


# ── Streamlit 再デプロイ用タイムスタンプ ────────────────────────────────────

def push_timestamp(token: str, repo: str) -> bool:
    """main ブランチの .last_screening を更新して Streamlit 再デプロイを誘発する"""
    owner, repo_name = repo.split("/", 1)
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    base_url = f"https://api.github.com/repos/{owner}/{repo_name}"
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M JST\n")
    content_b64 = base64.b64encode(now_str.encode()).decode()

    get_r = requests.get(
        f"{base_url}/contents/{TIMESTAMP_FILE}", headers=headers, timeout=10
    )
    sha = get_r.json().get("sha") if get_r.status_code == 200 else None

    payload: dict = {
        "message": f"screening update {now_str.strip()}",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(
        f"{base_url}/contents/{TIMESTAMP_FILE}",
        headers=headers, json=payload, timeout=30,
    )
    ok = r.status_code in (200, 201)
    if ok:
        print("[timestamp] Streamlit 再デプロイ用タイムスタンプを更新しました")
    else:
        print(f"[timestamp] 更新失敗: HTTP {r.status_code}")
    return ok


# ── CLI エントリーポイント ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GitHub data ブランチへ DB を保存/取得")
    parser.add_argument("--push", action="store_true", help="DB を GitHub へ保存")
    parser.add_argument("--pull", action="store_true", help="DB を GitHub から取得")
    parser.add_argument("--timestamp", action="store_true",
                        help="Streamlit 再デプロイ用タイムスタンプを更新")
    args = parser.parse_args()

    token, repo = get_credentials()
    if not token or not repo:
        print("エラー: GH_TOKEN (または GITHUB_TOKEN) と GITHUB_REPO を設定してください")
        sys.exit(1)

    if args.push:
        sys.exit(0 if push_db(token, repo) else 1)
    elif args.pull:
        sys.exit(0 if pull_db(token, repo) else 1)
    elif args.timestamp:
        sys.exit(0 if push_timestamp(token, repo) else 1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
