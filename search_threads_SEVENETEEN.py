import argparse
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from selenium.common.exceptions import WebDriverException

import SEVENETEEN as settings
from search_threads_today import (
    build_downloaded_post,
    build_error_post,
    is_media_post_url,
    load_local_env,
    normalize_post_url,
    search_url,
    start_driver,
    wait_for_body,
)


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "seventeen.sqlite3"
DEFAULT_PROFILE_DIR = "chrome_profile"
DEFAULT_SEARCH_WORD = getattr(settings, "SEARCH_WORD", "seventeen")
DEFAULT_SEARCH_WORDS = list(getattr(settings, "SEARCH_WORDS", [DEFAULT_SEARCH_WORD]))
DEFAULT_MAX_N = int(getattr(settings, "MAX_N", 10))
DEFAULT_MAX_IDLE_ROUNDS = int(getattr(settings, "MAX_IDLE_ROUNDS", 24))


def connect_database(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            num INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT NOT NULL UNIQUE,
            main_text TEXT NOT NULL DEFAULT '',
            post_time TEXT NOT NULL DEFAULT '',
            scraped_at TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.commit()
    return conn


def downloaded_source_urls(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT source_url FROM posts").fetchall()
    return {normalize_post_url(row[0]) for row in rows if row[0]}


def saved_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM posts").fetchone()
    return int(row[0] or 0)


def post_time_to_hour(post_time: str) -> str:
    post_time = (post_time or "").strip()
    if not post_time:
        return ""

    for date_format in ("%Y.%m.%d %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(post_time, date_format).strftime("%Y.%m.%d %H")
        except ValueError:
            pass

    return post_time[:13] if len(post_time) >= 16 and post_time[13] == ":" else post_time


def save_post(conn: sqlite3.Connection, post: dict) -> int | None:
    source_url = normalize_post_url(post.get("source_url", ""))
    if not source_url:
        return None

    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO posts (
            source_url,
            main_text,
            post_time,
            scraped_at,
            error
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            source_url,
            post.get("main_text", "") or "",
            post_time_to_hour(post.get("post_time", "")),
            post.get("scraped_at", "") or datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
            post.get("error", "") or "",
        ),
    )
    conn.commit()

    if cursor.rowcount == 0:
        return None

    row = conn.execute("SELECT num FROM posts WHERE source_url = ?", (source_url,)).fetchone()
    return int(row[0]) if row else None


def print_saved(num: int | None, main_text: str) -> None:
    if num is None:
        return
    preview = " ".join((main_text or "").split())[:20]
    print(f"{num}. {preview}")


def record_error(conn: sqlite3.Connection, url: str, error: Exception | str) -> None:
    error_post = build_error_post(url, error)
    num = save_post(conn, error_post)
    print_saved(num, error_post.get("main_text", ""))


def reached_limit(conn: sqlite3.Connection, limit: int) -> bool:
    return limit > 0 and saved_count(conn) >= limit


def extract_post_without_date(driver, url: str, keyword: str, delay: float) -> dict | None:
    if is_media_post_url(url):
        return None

    driver.get(url.replace("https://www.threads.com/", "https://www.threads.net/"))
    time.sleep(delay)
    body_text = wait_for_body(driver)

    return {
        "search_word": keyword,
        "url": normalize_post_url(url),
        "final_url": driver.current_url,
        "text": body_text,
        "raw_html": driver.page_source or "",
        "view_count": "",
        "action_counts": {},
        "scraped_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def visible_post_urls_driver(driver) -> list[str]:
    hrefs = driver.execute_script(
        "return Array.from(document.querySelectorAll(\"a[href*='/post/']\")).map((a) => a.href);"
    ) or []
    return [normalize_post_url(href) for href in hrefs if href and "threads." in href]


def scroll_search_page_driver(driver, idle_rounds: int) -> None:
    distance = 700 + min(idle_rounds, 12) * 180
    if idle_rounds < 3:
        direction = 1
    else:
        phase = (idle_rounds - 3) % 12
        direction = -1 if phase < 5 else 1

    driver.execute_script(
        """
        const distance = arguments[0] * arguments[1];
        const event = new WheelEvent('wheel', {deltaY: distance, bubbles: true, cancelable: true});
        window.dispatchEvent(event);
        document.dispatchEvent(event);
        if (document.activeElement) {
            document.activeElement.dispatchEvent(event);
        }
        window.scrollBy({top: distance, behavior: 'instant'});
        """,
        distance,
        direction,
    )


def process_post(conn: sqlite3.Connection, url: str, post: dict | None, downloaded: set[str]) -> None:
    if not post:
        return

    downloaded_post = build_downloaded_post(post)
    num = save_post(conn, downloaded_post)
    downloaded.add(normalize_post_url(url))
    print_saved(num, downloaded_post.get("main_text", ""))


def unique_keywords(keywords: list[str]) -> list[str]:
    unique = []
    seen = set()
    for keyword in keywords:
        keyword = str(keyword).strip()
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        unique.append(keyword)
    return unique


def scrape_keyword(
    conn: sqlite3.Connection,
    driver,
    keyword: str,
    limit: int,
    delay: float,
    search_handle: str,
    post_handle: str,
    seen: set[str],
    downloaded: set[str],
    max_idle_rounds: int,
) -> None:
    print(f"Search word: {keyword}")
    driver.switch_to.window(search_handle)
    driver.get(search_url(keyword))
    time.sleep(delay)
    idle_rounds = 0

    while idle_rounds < max_idle_rounds:
        if reached_limit(conn, limit):
            break

        try:
            wait_for_body(driver)
        except Exception:
            pass

        saved_before = saved_count(conn)
        for normalized_url in visible_post_urls_driver(driver):
            if reached_limit(conn, limit):
                break
            if normalized_url in seen or is_media_post_url(normalized_url):
                continue
            seen.add(normalized_url)

            driver.switch_to.window(post_handle)
            try:
                post = extract_post_without_date(
                    driver,
                    normalized_url,
                    keyword=keyword,
                    delay=delay,
                )
                process_post(conn, normalized_url, post, downloaded)
            except Exception as error:
                record_error(conn, normalized_url, error)
                downloaded.add(normalized_url)
            finally:
                driver.switch_to.window(search_handle)

        saved_after = saved_count(conn)
        idle_rounds = idle_rounds + 1 if saved_after == saved_before else 0
        scroll_search_page_driver(driver, idle_rounds)
        time.sleep(delay)

    if idle_rounds >= max_idle_rounds:
        print(f"No new posts for {max_idle_rounds} rounds. Moving to next keyword.")


def run_with_driver(
    conn: sqlite3.Connection,
    keywords: list[str],
    limit: int,
    profile_dir: str,
    delay: float,
    max_idle_rounds: int,
) -> None:
    try:
        driver = start_driver(profile_dir)
    except WebDriverException as error:
        raise SystemExit(
            "Could not start Chrome with chrome_profile. Close any Chrome window using this profile and try again.\n"
            f"{error}"
        ) from error

    try:
        print(f"Saved posts: {saved_count(conn)}/{limit if limit > 0 else 'unlimited'}")
        downloaded = downloaded_source_urls(conn)
        seen = set(downloaded)

        driver.get("about:blank")
        search_handle = driver.current_window_handle
        driver.switch_to.new_window("tab")
        post_handle = driver.current_window_handle
        driver.switch_to.window(search_handle)

        for keyword in keywords:
            if reached_limit(conn, limit):
                break

            scrape_keyword(
                conn,
                driver,
                keyword=keyword,
                limit=limit,
                delay=delay,
                search_handle=search_handle,
                post_handle=post_handle,
                seen=seen,
                downloaded=downloaded,
                max_idle_rounds=max_idle_rounds,
            )
    finally:
        driver.quit()


def main() -> None:
    load_local_env()

    parser = argparse.ArgumentParser(description="Search Threads posts containing seventeen and save them to SQLite.")
    parser.add_argument("--keyword", default=None, help="Search one keyword only. Default: use SEARCH_WORDS from SEVENETEEN.py")
    parser.add_argument("--limit", "--max-n", dest="limit", type=int, default=DEFAULT_MAX_N, help=f"Stop when the database reaches N saved posts. Use 0 for no limit. Default: {DEFAULT_MAX_N}")
    parser.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR, help=f"Chrome profile directory. Default: {DEFAULT_PROFILE_DIR}")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help=f"SQLite database path. Default: {DEFAULT_DB_PATH}")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay after page actions. Default: 2")
    parser.add_argument("--max-idle-rounds", type=int, default=DEFAULT_MAX_IDLE_ROUNDS, help=f"Rounds without new saved posts before moving to the next keyword. Default: {DEFAULT_MAX_IDLE_ROUNDS}")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    conn = connect_database(db_path)
    try:
        keywords = unique_keywords([args.keyword] if args.keyword else DEFAULT_SEARCH_WORDS)
        print(f"Search words: {len(keywords)}")
        print(f"Database: {db_path.resolve()}")
        if args.limit > 0:
            print(f"Max saved posts: {args.limit}")
        else:
            print("Max saved posts: unlimited")

        run_with_driver(
            conn,
            keywords=keywords,
            limit=args.limit,
            profile_dir=args.profile_dir,
            delay=args.delay,
            max_idle_rounds=args.max_idle_rounds,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
