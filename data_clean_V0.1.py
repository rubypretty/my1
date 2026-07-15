import sqlite3
import unicodedata
from pathlib import Path


SOURCE_DB = Path("seventeen.sqlite3")
OUTPUT_DB = Path("seventeen_clean.sqlite3")


def clean_main_text(text: str) -> str:
    """Keep content intact while removing storage/scraping noise."""
    if text is None:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")

    cleaned_chars = []
    for char in text:
        category = unicodedata.category(char)
        if category == "Cc" and char not in ("\n", "\t"):
            continue
        cleaned_chars.append(char)

    return "".join(cleaned_chars).strip()


def prepare_output_db() -> sqlite3.Connection:
    if OUTPUT_DB.exists():
        OUTPUT_DB.unlink()

    con = sqlite3.connect(OUTPUT_DB)
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
    source_cur = source_con.cursor()

    output_con = prepare_output_db()
    output_cur = output_con.cursor()

    total_rows = 0
    blank_rows = 0
    duplicate_rows = 0
    inserted_rows = 0
    seen_texts = set()

    query = """
        SELECT num, main_text
        FROM posts
        ORDER BY num ASC
    """

    with output_con:
        for num, main_text in source_cur.execute(query):
            total_rows += 1
            cleaned_text = clean_main_text(main_text)

            if not cleaned_text:
                blank_rows += 1
                continue

            if cleaned_text in seen_texts:
                duplicate_rows += 1
                continue

            seen_texts.add(cleaned_text)
            inserted_rows += 1
            output_cur.execute(
                """
                INSERT INTO posts (serial_id, num, main_text)
                VALUES (?, ?, ?)
                """,
                (inserted_rows, num, cleaned_text),
            )

    source_con.close()
    output_con.close()

    print(f"Source rows: {total_rows}")
    print(f"Removed blank main_text rows: {blank_rows}")
    print(f"Removed duplicate main_text rows: {duplicate_rows}")
    print(f"Clean rows written: {inserted_rows}")
    print(f"Output database: {OUTPUT_DB}")


if __name__ == "__main__":
    main()
