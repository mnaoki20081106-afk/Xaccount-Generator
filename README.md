# Xaccount-Generator

X（Twitter）アカウント自動生成・ウォームアップ・シャドウバン管理Bot

---

## 必要な有料サービスと費用感

100垢を21日間ウォームアップして販売可能状態にするまでの目安コスト。

| サービス | 用途 | 目安コスト |
|---|---|---|
| VPS | Bot本体の稼働 | $4〜6/月 |
| 住宅プロキシ (IPRoyal) | 登録・操作時のIP偽装 | $3〜8/100垢 |
| SMS認証 (5sim) | 電話番号認証 | $4〜8/100垢 |
| Arkoseソルバー (CDS) | 登録時のFunCaptcha解決 | $2〜5/100垢 |
| メールアドレス (Outlook) | 無料 | $0 |

**合計: 100垢で約 $13〜27**（VPS除く消耗品のみ）

---

## 1. VPS

### スペック要件

| 構成 | 推奨スペック |
|---|---|
| ウォームアップのみ（APIモード） | 1 vCPU / 1GB RAM |
| 生成 + ウォームアップ同時 | 2 vCPU / 2GB RAM |
| 100垢フル並列 | 2 vCPU / 4GB RAM |

APIモード（`auth_token + ct0` がある垢）はChromiumを起動しないため非常に軽い。
生成フェーズだけブラウザを使うので、生成と運用を分けると安く済む。

### おすすめプロバイダー

```
Contabo   — $4.5/月〜 (2 vCPU / 4GB)  ← コスパ最強
Hetzner   — $4.1/月〜 (2 vCPU / 2GB)  ← EU、安定
Vultr     — $6/月〜   (1 vCPU / 1GB)
DigitalOcean — $6/月〜 (1 vCPU / 1GB)
```

> ロケーションは **米国 or 欧州** を選ぶ。日本VPSはIP評価が高くX側にBotとして弾かれやすい。

### OS・セットアップ

```bash
# Ubuntu 22.04 LTS 推奨
apt update && apt upgrade -y
apt install -y docker.io docker-compose git

# リポジトリをクローン
git clone https://github.com/YOUR_REPO/Xaccount-Generator.git
cd Xaccount-Generator
```

---

## 2. 住宅プロキシ (IPRoyal)

X はデータセンターIPを高確率で弾く。**住宅用（Residential）プロキシ必須**。

### 購入

1. [iproyal.com](https://iproyal.com) でアカウント作成
2. **Residential Proxies** → 購入（GB単位課金）
3. プロキシ情報をコピー

### コスト感

- 混合IP（country: ""）: $1.75/GB〜
- 100垢の登録+21日運用: 約 2〜4GB = **$3.5〜7**
- APIモードに移行後は通信量が激減するため登録フェーズが一番消費する

### config.yaml に記入

```yaml
proxy:
  provider: "iproyal"
  host: "geo.iproyal.com"
  port: 12321
  username: "YOUR_PROXY_USER"   # ダッシュボードからコピー
  password: "YOUR_PROXY_PASS"
  country: ""                   # 混合IP（最安）
  sticky_ttl_minutes: 30
  max_accounts_per_ip: 3
```

> `max_accounts_per_ip: 3` は1つのIPに割り当てる垢数の上限。増やすと節約できるが同一IP複数垢はBAN率が上がる。

---

## 3. SMS認証 (5sim)

### 購入

1. [5sim.net](https://5sim.net) でアカウント作成・チャージ
2. **Profile → API Key** でキーをコピー

### コスト感

- X用番号（USA/UK/DE/FR/NL）: $0.04〜$0.08/件
- 100垢: 最大 **$8**（成功率85〜90%なので多めに買っておく）

### バックアップ: smsactivate

5simで番号取れない場合の保険。`provider: "auto"` で自動切り替え。

1. [sms-activate.org](https://sms-activate.org) でアカウント作成
2. APIキーをコピー

```yaml
sms:
  provider: "auto"              # 5sim優先、失敗したらsmsactivateへ自動切り替え
  api_key: "YOUR_5SIM_KEY"
  api_key2: "YOUR_SMSACTIVATE_KEY"   # バックアップ（省略可）
  preferred_countries:
    - "usa"
    - "england"
    - "germany"
    - "france"
    - "netherlands"
```

> `russia`, `india`, `indonesia` 等の安い国は **X側に即BAN** されやすいので使わない。

---

## 4. Arkoseソルバー (CDS)

X の登録フロー（api_flow.py）でFunCaptchaが出る。これを解くためのAPI。

### 取得方法

Telegram で **@keysolv_bot** に `/getkey` → **無料APIキー** が貰える（制限あり）

有料枠が必要な場合: [cds-solver.com](https://cds-solver.com)

```yaml
captcha:
  provider: "capsolver"         # ブラウザフローのFunCaptcha用
  api_key: "YOUR_CAPSOLVER_KEY"
  cds_api_key: "YOUR_CDS_KEY"   # APIフロー（api_flow.py）用
```

### ブラウザフロー用: Capsolver（任意）

ブラウザ経由で登録する場合のみ必要。APIフローが成功していれば不要。

1. [capsolver.com](https://capsolver.com) でアカウント作成
2. チャージ → APIキーをコピー（$0.5〜2/100垢程度）

---

## 5. メールアドレス (Outlook)

**無料**。自動でOutlookアドレスを生成する。

```yaml
email:
  provider: "auto"              # Outlook優先 → 失敗時 mail.tm
  outlook_domain: "outlook.com" # または hotmail.com
```

> mail.tm の使い捨てドメインはXに弾かれやすい。Outlookを主にする。

---

## セットアップ手順

### 1. 設定ファイルを作る

```bash
cp config.example.yaml config.yaml
nano config.yaml   # 各APIキーを記入
```

### 2. Docker で起動（推奨）

```bash
# ビルド
docker compose build

# フルデーモン起動（100垢目標、バックグラウンド）
docker compose up -d

# ログを確認
docker compose logs -f generator
```

### 3. Docker なし（直接起動）

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 垢を10件生成してみる
python -m src.main generate --count 10

# ウォームアップを1サイクル回す
python -m src.main warmup

# シャドウバン確認
python -m src.main check

# フルデーモン（100垢目標）
python -m src.main run --target 100
```

### 4. 2GB VPS でメモリが足りない場合

生成とウォームアップを別プロセスに分ける。

```bash
# ターミナル1: 生成ワーカー
python -m src.main worker --type generate --concurrency 1 &

# ターミナル2: ウォームアップワーカー
python -m src.main worker --type warmup --concurrency 3
```

---

## 垢一覧・ステータス確認

```bash
python -m src.main list
```

```
  ID  username               status          shadow  created
────────────────────────────────────────────────────────────
   1  example_user123        warming           0.00  10-01 09:12
   2  another_user456        clean             0.00  09-10 14:33   ← 販売可能
   3  banned_user789         soft_shadow       0.40  10-05 11:20
```

| ステータス | 意味 |
|---|---|
| `created` | 生成直後 |
| `warming` | ウォームアップ中（21日待機） |
| `clean` | ウォームアップ完了・**販売可能** |
| `soft_shadow` | 軽いシャドウバン、自動回復中 |
| `hard_shadow` | 重篤なバン、クーリング中 |
| `locked` | ロック状態 |
| `suspended` | 凍結 |

---

## ウォームアップの流れ

```
Day 0〜3  (SEED)    タイムライン閲覧のみ
Day 3〜7  (OBSERVE) 閲覧 + 検索ブラウズ
Day 7〜14 (ENGAGE)  いいね + フォロー + プロフィール訪問
Day 14〜21(ACTIVE)  ツイート + リツイート解禁
Day 21+   (MATURE)  全機能フル + 引用ツイート
         ↓
  【卒業チェック】
  ・シャドウバン: 3ソース多数決でスコア0
  ・検索TOP:  live search + typeahead 可視性 ≥ 60%
         ↓
  AccountStatus.CLEAN → 販売可能
```

---

## よくある問題

**生成が全然通らない**
→ CDSキーを確認。Telegram `@keysolv_bot` で無料キー再取得。
→ プロキシを混合IPに変更（`country: ""`）。

**ウォームアップ中にBANされる**
→ `max_accounts_per_ip` を `2` に下げる。
→ SMS番号の国を USA/England に絞る。

**21日経ってもCLEANにならない**
→ `check` コマンドで詳細確認。
→ shadowban.eu で手動確認してエラー内容を見る。

**VPS のメモリが足りない**
→ `worker` コマンドで生成・ウォームアップを別プロセスに分ける。
→ `warmup-concurrency` を `3` に下げる。

---

## 注意事項

Xの利用規約に反します。自己責任で使用してください。
