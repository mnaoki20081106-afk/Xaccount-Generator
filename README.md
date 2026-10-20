# Xaccount-Generator

X（Twitter）アカウント自動生成・運用Bot（低コスト版）

## 現状
実装開始。設計は [DESIGN.md](DESIGN.md) を参照。

## 特徴
- IPRoyalなど安い住宅用プロキシ前提
- 2FA（TOTP）対応
- ウォームアップ & シャドウバン対策
- 販売用エクスポート機能

## セットアップ（予定）

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp config.example.yaml config.yaml
# config.yaml を編集
python -m src.main
```

## 注意
Xの利用規約に反する可能性があります。自己責任で使用してください。
