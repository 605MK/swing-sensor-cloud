# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

東証プライム上場銘柄（約1,575銘柄）を対象に、バックテスト検証済みの2つのスイングトレード戦略（順張り・逆張り）を毎日自動スクリーニングするシステム。

- **データソース**: yfinance（Yahoo Finance API）
- **UI**: Streamlit（Streamlit Community Cloud でホスティング済み）
- **DB永続化**: GitHub `data` ブランチに SQLite DB を保存
- **自動実行**: GitHub Actions（平日 JST 16:00）

---

## 現在の運用形態（クラウド完結）

```
毎日 16:00（JST）
    ↓
GitHub Actions（PCなしで自動起動）
    ↓
data ブランチから screening.db をダウンロード
    ↓
daily_update.py → screening.py → notify_cli.py
    ↓
更新済み screening.db を data ブランチへ保存
    ↓
スマホで Streamlit アプリを開く
→「📥 GitHubからDBをダウンロード」を押す
→ 最新スクリーニング結果を確認
```

### 手動実行（スマホから）

1. Streamlit アプリにアクセス
2. 画面上部「🔄 手動実行」ボタンを押す
3. 進捗が表示され、完了後に GitHub へ DB が自動保存される

### /tmp/ リセットについて

Streamlit Cloud は数時間〜数日おきに `/tmp/` をリセットする。
アクセス時に「データベースが見つかりません」と表示された場合は
「📥 GitHubからDBをダウンロード」ボタンを押せば復旧する。

---

## ファイル構成と責務

| ファイル | 責務 |
|---------|------|
| `indicators.py` | MA/RSI/MACD/乖離率/ローソク足成分の計算。`init_db.py` と `daily_update.py` の両方から import |
| `init_db.py` | DB スキーマ作成 + 2年分の初期データ取得。JPX サイトから tickers.csv を自動取得する機能あり |
| `daily_update.py` | 直近5営業日の差分取得。新規日付のみ prices_raw・indicators へ upsert。実行後に prices_raw/indicators を 120 日より古いデータを自動削除（prune） |
| `screening.py` | Day0/Day1 条件を判定し signals テーブルへ書き込む。閾値は `config.json` から読み込み |
| `app.py` | Streamlit ダッシュボード。4タブ構成（買い推奨・監視中・履歴・設定） |
| `push_db.py` | GitHub data ブランチへの DB アップロード・ダウンロード |
| `notifier.py` | メール（Gmail/SMTP）通知モジュール |
| `notify_cli.py` | GitHub Actions から呼ばれる通知 CLI スクリプト |
| `prune_db.py` | 初回 GitHub アップロード前に DB を 120 日分に圧縮する一回限りのスクリプト |
| `config.json` | スクリーニング閾値・通知設定・スケジュール設定（設定タブで編集・保存） |
| `.github/workflows/daily_screening.yml` | GitHub Actions 自動実行ワークフロー |

---

## DB パスの解決ロジック

**重要**: Streamlit Cloud では `/mount/src/` にコードがマウントされるが、
大きなファイルは書き込めない。すべてのスクリプトで以下のパターンを使う。

```python
def _resolve_db_path() -> Path:
    base = Path(__file__).parent
    if str(base).startswith("/mount/src"):   # Streamlit Cloud を検出
        return Path("/tmp/screening.db")
    default = base / "screening.db"
    try:
        test = default.parent / ".wtest"
        test.touch()
        test.unlink()
        return default                       # ローカル環境
    except OSError:
        return Path("/tmp/screening.db")     # その他の読み取り専用環境
```

`app.py` / `daily_update.py` / `screening.py` の全スクリプトに実装済み。
環境変数 `SCREENING_DB_PATH` が設定されている場合はそちらを優先する。

---

## DB スキーマ（screening.db / SQLite）

- `watchlist` — ticker, name, market_code（初回のみ更新）
- `prices_raw` — ticker, date, open, high, low, close, volume
- `indicators` — ticker, date, ma5/25/75, rsi14, macd/signal/hist, volume_ratio25, kairi25, body_pct, upper/lower_shadow, n225_close/chg
- `signals` — ticker, date, strategy('trend'/'contra'), signal_type('watch'/'buy'), entry_date, entry_price, notes(JSON)

`indicators` テーブルに `close` 列は**ない**。終値が必要なクエリは `prices_raw` を JOIN すること。

`prices_raw` / `indicators` は直近 120 日のみ保持（`daily_update.py` が自動 prune）。
`signals` は全期間保持（履歴・パフォーマンス分析のため削除しない）。

---

## 戦略の判定ロジック（screening.py）

### 順張り戦略（strategy='trend'）

**Day0（全8条件 AND）**
- `close > ma5 > ma25 > ma75`
- `body_pct`: 4〜8%
- `upper_shadow` < 2%
- `lower_shadow` > 0.5%
- `volume_ratio25`: 1.5〜4.0
- `kairi25`: +2〜+7%
- `rsi14`: 52〜70
- `macd_hist` > 0 かつ 前日比改善

**Day1（全3条件 AND）**
- `low` >= Day0の安値
- `volume_ratio25` > 1.2
- `upper_shadow` < 3%

### 逆張り戦略（strategy='contra'）

**Day0（全6条件 AND）**
- `kairi25` < −10%
- `rsi14` < 40
- `lower_shadow` > 1%
- `volume_ratio25` > 1.2
- `macd_hist` 前日比改善（値がマイナスでも可）
- `n225_chg` < 0（日経平均が当日下落）

**Day1（1条件）**
- `body_pct` > 0（陽線）

### シグナル生成の流れ

1. 当日の全銘柄に Day0 チェック → `signal_type='watch'` で signals へ INSERT
2. 前日の `watch` シグナルに対して当日データで Day1 チェック → 通過で `signal_type='buy'` を INSERT
3. `entry_date` = 翌営業日（土日スキップ）、`entry_price` は null（翌朝寄付き価格は未確定）

### 閾値の変更方法

- Streamlit の「⚙️ 設定」タブ → 「スクリーニング条件の閾値」から UI で変更・保存
- `config.json` に保存され、次回スクリーニングから反映される
- デフォルト値は `screening.py` の `DEFAULT_THRESHOLDS` に定義

---

## GitHub 連携（push_db.py）

### data ブランチへの DB 保存

孤立コミット（orphan commit）方式で履歴を持たずに force push する。
これによりリポジトリサイズが肥大化しない（常に最新 DB 1件のみ）。

```python
# 4ステップ: blob作成 → tree作成 → commit作成（parents=[]）→ ref更新
POST /repos/{owner}/{repo}/git/blobs
POST /repos/{owner}/{repo}/git/trees
POST /repos/{owner}/{repo}/git/commits   # parents=[] で孤立コミット
PATCH /repos/{owner}/{repo}/git/refs/heads/data  # force=true
```

### Streamlit Secrets の設定

```toml
GITHUB_TOKEN = "ghp_..."          # Personal Access Token（repo + workflow スコープ）
GITHUB_REPO  = "ユーザー名/swing-sensor"
```

`st.secrets` から読む（`os.environ` では読めない点に注意）。
`_gh_secret(key)` 関数が `st.secrets` → `os.environ` の順で検索する。

---

## GitHub Actions（.github/workflows/daily_screening.yml）

- **スケジュール**: `cron: '0 7 * * 1-5'`（UTC 07:00 = JST 16:00 月〜金）
- **手動トリガー**: `workflow_dispatch`（GitHub Actions 画面から）
- **実行ステップ**: pull DB → daily_update.py → screening.py → notify_cli.py → push DB → push タイムスタンプ
- **無料枠**: 月2,000分。1回30分として月66回まで（平日毎日で月約22回）

---

## 通知設定（GitHub Secrets）

| Secret名 | 内容 |
|----------|------|
| `NOTIFY_EMAIL_ENABLED` | `true` |
| `NOTIFY_EMAIL_SMTP` | `smtp.gmail.com` |
| `NOTIFY_EMAIL_PORT` | `587` |
| `NOTIFY_EMAIL_USER` | 送信元 Gmail アドレス |
| `NOTIFY_EMAIL_PASSWORD` | Gmail アプリパスワード（16桁）|
| `NOTIFY_EMAIL_TO` | 受信先メールアドレス |

LINE Notify は 2025年3月にサービス終了済み。メールのみ使用可能。

---

## yfinance 取得仕様

- ティッカー形式: `"7203.T"`（.T サフィックス必須）
- バッチサイズ: 100銘柄ずつ、`sleep(1.5)` 挟む
- 複数銘柄一括: `yf.download(batch, group_by='ticker', auto_adjust=False)`
  - 戻り値は MultiIndex DataFrame。`raw[ticker]` でアクセス
  - 銘柄が1件の場合は MultiIndex にならないため分岐が必要
- 日経平均: `"^N225"` で取得

---

## バックテスト結果（参考）

- 順張り: 推奨保有3〜8日、損切り −5〜6%
- 逆張り: 推奨保有8〜10日、損切り −7%
- 検証期間: 2025年1月〜2026年5月、東証プライム全銘柄

---

## ローカル開発時のコマンド

```bash
# 初回セットアップ（一度だけ）
pip install -r requirements.txt
python init_db.py        # 2年分データ取得・DB構築（30〜60分）

# 毎日の更新（東証引け後 15:30以降）
python daily_update.py   # 差分データ取得 + 指標計算
python screening.py      # シグナル判定（watch/buy 生成）
streamlit run app.py     # ダッシュボード起動

# GitHub へ DB をアップロード（初回または手動）
python push_db.py --push

# DB を GitHub から取得
python push_db.py --pull

# DB を圧縮（初回アップロード前）
python prune_db.py
```
