# CLAUDE.md

このファイルは Claude Code がリポジトリで作業する際のガイダンスを提供します。

## プロジェクト概要

東証プライム上場銘柄（約1,575銘柄）を対象に、バックテスト検証済みの2つのスイングトレード戦略（順張り・逆張り）を毎日自動スクリーニングするシステム。

- **データソース**: yfinance（Yahoo Finance API）
- **UI**: Streamlit（Streamlit Community Cloud でホスティング済み）
- **DB永続化**: GitHub `data` ブランチに SQLite DB を保存

---

## 現在の運用形態（クラウド完結）

```
毎日 東証引け後（15:30以降）
    ↓
Streamlit アプリの「🔄 手動実行」ボタンを押す
    ↓
daily_update.py → screening.py → push_db.py（GitHub保存）
    ↓
スマホ・PCで Streamlit アプリを開いて結果を確認
```

### 手動実行（Streamlit 画面から）

1. Streamlit アプリにアクセス
2. 画面上部「🔄 手動実行」ボタンを押す
3. 進捗が表示され、完了後に「✅ GitHubへの保存が完了しました」と表示されれば正常

### /tmp/ リセットについて

Streamlit Cloud は数時間〜数日おきに `/tmp/` をリセットする。
アクセス時にデータが古い・空の場合は **自動的に GitHub から最新 DB を取得**（`main()` 冒頭で処理）。
それでもデータが古い場合は手動実行ボタンを押す。

### push_timestamp は使用しない

`push_db.py --timestamp` は main ブランチを更新するため Streamlit Cloud の**再デプロイを誘発し `/tmp/` が消える**。
`_push_db_to_github()` では `--timestamp` を呼ばない。

---

## ファイル構成と責務

| ファイル | 責務 |
|---------|------|
| `app.py` | Streamlit ダッシュボード。4タブ構成（買い推奨・監視中・履歴・設定）。DB パス解決・サブプロセス実行・GitHub push を担う |
| `daily_update.py` | 直近10日のデータを yfinance から取得し、新規日付のみ prices_raw・indicators へ upsert。120日超のデータを自動 prune |
| `screening.py` | Day0/Day1 条件を判定し signals テーブルへ書き込む。バックフィル対応（未処理日を遡って処理）。閾値は `config.json` から読み込み |
| `indicators.py` | MA/RSI/MACD/乖離率/ローソク足成分の計算。`init_db.py` と `daily_update.py` から import |
| `init_db.py` | DB スキーマ作成 + 2年分の初期データ取得。JPX サイトから tickers.csv を自動取得する機能あり |
| `push_db.py` | GitHub `data` ブランチへの DB アップロード・ダウンロード（orphan commit 方式） |
| `notifier.py` | メール（Gmail/SMTP）通知モジュール |
| `notify_cli.py` | 通知 CLI スクリプト |
| `prune_db.py` | DB を 120 日分に圧縮するスクリプト |
| `config.json` | スクリーニング閾値・通知設定・スケジュール設定（設定タブで編集・保存） |

---

## DB パスの解決ロジック（重要）

Streamlit Cloud では `/mount/src/` にコードがマウントされるが、大きなファイルは書き込めない。
すべてのスクリプトで以下の優先順位でパスを決定する：

1. 環境変数 `SCREENING_DB_PATH` が設定されていればそれを使用
2. スクリプトの親ディレクトリが `/mount/src` で始まるなら `/tmp/screening.db`
3. それ以外はスクリプトと同じディレクトリの `screening.db`

```python
_env_db = os.environ.get("SCREENING_DB_PATH", "")
DB_PATH = Path(_env_db) if _env_db else (
    Path("/tmp/screening.db") if str(BASE_DIR).startswith("/mount/src")
    else BASE_DIR / "screening.db"
)
```

`app.py` はサブプロセスを呼び出す際に必ず `SCREENING_DB_PATH=str(DB_PATH)` を環境変数として渡す（`_subprocess_env()` 関数）。`daily_update.py`・`screening.py`・`push_db.py` の全スクリプトに実装済み。

---

## DB スキーマ（screening.db / SQLite）

- `watchlist` — ticker, name, market_code（初回のみ更新）
- `prices_raw` — ticker, date, open, high, low, close, volume
- `indicators` — ticker, date, ma5/25/75, rsi14, macd/signal/hist, volume_ratio25, kairi25, body_pct, upper/lower_shadow, n225_close/chg
- `signals` — ticker, date, strategy('trend'/'contra'), signal_type('watch'/'buy'), entry_date, entry_price, notes(JSON)

**注意**: `indicators` テーブルに `close` 列はない。終値が必要なクエリは `prices_raw` を JOIN すること。

`prices_raw` / `indicators` は直近 120 日のみ保持（`daily_update.py` が自動 prune）。
`signals` は全期間保持（履歴・パフォーマンス分析のため削除しない）。

---

## 戦略の判定ロジック（screening.py）

### シグナル生成の基本ルール

- **Day0 → Day1 は連続した営業日でなければならない**
- `screening.py` は「直前の営業日の watch」のみを Day1 チェックする
- Day1 不通過の watch シグナルは翌日以降チェックされない
- 同じ銘柄が後日また Day0 条件を満たせば、新しいシグナルとして監視中に入る

### バックフィル（遡り処理）

`get_dates_to_screen()` が LEFT JOIN で「indicators にあるが signals にない最古の日付」を検出し、
その日以降の全 indicators 日を処理する。これにより数日サボって実行しても欠けた日が補完される。

```python
# indicators にあるが signals にない最古日を探す
SELECT MIN(i.date) FROM (SELECT DISTINCT date FROM indicators) i
LEFT JOIN (SELECT DISTINCT date FROM signals) s ON s.date = i.date
WHERE s.date IS NULL
```

- 処理済み日付は `INSERT OR IGNORE` で安全にスキップ
- 既に一部スクリーニング済みの日も含めて再処理する（Day1 の取りこぼし防止）
- 自動実行時は通常1日分のみ新規処理されるため動作変化なし

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

### signals テーブルへの書き込み

1. 当日の全銘柄に Day0 チェック → 通過で `signal_type='watch'` を INSERT（`INSERT OR IGNORE`）
2. 前日の `watch` シグナルに当日データで Day1 チェック → 通過で `signal_type='buy'` を INSERT
3. `entry_date` = 翌営業日（土日スキップ）
4. `entry_price` は null（翌朝寄付き価格は未確定）

### 閾値の変更方法

- Streamlit の「⚙️ 設定」タブ → 「スクリーニング条件の閾値」から UI で変更・保存
- `config.json` に保存され、次回スクリーニングから反映される
- デフォルト値は `screening.py` の `DEFAULT_THRESHOLDS` に定義

---

## 監視中タブの表示ルール

以下の条件を**すべて満たす** watch シグナルのみ表示する：

1. `entry_date > 今日` → Day1 チェック期限が未来のもの（期限切れ・チェック済みは非表示）
2. 同じ ticker・strategy の `buy` シグナルが後日付で存在しない（昇格済みは非表示）

これにより「Day1 不通過で期限切れになった watch」と「buy に昇格済みの watch」が自動的に除外される。

---

## 買い推奨タブの表示ルール

`MAX(date) FROM indicators`（最新スクリーニング日）を基準に当日の buy シグナルを表示。
`MAX(date) FROM signals WHERE signal_type='buy'` は使わない（buy ゼロの日を飛ばして過去日を返すため）。

---

## 履歴タブ（カレンダーUI）

### 画面構成

1. **年月ナビゲーション**：◀前月 / 翌月▶ ボタン + 戦略フィルタ
2. **通算成績**：月間・年間を2列で並べて表示
   - シグナル数・勝率(5日後) + 3/5/10日後リターンの平均・中央値テーブル
3. **カレンダー**：日〜土（土日は列幅狭め）
   - シグナルあり日：「買N 監N」ボタン → クリックで詳細展開
   - 未実行日（indicators あり・signals なし）：「未実行」グレー表示
   - 休場日（weekday だが indicators データなし）：「休場」表示
   - 上記以外（データなし）：日付のみ
4. **詳細パネル**（日付クリック時）：買い推奨・監視の一覧、ティッカー→株探リンク
5. **期間シグナル一覧**：月間・年間の折りたたみ。買い推奨は3/5/10日後リターン＋日経変化率付き

### 休場判定のロジック

```python
# indicators の日付範囲内の平日で indicators データがない日 = 休場
if weekday and min_ind_date <= date <= max_ind_date and date not in ind_dates:
    holidays.add(date)
```

yfinance が祝日にデータを返さない性質を利用した自動判定。

### パフォーマンス計算（calc_performance）

買いシグナルについて3・5・10営業日後のリターンと日経比較を計算する。

```python
# 日経終値は indicators テーブルから取得（銘柄共通）
def _n225_close_at(conn, date): ...

# N日後リターン = (終値N日後 - エントリー価格) / エントリー価格 * 100
# 日経比較 = (日経N日後 - 日経エントリー日) / 日経エントリー日 * 100
```

戦略フィルタはカレンダー件数・サマリー・シグナル一覧すべてに連動する。

---

## ログのタイムゾーン（JST）

`daily_update.py` と `screening.py` は `_JSTFormatter` クラスで JST（UTC+9）のタイムスタンプを出力する。
Streamlit Cloud サーバーは UTC で動作するが、ログには日本時間が表示される。

```python
class _JSTFormatter(logging.Formatter):
    _JST = timezone(timedelta(hours=9))
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=self._JST)
        return dt.strftime(datefmt or "%H:%M:%S")
```

---

## yfinance 取得仕様と互換性対応

- ティッカー形式: `"7203.T"`（.T サフィックス必須）
- バッチサイズ: 100銘柄ずつ、`sleep(1.5)` 挟む
- 複数銘柄一括: `yf.download(batch, auto_adjust=False)`（`group_by='ticker'` は不安定なため使用しない）
- 日経平均: `"^N225"` で取得
- **`end` パラメータは排他的**（指定日を含まない）。当日データを取得するには `end = today + timedelta(days=1)` とする

### yfinance ≥0.2.38 の MultiIndex 変更への対応

新バージョンでは MultiIndex の順序が `(ticker, price_type)` から `(price_type, ticker)` に変わった。
また DatetimeIndex の `name` が `None` になり `reset_index()` で列名が `"index"` になる。

```python
# _download_batch: lv0/lv1 どちらにティッカーがあるか自動判定
if ticker in lv0:
    df = raw[ticker].copy()          # 旧形式
elif ticker in lv1:
    df = raw.xs(ticker, level=1, axis=1).copy()  # 新形式

# process_ticker / fetch_n225: date列をフォールバック付きで検索
date_col = next(
    (c for c in df.columns if "date" in str(c).lower()),
    df.columns[0],   # index.name=None の場合 reset_index で "index" 列になる
)
```

---

## GitHub 連携（push_db.py）

### data ブランチへの DB 保存

孤立コミット（orphan commit）方式で履歴を持たずに force push する。
これによりリポジトリサイズが肥大化しない（常に最新 DB 1件のみ）。

```
1. blob 作成（DB ファイルの base64）
2. tree 作成
3. commit 作成（parents=[] で孤立コミット）
4. data ブランチの ref を force 更新
```

### Streamlit Secrets の設定

Streamlit Cloud ダッシュボード → App Settings → Secrets に以下を設定：

```toml
GITHUB_TOKEN = "ghp_..."          # Personal Access Token（repo スコープ）
GITHUB_REPO  = "605MK/swing-sensor-cloud"
SCREENING_DB_PATH = "/tmp/screening.db"
```

`_gh_secret(key)` 関数が `st.secrets` → `os.environ` の順で値を検索する。

---

## app.py 主要関数一覧

| 関数 | 役割 |
|------|------|
| `_resolve_db_path()` | DB パス解決 |
| `_gh_secret(key)` | Streamlit secrets / 環境変数から認証情報を取得 |
| `_download_db_from_github()` | GitHub data ブランチから DB をダウンロード |
| `_push_db_to_github()` | DB を GitHub へアップロード（--timestamp は呼ばない） |
| `_subprocess_env()` | サブプロセス用環境変数（SCREENING_DB_PATH を含む） |
| `run_pipeline_manual(placeholder)` | 手動実行パイプライン。ログを session_state に保存し rerun 後に表示 |
| `nth_trading_day_after(conn, ticker, from_date, n)` | 翌 n 営業日の日付を prices_raw から取得 |
| `_n225_close_at(conn, date)` | 指定日の日経終値を indicators から取得 |
| `calc_performance(conn, year, month, strategy_filter)` | 買いシグナルの 3/5/10 日後リターン＋日経比較を計算 |
| `render_header(conn)` | ヘッダー（最終更新日・買い推奨件数・監視中件数・手動実行ボタン） |
| `render_buy_tab(conn)` | 買い推奨タブ。`MAX(date) FROM indicators` を基準日に使用 |
| `render_watch_tab(conn)` | 監視中タブ |
| `_render_day_detail(conn, date_str)` | カレンダー日付クリック時のシグナル詳細（3/5/10日後リターン付き） |
| `_perf_metrics_block(df, label)` | 通算成績ブロック（コンパクト：シグナル数・勝率＋リターンテーブル） |
| `_signal_list_expander(conn, year, month, ...)` | 期間シグナル一覧折りたたみ（buy_df を受け取り再計算省略） |
| `render_history_tab(conn)` | 履歴タブ（カレンダーUI・通算成績・シグナル一覧） |
| `render_settings_tab(conn)` | 設定タブ（閾値・通知・スケジュール） |

---

## 指標の計算方式について

RSI・MACD の数値が Yahoo Finance Japan や株探と異なる場合があるが、**これは仕様**。

- **RSI**: EWM（指数平滑）の初期値の与え方が外部サービスと異なる
- **MACD**: EWM の計算開始点（使用データ期間）が異なる

バックテストも同じ計算式で行っているため、閾値はこのシステム独自の数値に対して最適化されており、外部サービスとの数値一致は不要。

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

# GitHub へ DB をアップロード
python push_db.py --push

# DB を GitHub から取得
python push_db.py --pull
```

---

## 既知の制限

| 項目 | 内容 |
|------|------|
| signals の「スクリーニング済み0件」 | screening 実行後 0 シグナルだった日は signals に行が存在しない。カレンダーでは「未実行」と同じ表示になる（区別不可） |
| indicators の保持期間 | 直近 120 日のみ。それより古い月は休場判定・未実行判定が動作しない |
| entry_price | 常に NULL。エントリー価格は entry_date の prices_raw.open で代用 |
| リターン計算 | prices_raw に N 日分のデータがない場合（直近シグナル等）は "-" 表示 |
