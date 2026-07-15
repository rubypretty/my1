import csv
import math
import random
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from pathlib import Path


SOURCE_DB = Path("seventeen_clean.sqlite3")
OUTPUT_IMAGE = Path("seventeen_wordcloud_notall.png")
OUTPUT_TOP_WORDS = Path("seventeen_wordcloud_notall_top_words.csv")

IMAGE_WIDTH = 1800
IMAGE_HEIGHT = 1200
MAX_WORDS = 500
RANDOM_SEED = 42

FONT_PATHS = [
    Path(r"C:\Windows\Fonts\NotoSansTC-VF.ttf"),
    Path(r"C:\Windows\Fonts\msjh.ttc"),
    Path(r"C:\Windows\Fonts\malgun.ttf"),
    Path(r"C:\Windows\Fonts\YuGothM.ttc"),
    Path(r"C:\Windows\Fonts\seguiemj.ttf"),
    Path(r"C:\Windows\Fonts\arial.ttf"),
]

EMOJI_FONT = Path(r"C:\Windows\Fonts\seguiemj.ttf")
PALETTE = [
    (56, 84, 142),
    (42, 157, 143),
    (91, 192, 125),
    (118, 68, 158),
    (231, 196, 15),
    (38, 111, 145),
]

STOPWORDS = {
    "你",
    "我",
    "他",
    "她",
    "它",
    "你們",
    "我們",
    "他們",
    "她們",
    "自己",
    "大家",
    "人",
    "的",
    "了",
    "是",
    "在",
    "有",
    "和",
    "跟",
    "也",
    "都",
    "就",
    "很",
    "還",
    "要",
    "會",
    "可以",
    "沒有",
    "不是",
    "如果",
    "因為",
    "所以",
    "這",
    "那",
    "這個",
    "那個",
    "一個",
    "什麼",
    "怎麼",
    "真的",
    "知道",
    "看到",
    "想",
    "說",
    "請",
    "啊",
    "啦",
    "嗎",
    "呢",
    "吧",
    "喔",
    "哦",
    "欸",
    "翻譯",
    "轉",
    "轉發",
    "分享",
    "原文",
    "thread",
    "threads",
    "this",
    "that",
    "the",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "for",
    "with",
    "is",
    "are",
    "be",
    "my",
    "me",
    "you",
    "your",
    "our",
    "we",
    "it",
    "a",
    "an",
    "이",
    "가",
    "은",
    "는",
    "을",
    "를",
    "에",
    "에서",
    "도",
    "와",
    "과",
    "하고",
    "다",
    "고",
    "서",
    "하",
    "어",
    "아",
    "지",
    "나",
    "요",
    "の",
    "に",
    "は",
    "を",
    "が",
    "て",
    "た",
    "し",
    "で",
    "と",
    "も",
    "から",
    "です",
    "ます",
    ".",
    ",",
    "，",
    "。",
    "!",
    "！",
    "?",
    "？",
    "、",
    "/",
    "\\",
    "-",
    "_",
    "(",
    ")",
    "（",
    "）",
    "[",
    "]",
    "【",
    "】",
    "{",
    "}",
    ":",
    "：",
    ";",
    "；",
    "\"",
    "'",
    "’",
    "‘",
    "“",
    "”",
    "...",
    "…",
    "~",
    "～",
    "+",
    "=",
    "&",
}


def require_packages() -> None:
    missing_packages = []

    try:
        import jieba  # noqa: F401
    except ImportError:
        missing_packages.append("jieba")

    try:
        import PIL  # noqa: F401
    except ImportError:
        missing_packages.append("pillow")

    try:
        import fontTools  # noqa: F401
    except ImportError:
        missing_packages.append("fonttools")

    if missing_packages:
        package_text = " ".join(missing_packages)
        raise RuntimeError(
            "Missing package(s): "
            f"{', '.join(missing_packages)}\n"
            f"Please install them first:\npython -m pip install {package_text}"
        )


def load_main_texts() -> list[str]:
    if not SOURCE_DB.exists():
        raise FileNotFoundError(f"Cannot find source database: {SOURCE_DB}")

    con = sqlite3.connect(SOURCE_DB)
    try:
        rows = con.execute(
            """
            SELECT main_text
            FROM posts
            WHERE main_text IS NOT NULL AND TRIM(main_text) <> ''
            ORDER BY serial_id ASC
            """
        ).fetchall()
    finally:
        con.close()

    return [row[0] for row in rows]


def is_emoji_char(char: str) -> bool:
    code = ord(char)
    return (
        0x1F000 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
        or 0xFE00 <= code <= 0xFE0F
        or code == 0x200D
    )


def is_invisible_joiner_or_selector(token: str) -> bool:
    return all(ord(char) == 0x200D or 0xFE00 <= ord(char) <= 0xFE0F for char in token)


def tokenize_text(text: str) -> list[str]:
    hashtag_pattern = re.compile(r"#[^\s#]+")
    tokens = []
    cursor = 0

    for match in hashtag_pattern.finditer(text):
        before_hashtag = text[cursor : match.start()]
        tokens.extend(tokenize_non_hashtag_text(before_hashtag))
        tokens.append(match.group())
        cursor = match.end()

    tokens.extend(tokenize_non_hashtag_text(text[cursor:]))
    return merge_emoji_selectors(tokens)


def tokenize_non_hashtag_text(text: str) -> list[str]:
    import jieba

    tokens = []
    for token in jieba.lcut(text, cut_all=False):
        token = token.strip()
        if token:
            tokens.append(token)
    return tokens


def merge_emoji_selectors(tokens: list[str]) -> list[str]:
    merged = []
    for token in tokens:
        if is_invisible_joiner_or_selector(token) and merged:
            merged[-1] += token
        else:
            merged.append(token)
    return merged


def build_frequencies(texts: list[str]) -> Counter:
    frequencies = Counter()
    for text in texts:
        tokens = [
            token
            for token in tokenize_text(text)
            if token.strip().lower() not in STOPWORDS
        ]
        frequencies.update(tokens)
    return frequencies


def save_top_words(frequencies: Counter, limit: int = 300) -> None:
    with OUTPUT_TOP_WORDS.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["word", "count"])
        writer.writerows(frequencies.most_common(limit))


class FontManager:
    def __init__(self, font_paths: list[Path]):
        from fontTools.ttLib import TTCollection, TTFont

        self.font_paths = [path for path in font_paths if path.exists()]
        if not self.font_paths:
            raise FileNotFoundError("Cannot find any usable font in FONT_PATHS.")

        self.supported_codepoints = {}
        for path in self.font_paths:
            codepoints = set()
            try:
                if path.suffix.lower() == ".ttc":
                    collection = TTCollection(str(path))
                    fonts = collection.fonts
                else:
                    fonts = [TTFont(str(path), fontNumber=0)]

                for font in fonts:
                    for table in font["cmap"].tables:
                        codepoints.update(table.cmap.keys())
            except Exception:
                codepoints = set()

            self.supported_codepoints[path] = codepoints

        self.font_cache = {}

    def font_for_char(self, char: str, size: int):
        codepoint = ord(char)

        if EMOJI_FONT.exists() and is_emoji_char(char):
            emoji_supported = self.supported_codepoints.get(EMOJI_FONT, set())
            if codepoint in emoji_supported:
                return self.get_font(EMOJI_FONT, size)

        for path in self.font_paths:
            if codepoint in self.supported_codepoints.get(path, set()):
                return self.get_font(path, size)

        return self.get_font(self.font_paths[0], size)

    def get_font(self, path: Path, size: int):
        from PIL import ImageFont

        cache_key = (str(path), size)
        if cache_key not in self.font_cache:
            self.font_cache[cache_key] = ImageFont.truetype(str(path), size=size)
        return self.font_cache[cache_key]


def split_font_runs(token: str, size: int, font_manager: FontManager):
    runs = []
    current_font = None
    current_text = []

    for char in token:
        font = font_manager.font_for_char(char, size)
        if current_font is not None and font.path == current_font.path:
            current_text.append(char)
        else:
            if current_text:
                runs.append(("".join(current_text), current_font))
            current_text = [char]
            current_font = font

    if current_text:
        runs.append(("".join(current_text), current_font))

    return runs


def text_size(token: str, size: int, font_manager: FontManager) -> tuple[int, int]:
    from PIL import Image, ImageDraw

    temp_image = Image.new("RGBA", (1, 1), (255, 255, 255, 0))
    draw = ImageDraw.Draw(temp_image)
    width = 0
    height = 0

    for text, font in split_font_runs(token, size, font_manager):
        bbox = draw.textbbox((0, 0), text, font=font, embedded_color=True)
        width += max(1, bbox[2] - bbox[0])
        height = max(height, max(1, bbox[3] - bbox[1]))

    return width, height


def make_token_image(token: str, size: int, color, font_manager: FontManager):
    from PIL import Image, ImageDraw

    width, height = text_size(token, size, font_manager)
    padding = max(4, size // 10)
    image = Image.new("RGBA", (width + padding * 2, height + padding * 2), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    x = padding

    for text, font in split_font_runs(token, size, font_manager):
        bbox = draw.textbbox((0, 0), text, font=font, embedded_color=True)
        draw.text(
            (x, padding - bbox[1]),
            text,
            font=font,
            fill=color + (255,),
            embedded_color=True,
        )
        x += max(1, bbox[2] - bbox[0])

    return image


def token_font_size(count: int, max_count: int) -> int:
    min_size = 18
    max_size = 210
    scale = math.sqrt(count / max_count)
    return int(min_size + (max_size - min_size) * scale)


def place_token(canvas, occupancy, token_image, rng: random.Random) -> bool:
    from PIL import ImageChops

    token_mask = token_image.getchannel("A")
    token_width, token_height = token_image.size
    if token_width >= IMAGE_WIDTH or token_height >= IMAGE_HEIGHT:
        return False

    for _ in range(900):
        center_bias = rng.random() < 0.72
        if center_bias:
            x_center = int(rng.gauss(IMAGE_WIDTH / 2, IMAGE_WIDTH / 5))
            y_center = int(rng.gauss(IMAGE_HEIGHT / 2, IMAGE_HEIGHT / 5))
            x = x_center - token_width // 2
            y = y_center - token_height // 2
        else:
            x = rng.randint(0, IMAGE_WIDTH - token_width)
            y = rng.randint(0, IMAGE_HEIGHT - token_height)

        if x < 0 or y < 0 or x + token_width > IMAGE_WIDTH or y + token_height > IMAGE_HEIGHT:
            continue

        existing = occupancy.crop((x, y, x + token_width, y + token_height))
        if ImageChops.logical_and(existing, token_mask.convert("1")).getbbox() is None:
            canvas.alpha_composite(token_image, (x, y))
            occupancy.paste(1, (x, y), token_mask)
            return True

    return False


def make_word_cloud(frequencies: Counter) -> int:
    from PIL import Image

    rng = random.Random(RANDOM_SEED)
    font_manager = FontManager(FONT_PATHS)
    canvas = Image.new("RGBA", (IMAGE_WIDTH, IMAGE_HEIGHT), (255, 255, 255, 255))
    occupancy = Image.new("1", (IMAGE_WIDTH, IMAGE_HEIGHT), 0)

    top_words = frequencies.most_common(MAX_WORDS)
    max_count = top_words[0][1]
    placed_count = 0

    for token, count in top_words:
        size = token_font_size(count, max_count)
        color = rng.choice(PALETTE)
        token_image = make_token_image(token, size, color, font_manager)

        if rng.random() > 0.86 and token_image.width < 380:
            token_image = token_image.rotate(90, expand=True, resample=Image.Resampling.BICUBIC)

        if place_token(canvas, occupancy, token_image, rng):
            placed_count += 1

    canvas.convert("RGB").save(OUTPUT_IMAGE)
    return placed_count


def main() -> None:
    require_packages()

    texts = load_main_texts()
    frequencies = build_frequencies(texts)

    if not frequencies:
        raise RuntimeError("No words were found in main_text.")

    save_top_words(frequencies)
    placed_count = make_word_cloud(frequencies)

    print(f"Source database: {SOURCE_DB}")
    print(f"Rows read: {len(texts)}")
    print(f"Unique tokens: {len(frequencies)}")
    print(f"Tokens drawn: {placed_count}")
    print(f"Word cloud image: {OUTPUT_IMAGE}")
    print(f"Top words CSV: {OUTPUT_TOP_WORDS}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
