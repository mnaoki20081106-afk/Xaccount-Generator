# XアカウントファームBot 設計書（低コスト版）

## 1. 概要

100〜1000個のXアカウントを自動生成・運用・販売するBot。
**できるだけ低コスト**で回すことを優先。

- プロキシ：IPRoyal などの安い住宅用プロキシ（日本）をメイン
- 2FA（TOTP）対応
- 凍結対策を最優先
- 販売用インベントリ管理まで含む

## 2. 全体アーキテクチャ

```
[IPRoyal JP住宅用プロキシ]
          ↓
[Account Factory] → [2FA Manager] → [Warm-up Engine]
          ↓                    ↓
[Shadowban Detector] ← [Operation Engine]
          ↓
[SQLite / PostgreSQL] → [販売用エクスポート]
```

初期はSQLiteで軽く開始。規模が大きくなったらPostgreSQLへ移行。

## 3. プロキシ方針（低コスト）

- **メイン**: IPRoyal Residential（日本）
  - 従量課金 or 安い帯域プランを使用
  - スティッキーセッション対応を確認して使う
- バックアップ候補: Proxy-Cheap, SOAX の安いプラン
- 無料プロキシは使わない（成功率・生存率が低すぎるため）
- 運用開始時は少容量からテストし、生存率を見てスケール

### プロキシ管理ルール
- アカウントごとにスティッキー（可能なら15〜40分）
- ヘルスチェック：地理・応答速度・BAN検知
- BANされたIPは一定期間ブラックリスト

## 4. 主要コンポーネント

### A. Account Factory
優先順位：
1. メール + 可能な範囲で電話（コスト次第）
2. 純メール（初期はこちらでコスト抑制）

- Playwright + stealth
- 指紋隔離（アカウントごとにプロファイル）
- 生成レート厳守（IPあたり少数）
- Captchaは必要最小限（Capsolver等、使った分だけ課金）

### B. 2FA Manager
- TOTP必須対応
- secretを暗号化して保存
- ログイン時に自動生成

### C. Warm-up Engine
- 低活動から徐々に上げる
- 日本時間帯に合わせた行動
- コンテンツは事前用意 or 軽量生成

### D. Shadowban Detector
- 定期チェック
- Soft / Hard で対応を分岐

### E. Operation & Inventory
- 行動ログを残す
- 一定基準を満たしたアカウントを「販売可能」にマーク
- エクスポート機能（credentials + cookies + 2fa）

## 5. 技術スタック（確定）

- Python 3.12+
- Playwright（stealth）
- SQLite（初期）→ 必要ならPostgreSQL
- httpx / aiohttp
- pyotp（2FA）
- cryptography（暗号化）
- PyYAML / pydantic（設定）
- loguru（ログ）

## 6. ディレクトリ構成（実装）

```
src/
  ├── main.py
  ├── config.py
  ├── models.py
  ├── db.py
  ├── proxy/
  │   └── manager.py
  ├── factory/
  │   └── account_factory.py
  ├── auth/
  │   └── twofa.py
  ├── warmup/
  │   └── engine.py
  ├── detector/
  │   └── shadowban.py
  └── utils/
      ├── browser.py
      └── crypto.py
config.example.yaml
requirements.txt
data/          # DBやセッション置き場（gitignore）
```

## 7. 進行方針

1. 基盤（設定・DB・プロキシ管理）を固める
2. ブラウザ指紋 + アカウント生成の骨格
3. 2FA
4. ウォームアップ
5. 検知・運用
6. 販売エクスポート

低コストを維持しながら、動くものから順に積み上げる。

---
更新: 2026-10-20（IPRoyal低コスト方針に変更）
