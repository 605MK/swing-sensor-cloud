# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

東証プライム上場銘柄（約1,575銘柄）を対象に、バックテスト検証済みの2つのスイングトレード戦略（順張り・逆張り）を毎日自動スクリーニングするシステム。データソースは yfinance（Yahoo Finance API）。

## 毎日の実行コマンド（東証引け後 15:30以降）

```bash
python daily_update.py   # 差分データ取得 + 指標計算
python screening.py      # シグナル判定（watch/buy 生成）
streamlit run app.py     # ダッシュボード起動
```

## 初回セットアップ（一度だけ）

```bash
pip install -r requirements.txt
python init_db.py        # 2年分データ取得・DB構築（30〜60分）
```

## アーキテクチャ

### データフロー

```
yfinance API
    │
    ▼
daily_update.py ──→ prices_raw テーブル
    │               indicators テーブル（指標計算済み）
    │
    ▼
screening.py ──→ signals テーブル（watch / buy）
    │
    ▼
app.py（Streamlit）──→ ブラウザ表示
```

### ファイル構成と責務

| ファイル | 責務 |
|---------|------|
| `indicators.py` | MA/RSI/MACD/乖離率/ローソク足成分の計算。`init_db.py` と `daily_update.py` の両方から import |
| `init_db.py` | DB スキーマ作成 + 2年分の初期データ取得。JPX サイトから tickers.csv を自動取得する機能あり |
| `daily_update.py` | 直近5営業日の差分取得。新規日付のみ prices_raw・indicators へ upsert |
| `screening.py` | Day0/Day1 条件を判定し signals テーブルへ書き込む |
| `app.py` | Streamlit ダッシュボード。4タブ構成（買い推奨・監視中・履歴・設定） |

### DB スキーマ（screening.db / SQLite）

- `watchlist` — ticker, name, market_code（初回のみ更新）
- `prices_raw` — ticker, date, open, high, low, close, volume
- `indicators` — ticker, date, ma5/25/75, rsi14, macd/signal/hist, volume_ratio25, kairi25, body_pct, upper/lower_shadow, n225_close/chg
- `signals` — ticker, date, strategy('trend'/'contra'), signal_type('watch'/'buy'), entry_date, entry_price, notes(JSON)

`indicators` テーブルに `close` 列は**ない**。終値が必要なクエリは `prices_raw` を JOIN すること。

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

## yfinance 取得仕様

- ティッカー形式: `"7203.T"`（.T サフィックス必須）
- バッチサイズ: 100銘柄ずつ、`sleep(1.5)` 挟む
- 複数銘柄一括: `yf.download(batch, group_by='ticker', auto_adjust=False)`
  - 戻り値は MultiIndex DataFrame。`raw[ticker]` でアクセス
  - 銘柄が1件の場合は MultiIndex にならないため分岐が必要
- 日経平均: `"^N225"` で取得

## 閾値の変更方法

`screening.py` 内の `check_trend_day0` / `check_trend_day1` / `check_contra_day0` / `check_contra_day1` 関数の数値を直接変更する。変更後は `python screening.py` を再実行。

## バックテスト結果（参考）

- 順張り: 推奨保有3〜8日、損切り −5〜6%
- 逆張り: 推奨保有8〜10日、損切り −7%
- 検証期間: 2025年1月〜2026年5月、東証プライム全銘柄
