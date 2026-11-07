# toyoko_watch

東横イン公式サイト (https://www.toyoko-inn.com/) の空室を10分ごとに調べて、空きが出たら GitHub の Issue で知らせる仕組みです。
**予約はしません。** 通知を見たら、自分で公式サイトから予約してください。

いま見ている日程・エリア（`config/target.json` の `targets`）:

| id | エリア | 日程（1泊） | 対象のホテル | 監視期限 |
|---|---|---|---|---|
| `shizuoka` | 静岡駅前 | 2026-12-26 → 12-27 | 名前に「静岡駅」を含む東横イン（「東静岡」は除く） | 12/26 20:00 |

| 項目 | 内容 |
|---|---|
| 人数 | どれも1室・大人2名（ツイン・ダブルなど、2名で泊まれる部屋ならどれでも。禁煙・喫煙どちらも。喫煙ルームは通知に「（喫煙）」と出る） |
| 確認の間隔 | 10分ごと（GitHub Actions。1回の実行で、期限内の日程・エリアを順に調べる） |
| 通知 | 空きが出たら Issue を作成し、あなたに割り当てる（GitHub の通知・メール・スマホアプリに届く）。Issue には日程・エリアごとのラベル `toyoko-<id>` が付く |
| 監視期限 | 日程・エリアごとに `stop_watching_at` を過ぎたら、その分は調べない。すべて過ぎたら自動で停止 |

日程・エリアを増やすときは、`config/target.json` の `targets` に1つ足します（`id` / `label` / `checkin` / `checkout` / `rooms` / `adults` / `hotel_keywords` / `exclude_keywords`（任意）/ `area_hints` / `stop_watching_at`）。

## 仕組み

```
GitHub Actions（10分ごと） .github/workflows/toyoko-watch.yml
  └ scripts/watch_vacancy.py
      1. 監視期限を過ぎた日程・エリアは飛ばす（すべて過ぎていたら、ワークフローを無効化して終了）
      2. 空室チェック … .claude/agents/toyoko-vacancy-checker.md（Claude Haiku・読み取り専用）
                     公式サイトで日程・人数・地名を入れて検索し、対象ホテルの部屋ごとの空き・料金を読む
      3. 日付・泊数・人数が条件どおりか、スクリプト側でもう一度確かめる
      4. 空きが出たら Issue「[東横イン] 空室あり: ...」を作る
         ・Issue が開いているあいだに別のホテルも空いたら、コメントで追記
         ・全館満室に戻ったら、コメントして Issue を閉じる（また空いたら新しい Issue）
```

- 空室チェックは Claude Code をヘッドレス (`claude -p --agent ...`) で動かし、Playwright MCP で Chrome を操作します。予約・ログイン・個人情報の入力は一切しません。
- 読み取れなかったホテルがある回は「満室に戻った」とは判断しません（読み違いで Issue を閉じないため）。
- 通知の Issue を自分で閉じた場合（予約できた・見送った）、そのとき知らせたホテルは、一度満室に戻るまで再通知しません。

## セットアップ

### 1. Secret を設定する
リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で登録します。

| 名前 | 内容 |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude の認証トークン。Team プランの利用枠で動くので、API の料金はかからない |

bus_book_app で使っているものと同じトークンを登録してかまいません（手元の PC で `claude setup-token` を実行すると新しく発行もできます）。
**公開リポジトリですが、Secrets の中身は外から見えません。** 予約をしないので、氏名や電話番号などの個人情報は登録しません。

### 2. このブランチを main にマージする
GitHub Actions の定期実行 (`schedule`) はデフォルトブランチ (main) にあるワークフローだけが動きます。

### 3. 手動で試す
**Actions → toyoko-watch → Run workflow** で `mode = check` を選んで実行します（`target` に `shizuoka` などの id を入れると、その日程・エリアだけを調べます）。
実行結果の Summary に、対象ホテルと空室状況の表が出ます。Haiku が本物のサイトを正しく読めているか確認してください（`check` では Issue は作りません）。

### 4. 通知の受け取り方
- Issue はあなたに割り当てられるので、GitHub の通知（ベルのアイコン）に届きます。
- メールで受け取るには、GitHub の **Settings → Notifications** で「Participating, @mentions and custom」の Email にチェックを入れます。
- スマホにすぐ届けたい場合は、GitHub Mobile アプリを入れて通知をオンにしてください。

## 10分ごとに確実に動かす
GitHub Actions の定期実行（`schedule`）は、混雑すると大きく遅れたり飛ばされたりします（数時間に1回しか動かないこともあります）。
そのため、Claude Code のクラウドセッション「toyoko-watch 起動係（10分ごと）」（Haiku）が、10分ごとにこのワークフローを `workflow_dispatch`（mode=watch）で起動します。
- 直前8分以内に実行がある・実行中のときは起動しません（GitHub の定期実行と重ならないように）。
- ワークフローが無効化されているあいだは起動しません。
- 監視期限を過ぎたら、起動係も止まります。

## 通知が来たら・止めたいとき

| 状況 | どうするか |
|---|---|
| `[東横イン] 空室あり: ...` の Issue が来た | Issue のリンクから公式サイトを開いて、早めに予約する（空室はすぐ埋まることがあります） |
| 予約できたので監視を止めたい | Actions → toyoko-watch → 右上の「…」→ **Disable workflow** |
| 予約できたが、ほかのホテルの空きも知りたい | Issue を閉じるだけでよい（知らせ済みのホテルは再通知しない） |
| `[東横イン] 空室チェックが続けて失敗しています` の Issue が来た | 実行ログを確認する（サイトのメンテナンス、画面の変更、海外IPからのアクセス制限など）。監視は続いています |
| 監視期限を過ぎた | 自動で終了し、Issue で知らせます |

## 費用の目安
- **Claude**: `CLAUDE_CODE_OAUTH_TOKEN` を使えば追加料金はなく、Team プランの利用枠を使います。空室チェックは軽い Haiku で行います。
- **GitHub Actions**: 公開リポジトリなので無料・無制限です。

## 注意
- 開発環境からは toyoko-inn.com に接続できなかったため、本物のサイトでの動作は「手動で試す」で確認してください。
- 東横イン公式サイトの空室表示は、他サイト（楽天トラベルなど）と異なることがあります。このシステムは公式サイトだけを見ます。
- 低頻度（10分に1回）のアクセスですが、サイトの利用規約は各自で確認してください。

## 手元で動かす
Node.js 18 以上、Python 3.11 以上、Google Chrome が必要です。`claude` に Team プランのアカウントでログイン済みなら、トークンの設定は要りません。

```bash
npm install -g @anthropic-ai/claude-code @playwright/mcp@0.0.83
python3 scripts/watch_vacancy.py --mode check   # 空室チェックだけ（結果は out/ に保存）
```

## ファイル構成

| パス | 内容 |
|---|---|
| `.claude/agents/toyoko-vacancy-checker.md` | 空室チェックのエージェント（Haiku・読み取り専用） |
| `.github/workflows/toyoko-watch.yml` | 10分ごとの定期実行タスク |
| `scripts/watch_vacancy.py` | 1回分の処理（期限の確認 → 空室チェック → 通知） |
| `config/target.json` | 監視条件（日程・人数・対象ホテル・監視期限） |
| `config/mcp.json`, `config/playwright.json` | Playwright MCP（ブラウザ）の設定 |
| `schemas/vacancy_result.schema.json` | 空室チェックの出力形式 |
