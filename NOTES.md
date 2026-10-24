# 実装メモ

## 現在の進捗（2026-10-20）

- [x] 設計（低コスト / IPRoyal前提）
- [x] プロジェクト骨格
- [x] 設定・DB・プロキシ管理・2FA
- [x] Playwright + stealth + 指紋対策
- [x] Xサインアップフロー骨格
- [x] **Outlookアカウント生成モジュール**
- [ ] Outlook実成功率の向上（Captcha・電話対策）
- [ ] メール認証コード受信の安定化
- [ ] X登録の本完成
- [ ] ウォームアップ
- [ ] シャドウバン検知

## Outlookについて

### 強み
- X側の信頼スコアが高め
- 販売時の見た目が良い
- 長期運用・復旧がしやすい

### 現実的な難しさ
- Microsoft側のCaptcha・電話認証・リスク判定が厳しい
- 住宅用プロキシ + 良い指紋でも成功率は高くない
- 量産するにはレート制限をかなり緩くする必要がある

### 使い方
```bash
# Outlook生成を試す
python -m src.main outlook

# Xアカウント生成（従来）
python -m src.main x
```

## 次のアクション候補
1. Outlookフローをheadless:falseで実機確認しセレクタ調整
2. Captchaソルバー接続
3. 電話認証が必要になった場合のSMSプロバイダ検討（コスト増）
4. 成功したOutlookをX登録のメールとして使う流れを接続

## 卒業テスト・ウォームアップ（2026-10-24）

- 卒業テストは X-Utility の shadowban.ts と同じ判定（観測者で `from:ユーザー名` Top検索 + 検索候補）。
  観測者の auth_token/ct0 は管理サイト「卒業テスト」タブから登録（暗号化保存）。未登録だと保留のまま。
- `python -m src.main graduate` で1回実行。`run` デーモンは check_interval ごとに実行。
- ウォームアップは垢ごとに1日1〜4セッションへ分散、上限は日数で滑らかに増加、約8日に1日は休み。
  自動化判定(226)・上限(344等)で24時間停止、凍結/ロック検知でステータス更新。
- いいね/投稿/RT/ホームTLは twikit と同じ GraphQL を優先（queryId は x.com の main.js から取得、旧1.1は予備）。
- 要実機確認: GraphQL の queryId/features は X 側で変わる。失敗時はログ `[XApi] ... failed, legacy fallback` を確認。
