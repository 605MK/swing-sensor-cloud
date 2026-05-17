# スイングセンサー クラウド公開セットアップ手順

スマホからいつでもアクセスでき、毎日自動でスクリーニング・通知まで行うクラウド環境を  
**完全無料**で構築します。

---

## 全体の流れ（所要時間: 約2〜3時間）

| ステップ | 作業場所 | 所要時間 |
|---------|---------|---------|
| ① GitHub アカウント作成 | PC ブラウザ | 5分 |
| ② Personal Access Token 作成 | PC ブラウザ | 5分 |
| ③ リポジトリ作成 | PC ブラウザ | 5分 |
| ④ 初回 DB 作成・圧縮 | PC（ターミナル） | 30〜60分 |
| ⑤ コードと DB を GitHub にアップロード | PC | 10分 |
| ⑥ GitHub Secrets に通知設定を登録 | PC ブラウザ | 10分 |
| ⑦ Streamlit Cloud にデプロイ | PC ブラウザ | 10分 |
| ⑧ Streamlit Secrets に設定を登録 | PC ブラウザ | 5分 |
| ⑨ 動作確認 | スマホ / PC | 10分 |

---

## ステップ① GitHub アカウント作成

1. https://github.com にアクセス
2. 「Sign up」→ メールアドレス・パスワード・ユーザー名を入力
3. メール認証を完了させる

> ✅ すでにアカウントがある場合はスキップ

---

## ステップ② Personal Access Token（PAT）を作成

GitHub からコードや DB を読み書きするための「鍵」です。

1. GitHub にログイン後、右上のアイコン → **Settings**
2. 左メニュー一番下の **Developer settings**
3. **Personal access tokens** → **Tokens (classic)**
4. **Generate new token (classic)** をクリック
5. 以下のように設定:

   | 項目 | 設定値 |
   |------|--------|
   | Note | swing-sensor |
   | Expiration | No expiration（期限なし） |
   | Scopes | ✅ **repo**（これだけにチェック） |

6. **Generate token** → 表示されたトークン（`ghp_...`）を **必ずメモ帳に保存**  
   （画面を閉じると二度と表示されません！）

---

## ステップ③ リポジトリ作成

1. GitHub のトップページ右上 **＋** → **New repository**
2. 以下のように設定:

   | 項目 | 設定値 |
   |------|--------|
   | Repository name | swing-sensor（任意） |
   | Public / Private | **Public**（無料で全機能使えます） |
   | Initialize this repository | チェックしない |

3. **Create repository** をクリック
4. 表示された URL（例: `https://github.com/yamada-taro/swing-sensor`）をメモしておく

---

## ステップ④ 初回 DB 作成・圧縮（PC で実行）

```bash
# 2年分の初期データを取得（30〜60分かかります）
python init_db.py

# DB を GitHub アップロード用に4ヶ月分に圧縮
python prune_db.py
```

`prune_db.py` 実行後に「✅ 完了！」と表示されれば OK です。

---

## ステップ⑤ コードと DB を GitHub にアップロード

### GitHub Desktop を使う方法（推奨・初心者向け）

1. https://desktop.github.com からインストール
2. 起動 → **File** → **Add local repository**
3. プロジェクトフォルダ（`スイングセンサーcloud`）を選択
4. **Initialize Git** → リポジトリとして初期化
5. 左上の **Current Repository** → **Publish repository**
6. ステップ③で作ったリポジトリ名を選択して **Publish**

> ⚠️ `.gitignore` に `screening.db` が含まれているため、DB は自動的に除外されます。  
> DB は後で別の方法でアップロードします（次を参照）。

### screening.db を直接アップロード

DB は `data` ブランチに置くため、GitHub Web からアップロードします。

1. GitHub のリポジトリページへアクセス
2. ブランチ選択（`main` と表示されている部分）→ **View all branches**
3. **New branch** → ブランチ名に `data` → **Create branch**
4. `data` ブランチに切り替える
5. **Add file** → **Upload files**
6. `screening.db` をドラッグ＆ドロップ
7. **Commit changes** をクリック

> ⚠️ ファイルが大きい（50MB 超）場合は GitHub のブラウザアップロードが失敗することがあります。  
> その場合は下記のコマンドラインで実行してください:
> ```bash
> # 環境変数を設定してから実行
> set GH_TOKEN=ghp_あなたのトークン
> set GITHUB_REPO=ユーザー名/swing-sensor
> python push_db.py --push
> ```

---

## ステップ⑥ GitHub Secrets に通知設定を登録

GitHub Actions（自動実行）が使う通知設定をここで登録します。

1. GitHub のリポジトリページ → **Settings** タブ
2. 左メニュー **Secrets and variables** → **Actions**
3. **New repository secret** で以下を登録:

### 必須の Secret

| Name | Value | 説明 |
|------|-------|------|
| （なし） | — | GITHUB_TOKEN は自動で使えます |

### LINE 通知を使う場合（任意）

LINE Notify のトークンを取得:
1. https://notify-bot.line.me/ にアクセス（LINE アカウントでログイン）
2. **マイページ** → **トークンを発行する**
3. トークン名（任意）と通知先グループを選択 → **発行する**
4. 表示されたトークンをコピー

| Name | Value |
|------|-------|
| `NOTIFY_LINE_TOKEN` | 取得したトークン |

### メール通知を使う場合（任意）

Gmail を使う場合、「アプリパスワード」が必要です:
1. Google アカウント → **セキュリティ** → **2段階認証プロセス** を有効化
2. **アプリパスワード** → アプリ選択「メール」→ デバイス選択「その他」→ **生成**
3. 表示された16桁パスワードをコピー

| Name | Value |
|------|-------|
| `NOTIFY_EMAIL_ENABLED` | `true` |
| `NOTIFY_EMAIL_SMTP` | `smtp.gmail.com` |
| `NOTIFY_EMAIL_PORT` | `587` |
| `NOTIFY_EMAIL_USER` | `あなたのGmailアドレス` |
| `NOTIFY_EMAIL_PASSWORD` | 生成したアプリパスワード |
| `NOTIFY_EMAIL_TO` | 通知先メールアドレス |

---

## ステップ⑦ Streamlit Community Cloud にデプロイ

1. https://share.streamlit.io にアクセス（GitHub アカウントでサインイン）
2. **New app** をクリック
3. 以下のように設定:

   | 項目 | 設定値 |
   |------|--------|
   | Repository | `ユーザー名/swing-sensor` |
   | Branch | `main` |
   | Main file path | `app.py` |

4. **Deploy!** をクリック（数分でデプロイ完了）
5. 表示された URL（例: `https://yamada-taro-swing-sensor.streamlit.app`）をスマホにブックマーク

---

## ステップ⑧ Streamlit Secrets に設定を登録

Streamlit アプリが GitHub にアクセスするための設定です。

1. Streamlit Cloud の管理画面 → デプロイしたアプリの **⋮（︙）** → **Settings**
2. **Secrets** タブ → 以下を貼り付けて **Save**:

```toml
GITHUB_TOKEN = "ghp_ステップ②でメモしたトークン"
GITHUB_REPO  = "ユーザー名/swing-sensor"
```

LINE や メール通知を Streamlit からも使う場合は、アプリの設定タブから入力できます。

---

## ステップ⑨ 動作確認

### 手動実行テスト

1. スマホで Streamlit のURLにアクセス
2. 画面右上の **🔄 手動実行** ボタンをタップ
3. 進捗が表示され「✅ 実行完了！」と出れば成功
4. 「GitHub への保存が完了しました」と出れば DB の永続化も OK

### 自動実行の確認

翌平日の 16:00 以降に GitHub のリポジトリページで  
**Actions** タブを開き、ワークフローが実行されているか確認してください。

---

## よくある質問

**Q: スマホでアクセスできない**  
A: Streamlit Cloud の URL（`.streamlit.app` で終わるもの）にアクセスしているか確認してください。

**Q: 「GitHub への保存に失敗しました」と出る**  
A: Streamlit Secrets の `GITHUB_TOKEN` と `GITHUB_REPO` が正しく入力されているか確認してください。トークンの有効期限も確認してください。

**Q: 自動実行が動かない**  
A: GitHub の Actions タブで赤いエラーが出ていないか確認してください。  
また、初めてのリポジトリでは Actions が無効になっている場合があります:  
**Actions** タブ → **I understand my workflows, go ahead and enable them** をクリック。

**Q: DB をリセットしたい**  
A: ローカルで `init_db.py` → `prune_db.py` を実行後、`python push_db.py --push` で再アップロードしてください。

**Q: 無料期間の制限は？**  
A: GitHub・Streamlit Community Cloud ともに個人利用の範囲では無期限・無料です。  
GitHub Actions は月 2,000 分まで無料。1回の実行が約 30 分なので月 66 回まで（平日毎日で月 22 回）。

---

## 自動実行のスケジュール変更

`.github/workflows/daily_screening.yml` の `cron` の値を変更してください:

```yaml
- cron: '0 7 * * 1-5'   # UTC 07:00 = JST 16:00 月〜金
```

例: JST 15:30 に変更する場合 → UTC 06:30 → `'30 6 * * 1-5'`

---

*スイングセンサー セットアップガイド - 2026年5月版*
