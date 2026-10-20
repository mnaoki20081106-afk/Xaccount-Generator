"""
ウォームアップ用日本語ツイートテンプレ。
自然に見えるよう多様なトピックをカバー。
"""
import random
from faker import Faker

fake = Faker("ja_JP")

# カテゴリ別テンプレ。{} はランダム埋め込み
_FOOD = [
    "今日のランチは{food}にした。美味しかった☺️",
    "{food}食べたい気分。どこかいい店ないかな",
    "夜ごはん{food}作った。我ながらうまくできた",
    "{food}って最高だよね。定期的に食べたくなる",
    "コンビニで{snack}買ってきた。これ好きすぎる",
    "カフェで作業してたら{drink}飲みすぎた",
    "{food}のレシピ調べてたらお腹すいてきた",
    "久しぶりに{food}食べたらめちゃくちゃ美味しかった",
]

_WEATHER = [
    "今日は天気いいな〜。気持ちいい",
    "急に寒くなってきた。もう秋だな",
    "雨続きで気分が上がらない",
    "今日暑すぎ。早く涼しくなってほしい",
    "朝起きたら快晴だった。いい一日になりそう",
    "今日は雲ひとつない青空。テンション上がる",
    "最近天気が読めないな。傘手放せない",
]

_DAILY = [
    "今日もお疲れ様でした",
    "あっという間に一週間終わった",
    "最近なんかいいことないかな〜",
    "ゆっくりする時間大事にしたい",
    "今日やろうと思ってたこと全部できた。珍しい",
    "なんか眠い。もう寝ようかな",
    "明日もがんばろ",
    "休日あっという間に終わる問題",
    "久しぶりに早起きしたら気持ちよかった",
    "ちょっと散歩してきた。気分転換になった",
    "積んでた{media}をやっと消化できた",
    "今週疲れたな〜。週末はゆっくりしたい",
    "なんとなく部屋の掃除したらすっきりした",
    "今日初めて{thing}やってみた。意外と楽しい",
    "もう今年も残り少ないの早すぎる",
]

_ENTERTAINMENT = [
    "最近{media}にハマってる。時間溶ける",
    "{genre}の新作気になってる。見てみようかな",
    "{media}って改めて面白いよな。何度でも見れる",
    "おすすめの{media}教えてほしい。ネタ切れ気味",
    "{genre}好きな人と話したい",
    "久しぶりに映画館行きたいな",
    "{media}の続き楽しみすぎる",
]

_SHORT = [
    "いい感じ",
    "眠い",
    "腹減った",
    "いい天気",
    "疲れた〜",
    "やる気出ない",
    "ひさしぶり",
    "のんびり",
    "まったり",
    "よし",
]

# 埋め込みワード
_FOODS = ["ラーメン", "カレー", "うどん", "寿司", "焼肉", "パスタ", "そば", "天ぷら", "餃子", "ハンバーグ"]
_SNACKS = ["チョコレート", "ポテチ", "アイス", "グミ", "プリン", "クッキー"]
_DRINKS = ["コーヒー", "緑茶", "カフェラテ", "紅茶", "ほうじ茶"]
_MEDIA = ["アニメ", "漫画", "ドラマ", "映画", "ゲーム", "小説", "YouTube"]
_GENRES = ["SF", "ファンタジー", "ミステリー", "ホラー", "ラブコメ", "アクション"]
_THINGS = ["筋トレ", "料理", "読書", "ヨガ", "ランニング", "瞑想"]


def random_tweet() -> str:
    """ランダムな日本語ツイートを生成。"""
    category = random.choices(
        ["food", "weather", "daily", "entertainment", "short"],
        weights=[25, 15, 35, 15, 10],
    )[0]

    if category == "food":
        tmpl = random.choice(_FOOD)
        return tmpl.format(
            food=random.choice(_FOODS),
            snack=random.choice(_SNACKS),
            drink=random.choice(_DRINKS),
        )
    elif category == "weather":
        return random.choice(_WEATHER)
    elif category == "daily":
        tmpl = random.choice(_DAILY)
        return tmpl.format(
            media=random.choice(_MEDIA),
            thing=random.choice(_THINGS),
        )
    elif category == "entertainment":
        tmpl = random.choice(_ENTERTAINMENT)
        return tmpl.format(
            media=random.choice(_MEDIA),
            genre=random.choice(_GENRES),
        )
    else:
        return random.choice(_SHORT)


def random_tweet_batch(n: int = 3) -> list[str]:
    """n本のツイートを重複なしで生成。"""
    tweets = set()
    while len(tweets) < n:
        tweets.add(random_tweet())
    return list(tweets)
