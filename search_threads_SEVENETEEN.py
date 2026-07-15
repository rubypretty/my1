import argparse
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import urlopen

from selenium.common.exceptions import WebDriverException

import SEVENETEEN as settings
from search_threads_today import (
    build_downloaded_post,
    build_error_post,
    is_media_post_url,
    load_local_env,
    make_browser_openable_html,
    normalize_post_url,
    search_url,
    start_driver,
    wait_for_body,
)


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "seventeen.sqlite3"
DEFAULT_HTML_DIR = BASE_DIR / "seventeen.html"
DEFAULT_PROFILE_DIR = "chrome_profile"
DEFAULT_SEARCH_WORD = getattr(settings, "SEARCH_WORD", "seventeen")
DEFAULT_SEARCH_WORDS = list(getattr(settings, "SEARCH_WORDS", [DEFAULT_SEARCH_WORD]))
DEFAULT_MAX_N = int(getattr(settings, "MAX_N", 10))
DEFAULT_MAX_IDLE_ROUNDS = int(getattr(settings, "MAX_IDLE_ROUNDS", 24))
TELEGRAM_BOT_TOKEN = getattr(settings, "telegram_bot_token", "")
TELEGRAM_CHAT_ID = getattr(settings, "telegram_chat_id", "")
TELEGRAM_TIMEOUT_SECONDS = 10 * 60
TELEGRAM_SEND_TIMEOUT_SECONDS = 5
TELEGRAM_FAILURE_COOLDOWN_SECONDS = 5 * 60
TELEGRAM_MAX_CONSECUTIVE_FAILURES = 3
TELEGRAM_MAX_MESSAGE_LENGTH = 3500
HTTP_429_ERROR = "這個網頁無法正常運作，HTTP ERROR 429"
POST_LOAD_RETRIES = 3
SELENIUM_COMMAND_TIMEOUT_SECONDS = 45
DRIVER_RESTART_RETRIES = 3


class Http429PageError(RuntimeError):
    pass


class BrowserRecoveryFailed(RuntimeError):
    pass


def connect_database(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            num INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT NOT NULL UNIQUE,
            search_keyword TEXT NOT NULL DEFAULT '',
            main_text TEXT NOT NULL DEFAULT '',
            post_time TEXT NOT NULL DEFAULT '',
            scraped_at TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS keyword_runs (
            search_keyword TEXT PRIMARY KEY,
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    ensure_database_columns(conn)
    ensure_database_indexes(conn)
    conn.commit()
    return conn


def ensure_database_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(posts)").fetchall()}
    if "search_keyword" not in columns:
        conn.execute("ALTER TABLE posts ADD COLUMN search_keyword TEXT NOT NULL DEFAULT ''")


def ensure_database_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_source_url ON posts(source_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_search_keyword ON posts(search_keyword)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_num ON posts(num)")


def downloaded_source_urls(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT source_url FROM posts").fetchall()
    return {normalize_post_url(row[0]) for row in rows if row[0]}


def used_search_keywords(conn: sqlite3.Connection) -> set[str]:
    used = {
        row[0]
        for row in conn.execute("SELECT DISTINCT search_keyword FROM posts WHERE search_keyword <> ''")
        if row[0]
    }
    used.update(
        row[0]
        for row in conn.execute("SELECT search_keyword FROM keyword_runs WHERE completed_at <> ''")
        if row[0]
    )
    return used


def mark_keyword_started(conn: sqlite3.Connection, keyword: str) -> None:
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    conn.execute(
        """
        INSERT OR IGNORE INTO keyword_runs (search_keyword, started_at, completed_at)
        VALUES (?, ?, '')
        """,
        (keyword, now),
    )
    conn.commit()


def mark_keyword_completed(conn: sqlite3.Connection, keyword: str) -> None:
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    conn.execute(
        """
        UPDATE keyword_runs
        SET completed_at = ?
        WHERE search_keyword = ?
        """,
        (now, keyword),
    )
    conn.commit()


def mark_existing_http_429_rows(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        UPDATE posts
        SET error = ?
        WHERE error = ''
          AND (
            main_text LIKE '%HTTP ERROR 429%'
            OR main_text LIKE '%網頁無法正常運作%'
            OR main_text LIKE '%網站擁有者%'
            OR main_text LIKE '%問題仍未解決%'
          )
        """,
        (HTTP_429_ERROR,),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def saved_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM posts").fetchone()
    return int(row[0] or 0)


def last_saved_info(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT num, search_keyword, main_text
        FROM posts
        ORDER BY num DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return build_notice_info(None, "", "")

    return build_notice_info(row[0], row[1], row[2])


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
            search_keyword,
            main_text,
            post_time,
            scraped_at,
            error
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            source_url,
            post.get("search_keyword", "") or post.get("search_word", "") or "",
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


def save_raw_html_file(num: int | None, raw_html: str) -> None:
    if num is None or not raw_html:
        return

    directory = html_bucket_dir(num)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{num:06d}.html"
    path.write_text(make_browser_openable_html(raw_html), encoding="utf-8")


def html_bucket_dir(num: int) -> Path:
    bucket = ((num - 1) // 1000 + 1) * 1000
    return DEFAULT_HTML_DIR / f"{bucket:06d}"


def migrate_existing_html_files() -> None:
    if not DEFAULT_HTML_DIR.exists():
        return

    moved = 0
    for path in DEFAULT_HTML_DIR.glob("*.html"):
        if not path.stem.isdigit():
            continue

        num = int(path.stem)
        target_dir = html_bucket_dir(num)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        if target.exists():
            continue

        path.replace(target)
        moved += 1

    if moved:
        print(f"Moved existing HTML files into buckets: {moved}")


def print_saved(num: int | None, keyword: str, main_text: str) -> None:
    if num is None:
        return
    preview = " ".join((main_text or "").split())[:20]
    print(f"{num}. {keyword} {preview}")


def post_preview(main_text: str) -> str:
    return " ".join((main_text or "").split())[:20]


def build_notice_info(num: int | None, keyword: str, main_text: str) -> dict:
    return {
        "num": "" if num is None else str(num),
        "keyword": keyword or "",
        "preview": post_preview(main_text),
    }


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, conn: sqlite3.Connection):
        self.token = token
        self.chat_id = str(chat_id or "").strip()
        self.conn = conn
        self.enabled = bool(self.token and self.chat_id)
        self.last_new_at = time.monotonic()
        self.last_timeout_at = 0.0
        self.last_info = last_saved_info(conn)
        current_count = saved_count(conn)
        self.next_report_count = current_count + 100
        self.consecutive_failures = 0
        self.disabled_until = 0.0

    def send_text(self, text: str) -> None:
        if not self.enabled:
            return
        if time.monotonic() < self.disabled_until:
            return

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        text = text[: TELEGRAM_MAX_MESSAGE_LENGTH - 3] + "..." if len(text) > TELEGRAM_MAX_MESSAGE_LENGTH else text
        payload = urlencode({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        try:
            with urlopen(url, data=payload, timeout=TELEGRAM_SEND_TIMEOUT_SECONDS):
                pass
            self.consecutive_failures = 0
        except Exception as error:
            print(f"Telegram notification failed: {error}")
            self.consecutive_failures += 1
            if self.consecutive_failures >= TELEGRAM_MAX_CONSECUTIVE_FAILURES:
                self.disabled_until = time.monotonic() + TELEGRAM_FAILURE_COOLDOWN_SECONDS
                self.consecutive_failures = 0
                print("Telegram notifications paused for 5 minutes after repeated failures.")

    def format_message(self, label: str, info: dict, error: str = "") -> str:
        message = f"[{label}]({info.get('num', '')} {info.get('keyword', '')} {info.get('preview', '')}"
        if error:
            message += f"({error}"
        message += ")"
        return message

    def notify(self, label: str, info: dict | None = None, error: str = "") -> None:
        self.send_text(self.format_message(label, info or self.last_info, error=error))

    def mark_saved(self, info: dict) -> None:
        if not info.get("num"):
            return

        self.last_new_at = time.monotonic()
        self.last_info = info
        current_count = saved_count(self.conn)
        while current_count >= self.next_report_count:
            self.notify("正常匯報", info)
            self.next_report_count += 100

    def check_timeout(self, keyword: str) -> None:
        now = time.monotonic()
        if now - self.last_new_at < TELEGRAM_TIMEOUT_SECONDS:
            return
        if now - self.last_timeout_at < TELEGRAM_TIMEOUT_SECONDS:
            return

        info = self.last_info
        if not info.get("keyword"):
            info = build_notice_info(None, keyword, "")
        self.notify("超時", info)
        self.last_timeout_at = now

    def notify_finished(self) -> None:
        self.notify("執行完畢", self.last_info)

    def notify_started(self) -> None:
        self.notify("程式開始", self.last_info)


def build_rate_limited_message(pause_seconds: int, keyword: str, info: dict) -> str:
    return f"[被 Threads 暫時限流](暫停 {pause_seconds} 秒) {info.get('num', '')} {keyword}"


def handle_http_429_delay(keyword: str, notifier: TelegramNotifier | None) -> None:
    pause_seconds = random.randint(1, 180)
    info = notifier.last_info if notifier is not None else build_notice_info(None, keyword, "")
    message = build_rate_limited_message(pause_seconds, keyword, info)
    print(message)
    if notifier is not None:
        notifier.send_text(message)
    time.sleep(pause_seconds)


def configure_driver_timeouts(driver) -> None:
    try:
        driver.set_page_load_timeout(SELENIUM_COMMAND_TIMEOUT_SECONDS)
        driver.set_script_timeout(SELENIUM_COMMAND_TIMEOUT_SECONDS)
    except Exception as error:
        print(f"Could not configure browser timeouts: {error}")

    executor = getattr(driver, "command_executor", None)
    for setter_name in ("set_timeout", "set_socket_timeout"):
        setter = getattr(executor, setter_name, None)
        if callable(setter):
            try:
                setter(SELENIUM_COMMAND_TIMEOUT_SECONDS)
                return
            except Exception:
                pass

    client_config = getattr(executor, "_client_config", None)
    if client_config is not None and hasattr(client_config, "timeout"):
        try:
            client_config.timeout = SELENIUM_COMMAND_TIMEOUT_SECONDS
        except Exception:
            pass


def create_driver(profile_dir: str):
    try:
        driver = start_driver(profile_dir)
    except WebDriverException as error:
        raise SystemExit(
            "Could not start Chrome with chrome_profile. Close any Chrome window using this profile and try again.\n"
            f"{error}"
        ) from error

    configure_driver_timeouts(driver)
    return driver


def reset_driver(driver, profile_dir: str):
    close_driver(driver)
    driver = create_driver(profile_dir)
    driver.get("about:blank")
    search_handle = driver.current_window_handle
    switch_window(driver, search_handle, "search")
    return driver, search_handle


def close_driver(driver) -> None:
    try:
        driver.quit()
    except Exception as error:
        print(f"Could not quit Chrome cleanly: {error}")


def is_browser_connection_error(error: Exception) -> bool:
    text = str(error)
    markers = (
        "HTTPConnectionPool",
        "Read timed out",
        "Max retries exceeded",
        "Connection refused",
        "Failed to establish a new connection",
        "invalid session id",
        "no such window",
        "session deleted",
        "disconnected",
        "not connected to devtools",
        "browser has closed the connection",
        "chrome not reachable",
        "target window already closed",
        "web view not found",
    )
    return any(marker.lower() in text.lower() for marker in markers)


def record_error(
    conn: sqlite3.Connection,
    url: str,
    error: Exception | str,
    search_keyword: str = "",
    notifier: TelegramNotifier | None = None,
) -> None:
    error_post = build_error_post(url, error)
    error_post["search_keyword"] = search_keyword
    num = save_post(conn, error_post)
    print_saved(num, search_keyword, error_post.get("main_text", ""))
    info = build_notice_info(num, search_keyword, error_post.get("main_text", ""))
    if notifier is not None:
        notifier.notify("error", info, error=str(error))
        notifier.mark_saved(info)


def reached_limit(conn: sqlite3.Connection, limit: int) -> bool:
    return limit > 0 and saved_count(conn) >= limit


def is_http_429_page(text: str) -> bool:
    normalized = " ".join((text or "").split())
    lower = normalized.lower()
    return (
        "HTTP ERROR 429" in normalized
        or "429" in normalized and ("error" in lower or "err_" in lower)
        or "http error" in lower
        or "這個網頁無法正常運作" in normalized
        or "網頁無法正常運作" in normalized
        or "網站擁有者" in normalized
        or "問題仍未解決" in normalized
    )

def extract_post_without_date(
    driver,
    url: str,
    keyword: str,
    delay: float,
    notifier: TelegramNotifier | None = None,
) -> dict | None:
    if is_media_post_url(url):
        return None

    target_url = url.replace("https://www.threads.com/", "https://www.threads.net/")
    body_text = ""
    for attempt in range(1, POST_LOAD_RETRIES + 1):
        driver.get(target_url)
        time.sleep(delay * attempt)
        body_text = wait_for_body(driver)
        page_text = "\n".join([body_text, driver.title or "", driver.page_source or ""])
        if not is_http_429_page(page_text):
            break
        if attempt >= POST_LOAD_RETRIES:
            raise Http429PageError(HTTP_429_ERROR)
        handle_http_429_delay(keyword, notifier)

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


def visible_posts_driver(driver, keyword: str) -> list[dict]:
    posts = driver.execute_script(
        """
        const textOf = (node) => (node && node.innerText ? node.innerText.trim() : '');
        const htmlOf = (node) => (node && node.outerHTML ? node.outerHTML : '');
        const chooseCard = (anchor) => {
            let best = anchor;
            let node = anchor;
            for (let depth = 0; node && depth < 10; depth += 1, node = node.parentElement) {
                const text = textOf(node);
                if (text.length >= 20 && text.length <= 5000) {
                    best = node;
                }
                if (node.getAttribute && node.getAttribute('role') === 'article') {
                    return node;
                }
            }
            return best;
        };
        const seen = new Set();
        const result = [];
        for (const anchor of document.querySelectorAll("a[href*='/post/']")) {
            const href = anchor.href;
            if (!href || seen.has(href)) continue;
            seen.add(href);

            const card = chooseCard(anchor);
            const text = textOf(card);
            if (!text) continue;

            result.push({
                url: href,
                text,
                raw_html: htmlOf(card),
            });
        }
        return result;
        """
    ) or []
    result = []
    for post in posts:
        url = post.get("url", "")
        if not url or "threads." not in url:
            continue
        result.append(
            {
                "search_word": keyword,
                "url": normalize_post_url(url),
                "final_url": normalize_post_url(url),
                "text": post.get("text", ""),
                "raw_html": post.get("raw_html", ""),
                "view_count": "",
                "action_counts": {},
                "scraped_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )
    return result


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


def switch_window(driver, handle: str, name: str) -> None:
    try:
        driver.switch_to.window(handle)
    except Exception as error:
        raise RuntimeError(f"Could not switch to {name} window: {error}") from error


def process_post(
    conn: sqlite3.Connection,
    url: str,
    post: dict | None,
    downloaded: set[str],
    notifier: TelegramNotifier | None = None,
) -> None:
    if not post:
        return

    downloaded_post = build_downloaded_post(post)
    downloaded_post["search_keyword"] = post.get("search_word", "")
    num = save_post(conn, downloaded_post)
    save_raw_html_file(num, downloaded_post.get("raw_html", ""))
    downloaded.add(normalize_post_url(url))
    print_saved(num, downloaded_post.get("search_keyword", ""), downloaded_post.get("main_text", ""))
    info = build_notice_info(
        num,
        downloaded_post.get("search_keyword", ""),
        downloaded_post.get("main_text", ""),
    )
    if notifier is not None:
        notifier.mark_saved(info)


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
    seen: set[str],
    downloaded: set[str],
    max_idle_rounds: int,
    notifier: TelegramNotifier,
) -> None:
    print(f"Search word: {keyword}")
    switch_window(driver, search_handle, "search")
    driver.get(search_url(keyword))
    time.sleep(delay)
    idle_rounds = 0

    while idle_rounds < max_idle_rounds:
        if reached_limit(conn, limit):
            break
        notifier.check_timeout(keyword)

        try:
            wait_for_body(driver)
        except Exception:
            pass

        saved_before = saved_count(conn)
        for post in visible_posts_driver(driver, keyword):
            if reached_limit(conn, limit):
                break
            normalized_url = normalize_post_url(post.get("url", ""))
            if normalized_url in seen or is_media_post_url(normalized_url):
                continue
            seen.add(normalized_url)

            try:
                process_post(conn, normalized_url, post, downloaded, notifier=notifier)
            except Exception as error:
                record_error(conn, normalized_url, error, search_keyword=keyword, notifier=notifier)
                downloaded.add(normalized_url)

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
    notifier: TelegramNotifier,
) -> None:
    driver = create_driver(profile_dir)

    try:
        print(f"Saved posts: {saved_count(conn)}/{limit if limit > 0 else 'unlimited'}")
        downloaded = downloaded_source_urls(conn)
        seen = set(downloaded)

        driver.get("about:blank")
        search_handle = driver.current_window_handle
        switch_window(driver, search_handle, "search")

        for keyword in keywords:
            if reached_limit(conn, limit):
                break

            completed_keyword = False
            attempts = 0
            while attempts <= DRIVER_RESTART_RETRIES and not completed_keyword:
                attempts += 1
                mark_keyword_started(conn, keyword)
                try:
                    scrape_keyword(
                        conn,
                        driver,
                        keyword=keyword,
                        limit=limit,
                        delay=delay,
                        search_handle=search_handle,
                        seen=seen,
                        downloaded=downloaded,
                        max_idle_rounds=max_idle_rounds,
                        notifier=notifier,
                    )
                    completed_keyword = True
                except Exception as error:
                    if is_browser_connection_error(error) and attempts <= DRIVER_RESTART_RETRIES:
                        print(
                            f"Browser session was lost for {keyword}. "
                            f"Restarting ChromeDriver and retrying ({attempts}/{DRIVER_RESTART_RETRIES})."
                        )
                        try:
                            driver, search_handle = reset_driver(driver, profile_dir)
                        except Exception as reset_error:
                            if attempts >= DRIVER_RESTART_RETRIES:
                                raise BrowserRecoveryFailed(
                                    f"Could not restart ChromeDriver for {keyword}. Last error: {reset_error}"
                                ) from reset_error
                            print(f"ChromeDriver restart failed for {keyword}: {reset_error}")
                            time.sleep(delay)
                        continue

                    if is_browser_connection_error(error):
                        raise BrowserRecoveryFailed(
                            f"Browser session kept failing for {keyword} after "
                            f"{DRIVER_RESTART_RETRIES} restart attempts. Last error: {error}"
                        ) from error

                    print(f"Keyword failed: {keyword}: {error}")
                    error_url = f"https://www.threads.com/search_error/{quote(keyword, safe='')}"
                    record_error(conn, error_url, error, search_keyword=keyword, notifier=notifier)
                    completed_keyword = True

                mark_keyword_completed(conn, keyword)
    finally:
        close_driver(driver)


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
    fixed_429_rows = mark_existing_http_429_rows(conn)
    if fixed_429_rows:
        print(f"Marked existing HTTP 429 rows as error: {fixed_429_rows}")
    migrate_existing_html_files()
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, conn)
    notifier.notify_started()
    completed = False
    try:
        keywords = unique_keywords([args.keyword] if args.keyword else DEFAULT_SEARCH_WORDS)
        if args.keyword is None:
            used_keywords = used_search_keywords(conn)
            keywords = [keyword for keyword in keywords if keyword not in used_keywords]
            if used_keywords:
                print(f"Skipped used search words: {len(used_keywords)}")
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
            notifier=notifier,
        )
        completed = True
    finally:
        if completed:
            notifier.notify_finished()
        conn.close()


if __name__ == "__main__":
    main()
