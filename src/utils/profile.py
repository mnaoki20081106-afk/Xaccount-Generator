"""
サインアップ後のプロフィール完成。
- アイコン画像 (thispersondoesnotexist.com から取得)
- ヘッダー画像 (無地グラデーション生成)
- 日本語 bio
- 場所 (日本の都市)
"""
import asyncio
import random
import httpx
from pathlib import Path
from loguru import logger
from faker import Faker

fake = Faker("ja_JP")

# 日本語 bio テンプレ
BIO_TEMPLATES = [
    "{hobby}が好きです。{place}在住。",
    "{hobby}｜{hobby2}｜日常呟き",
    "趣味は{hobby}。のんびり生きてます🌿",
    "{hobby}好き。{place}で暮らしてます。",
    "日常のことを呟いています。{hobby}が癒し。",
    "#{hobby} #{hobby2} 好きな人と繋がりたい",
    "{hobby}にハマり中。{place}から発信。",
    "🎵{hobby} 🍜{hobby2} 📸日常",
    "{place}在住。{hobby}と{hobby2}が趣味です。",
    "ゆるく呟いてます。{hobby}好き。",
]

HOBBIES = [
    "映画", "音楽", "読書", "料理", "カフェ巡り", "旅行", "写真",
    "アニメ", "ゲーム", "ヨガ", "ランニング", "登山", "キャンプ",
    "筋トレ", "スイーツ", "DIY", "ガーデニング", "釣り", "サイクリング",
    "コーヒー", "ドラマ", "漫画", "イラスト", "手芸", "バイク",
]

JP_CITIES = [
    "東京", "大阪", "名古屋", "福岡", "札幌", "仙台", "広島",
    "京都", "神戸", "横浜", "千葉", "埼玉", "新潟", "静岡",
]

# thispersondoesnotexist.com が返す顔写真 (毎リクエストランダム)
FACE_URL = "https://thispersondoesnotexist.com"


async def fetch_face_image(save_dir: str = "data/faces") -> str | None:
    """AI生成顔写真を取得してローカル保存。返り値はファイルパス。"""
    Path(save_dir).mkdir(parents=True, exist_ok=True)
    filename = f"{save_dir}/face_{random.randint(100000, 999999)}.jpg"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(FACE_URL, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                with open(filename, "wb") as f:
                    f.write(resp.content)
                logger.debug(f"顔写真取得: {filename}")
                return filename
    except Exception as e:
        logger.warning(f"顔写真取得失敗: {e}")
    return None


def generate_bio() -> str:
    tmpl = random.choice(BIO_TEMPLATES)
    hobby = random.choice(HOBBIES)
    hobby2 = random.choice([h for h in HOBBIES if h != hobby])
    place = random.choice(JP_CITIES)
    return tmpl.format(hobby=hobby, hobby2=hobby2, place=place)


async def setup_profile(page, username: str, display_name: str) -> bool:
    """
    X プロフィールページでアイコン・bio・場所を設定する。
    サインアップ完了後のページで呼ぶ。
    """
    try:
        # プロフィール編集ページへ
        await page.goto(
            f"https://x.com/settings/profile",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(random.uniform(1.5, 3.0))

        # 顔写真アップロード
        face_path = await fetch_face_image()
        if face_path:
            try:
                # アイコン変更ボタン
                avatar_selectors = [
                    '[data-testid="UserAvatar-Container-unknown"]',
                    'input[accept*="image"][data-testid="fileInput"]',
                    'label[for*="avatar"]',
                ]
                for sel in avatar_selectors:
                    try:
                        loc = page.locator(sel).first
                        if await loc.is_visible(timeout=3000):
                            if sel.startswith("input"):
                                await loc.set_input_files(face_path)
                            else:
                                await loc.click()
                                await asyncio.sleep(1)
                                file_input = page.locator('input[type="file"]').first
                                if await file_input.count() > 0:
                                    await file_input.set_input_files(face_path)
                            await asyncio.sleep(random.uniform(1.0, 2.0))
                            # Apply ボタン
                            apply_sel = [
                                'div[role="button"]:has-text("Apply")',
                                'div[role="button"]:has-text("適用")',
                                '[data-testid="applyButton"]',
                            ]
                            for asel in apply_sel:
                                try:
                                    aloc = page.locator(asel).first
                                    if await aloc.is_visible(timeout=2000):
                                        await aloc.click()
                                        await asyncio.sleep(1)
                                        break
                                except Exception:
                                    continue
                            break
                    except Exception:
                        continue
            except Exception as e:
                logger.debug(f"[{username}] アイコン設定スキップ: {e}")

        # bio 入力
        bio = generate_bio()
        bio_selectors = [
            'textarea[name="description"]',
            'textarea[data-testid="UserDescription"]',
            '[placeholder*="自己紹介"]',
            '[placeholder*="Bio"]',
        ]
        for sel in bio_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=3000):
                    await loc.fill(bio)
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                    break
            except Exception:
                continue

        # 場所
        city = random.choice(JP_CITIES)
        location_selectors = [
            'input[name="location"]',
            '[placeholder*="場所"]',
            '[placeholder*="Location"]',
        ]
        for sel in location_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2000):
                    await loc.fill(city)
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    break
            except Exception:
                continue

        # 保存
        save_selectors = [
            'div[role="button"]:has-text("保存")',
            'div[role="button"]:has-text("Save")',
            '[data-testid="settingsSaveButton"]',
        ]
        for sel in save_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=3000):
                    await loc.click()
                    await asyncio.sleep(random.uniform(1.5, 2.5))
                    logger.info(f"[{username}] プロフィール設定完了 bio='{bio[:20]}...'")
                    return True
            except Exception:
                continue

        logger.warning(f"[{username}] プロフィール保存ボタン見つからず")
        return False

    except Exception as e:
        logger.error(f"[{username}] プロフィール設定エラー: {e}")
        return False
