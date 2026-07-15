import sqlite3
from pathlib import Path


SOURCE_DB = Path("seventeen_clean.sqlite3")
OUTPUT_DB = Path("seventeen_clean_zh.sqlite3")
TEMP_OUTPUT_DB = Path("seventeen_clean_zh.tmp.sqlite3")

MIN_CHINESE_CHARS = 2
MIN_CHINESE_RATIO = 0.30
MAX_KANA_RATIO = 0.20
EXCLUDE_PHRASES = ("AI 資訊", "尚無回覆", "查看動態")


def is_chinese_char(char: str) -> bool:
    return (
        "\u3400" <= char <= "\u4dbf"
        or "\u4e00" <= char <= "\u9fff"
        or "\uf900" <= char <= "\ufaff"
    )


def is_kana_char(char: str) -> bool:
    return "\u3040" <= char <= "\u30ff"


def is_meaningful_char(char: str) -> bool:
    return char.isalpha() or char.isdigit()


def is_chinese_text(text: str) -> bool:
    if not text:
        return False

    text = text.replace("翻譯", "")

    chinese_count = sum(is_chinese_char(char) for char in text)
    meaningful_count = sum(is_meaningful_char(char) for char in text)
    kana_count = sum(is_kana_char(char) for char in text)

    if meaningful_count == 0:
        return False

    chinese_ratio = chinese_count / meaningful_count
    kana_ratio = kana_count / meaningful_count

    return (
        chinese_count >= MIN_CHINESE_CHARS
        and chinese_ratio >= MIN_CHINESE_RATIO
        and kana_ratio <= MAX_KANA_RATIO
    )


def should_keep_row(text: str) -> bool:
    if not text:
        return False

    if any(phrase in text for phrase in EXCLUDE_PHRASES):
        return False

    return is_chinese_text(text)


def prepare_output_db() -> sqlite3.Connection:
    if TEMP_OUTPUT_DB.exists():
        TEMP_OUTPUT_DB.unlink()

    con = sqlite3.connect(TEMP_OUTPUT_DB)
    con.execute(
        """
        CREATE TABLE posts (
            serial_id INTEGER PRIMARY KEY,
            num INTEGER NOT NULL,
            main_text TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE UNIQUE INDEX idx_posts_num ON posts(num)")
    return con


def main() -> None:
    if not SOURCE_DB.exists():
        raise FileNotFoundError(f"Cannot find source database: {SOURCE_DB}")

    source_con = sqlite3.connect(SOURCE_DB)
    output_con = prepare_output_db()

    total_rows = 0
    kept_rows = 0

    try:
        source_cur = source_con.execute(
            """
            SELECT num, main_text
            FROM posts
            WHERE main_text IS NOT NULL AND TRIM(main_text) <> ''
            ORDER BY serial_id ASC
            """
        )

        with output_con:
            for num, main_text in source_cur:
                total_rows += 1
                if not should_keep_row(main_text):
                    continue

                kept_rows += 1
                output_con.execute(
                    """
                    INSERT INTO posts (serial_id, num, main_text)
                    VALUES (?, ?, ?)
                    """,
                    (kept_rows, num, main_text),
                )
    finally:
        source_con.close()
        output_con.close()

    if OUTPUT_DB.exists():
        OUTPUT_DB.unlink()
    TEMP_OUTPUT_DB.replace(OUTPUT_DB)

    print(f"Source rows checked: {total_rows}")
    print(f"Rows kept as Chinese text: {kept_rows}")
    print(f"Output database: {OUTPUT_DB}")


if __name__ == "__main__":
    main()
