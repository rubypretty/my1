import argparse
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium_stealth import stealth

import SEVENETEEN_SEARCH_WORDS as settings


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DEFAULT_DB_PATH = BASE_DIR / "seventeen.sqlite3"
DEFAULT_HTML_DIR = BASE_DIR / "seventeen.html"
DEFAULT_PROFILE_DIR = "chrome_profile"
DEFAULT_SEARCH_WORDS = list(getattr(settings, "SEARCH_WORDS", ["seventeen"]))
DEFAULT_SEARCH_WORD = DEFAULT_SEARCH_WORDS[0]
DEFAULT_MAX_N = int(getattr(settings, "MAX_N", 100000))
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
OUTPUT_TIME_FORMAT = "%Y.%m.%d %H:%M"
MIN_CHINESE_CHARS = 2
MIN_CHINESE_RATIO = 0.30
MAX_KANA_RATIO = 0.20
EXCLUDE_PHRASES = ("AI 資訊", "尚無回覆", "查看動態")


class Http429PageError(RuntimeError):
    pass


class BrowserRecoveryFailed(RuntimeError):
    pass


def load_local_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def normalize_post_url(url: str) -> str:
    parts = urlsplit(url.replace("https://www.threads.net/", "https://www.threads.com/"))
    path = parts.path.rstrip("/")
    return urlunsplit(("https", "www.threads.com", path, "hl=zh-tw", ""))


def is_media_post_url(url: str) -> bool:
    parts = urlsplit(url.replace("https://www.threads.net/", "https://www.threads.com/"))
    return parts.path.rstrip("/").endswith("/media")


def wait_for_body(driver: webdriver.Chrome, timeout: int = 20) -> str:
    wait = WebDriverWait(driver, timeout)
    wait.until(lambda current: current.find_elements(By.TAG_NAME, "body"))
    wait.until(lambda current: len((current.find_element(By.TAG_NAME, "body").text or "").strip()) > 20)
    return driver.find_element(By.TAG_NAME, "body").text or ""


def normalize_view_count(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def format_count_number(text: str) -> str:
    normalized = normalize_view_count(text)
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(億|萬|千|k|m|b)?", normalized, flags=re.IGNORECASE)
    if not match:
        return normalized

    amount = float(match.group(1).replace(",", ""))
    unit = (match.group(2) or "").lower()
    multipliers = {
        "千": 1_000,
        "萬": 10_000,
        "億": 100_000_000,
        "k": 1_000,
        "m": 1_000_000,
        "b": 1_000_000_000,
    }
    value = int(round(amount * multipliers.get(unit, 1)))
    return f"{value:,}"


def format_view_count(text: str) -> str:
    normalized = normalize_view_count(text)
    return format_count_number(normalized) if looks_like_view_count(normalized) else normalized


def looks_like_view_count(text: str) -> bool:
    normalized = normalize_view_count(text).lower()
    return bool(normalized) and (
        "\u6b21\u700f\u89bd" in normalized
        or "\u700f\u89bd\u6b21\u6578" in normalized
        or "view" in normalized
    )


def clean_lines(text: str) -> list[str]:
    stop_markers = [
        "登入即可查看更多",
        "登入查看更多 Threads",
        "© 2026",
        "Threads 使用條款",
    ]
    for marker in stop_markers:
        if marker in text:
            text = text.split(marker, 1)[0].strip()

    noise_lines = {"登入", "註冊", "搜尋", "Threads"}
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line in noise_lines:
            continue
        lines.append(line)
    return lines


def extract_author_from_url(url: str) -> str:
    match = re.search(r"/@([^/?#]+)/post/", url)
    return match.group(1) if match else ""


def looks_like_metric(line: str) -> bool:
    return bool(re.fullmatch(r"\d[\d,]*", line))


def looks_like_time(line: str) -> bool:
    if re.fullmatch(r"\d+\s*(秒|分鐘|小時|天|週|月|年|s|m|h|sec|min|hr|d|w)", line, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", line):
        return True
    return line in {"昨天", "前天"}


def format_post_time(post_time: str, reference_time: datetime | None = None) -> str:
    if not post_time:
        return ""

    reference_time = reference_time or datetime.now().astimezone()
    absolute_match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", post_time)
    if absolute_match:
        year, month, day = (int(part) for part in absolute_match.groups())
        return datetime(year, month, day, tzinfo=reference_time.tzinfo).strftime(OUTPUT_TIME_FORMAT)

    relative_match = re.fullmatch(
        r"(\d+)\s*(秒|分鐘|小時|天|週|月|年|s|m|h|sec|min|hr|d|w)",
        post_time,
        flags=re.IGNORECASE,
    )
    if relative_match:
        amount = int(relative_match.group(1))
        unit = relative_match.group(2).lower()
        deltas = {
            "秒": timedelta(seconds=amount),
            "s": timedelta(seconds=amount),
            "sec": timedelta(seconds=amount),
            "分鐘": timedelta(minutes=amount),
            "m": timedelta(minutes=amount),
            "min": timedelta(minutes=amount),
            "小時": timedelta(hours=amount),
            "h": timedelta(hours=amount),
            "hr": timedelta(hours=amount),
            "天": timedelta(days=amount),
            "d": timedelta(days=amount),
            "週": timedelta(weeks=amount),
            "w": timedelta(weeks=amount),
            "月": timedelta(days=amount * 30),
            "年": timedelta(days=amount * 365),
        }
        return (reference_time - deltas[unit]).strftime(OUTPUT_TIME_FORMAT)

    if post_time == "昨天":
        return (reference_time - timedelta(days=1)).strftime(OUTPUT_TIME_FORMAT)
    if post_time == "前天":
        return (reference_time - timedelta(days=2)).strftime(OUTPUT_TIME_FORMAT)
    return post_time


def drop_carousel_markers(lines: list[str]) -> list[str]:
    cleaned = []
    index = 0
    while index < len(lines):
        if (
            index + 2 < len(lines)
            and lines[index].isdigit()
            and lines[index + 1] == "/"
            and lines[index + 2].isdigit()
        ):
            index += 3
            continue
        cleaned.append(lines[index])
        index += 1
    return cleaned


def find_reply_start(lines: list[str], start: int) -> int | None:
    for index in range(start, len(lines)):
        line = lines[index]
        if line.startswith("回覆"):
            return index + 1
        if line in {"回覆", "更多回覆"}:
            return index
    return None


def find_metric_start(lines: list[str], start: int, stop: int) -> int | None:
    for index in range(start, stop):
        if looks_like_metric(lines[index]):
            metric_count = 0
            for line in lines[index : min(stop, index + 6)]:
                if looks_like_metric(line):
                    metric_count += 1
            if metric_count >= 2:
                return index
    return None


def find_metric_end(lines: list[str], start: int, stop: int) -> int:
    index = start
    while index < stop and looks_like_metric(lines[index]):
        index += 1
    return index


def trim_ui_lines(lines: list[str]) -> list[str]:
    return [
        line
        for line in lines
        if line not in {"回覆", "更多回覆", "查看翻譯"} and not line.startswith("回覆")
    ]


def parse_thread_lines(
    lines: list[str],
    source_url: str = "",
    reference_time: datetime | None = None,
) -> dict:
    if not lines:
        return {}

    lines = drop_carousel_markers(lines)
    author_from_url = extract_author_from_url(source_url)

    view_index = next((index for index, line in enumerate(lines) if looks_like_view_count(line)), None)
    view_count = format_view_count(lines[view_index]) if view_index is not None else ""

    author_index = None
    if author_from_url:
        author_index = next((index for index, line in enumerate(lines) if line == author_from_url), None)
    if author_index is None:
        start = (view_index + 1) if view_index is not None else 0
        author_index = start if start < len(lines) else None

    author = author_from_url or (lines[author_index] if author_index is not None else "")

    post_time = ""
    time_index = None
    if author_index is not None:
        for index in range(author_index + 1, min(len(lines), author_index + 6)):
            if looks_like_time(lines[index]):
                post_time = lines[index]
                time_index = index
                break

    text_start = (time_index + 1) if time_index is not None else ((author_index + 1) if author_index is not None else 0)
    reply_start = find_reply_start(lines, text_start)
    main_stop = reply_start - 1 if reply_start is not None else len(lines)
    metric_start = find_metric_start(lines, text_start, main_stop)
    if metric_start is not None:
        main_stop = metric_start

    main_lines = trim_ui_lines(lines[text_start:main_stop])

    metrics_stop = reply_start - 1 if reply_start is not None else len(lines)
    metric_source_start = metric_start if metric_start is not None else text_start
    metric_end = find_metric_end(lines, metric_source_start, metrics_stop)
    metrics = [
        line
        for line in lines[metric_source_start:metric_end]
        if looks_like_metric(line)
    ]

    if reply_start is not None:
        extra_lines = trim_ui_lines(lines[reply_start:])
    elif metric_start is not None:
        extra_lines = trim_ui_lines(lines[metric_end:])
    else:
        extra_lines = []

    return {
        "author": author,
        "post_time": format_post_time(post_time, reference_time),
        "view_count": view_count,
        "main_text": "\n".join(main_lines).strip(),
        "愛心": metrics[0] if len(metrics) > 0 else "",
        "留言": metrics[1] if len(metrics) > 1 else "",
        "轉發": metrics[2] if len(metrics) > 2 else "",
        "分享": "",
        "related_or_replies": "\n".join(extra_lines).strip(),
    }


def search_url(keyword: str) -> str:
    return f"https://www.threads.com/search?q={quote(keyword)}"


def build_downloaded_post(post: dict) -> dict:
    scraped_at = datetime.now().astimezone()
    text = (post.get("text") or "").strip()
    lines = clean_lines(text)
    cleaned_text = "\n".join(lines).strip()
    parsed = parse_thread_lines(
        lines,
        source_url=post.get("url", ""),
        reference_time=scraped_at,
    )
    if post.get("view_count"):
        parsed["view_count"] = post["view_count"]
    for key, value in (post.get("action_counts") or {}).items():
        if value:
            parsed[key] = value

    return {
        "source_url": post.get("url", ""),
        "final_url": post.get("final_url", ""),
        **parsed,
        "text": cleaned_text or text,
        "raw_html": post.get("raw_html", post.get("raw_xml", "")),
        "error": "" if cleaned_text else "No readable public post text found.",
        "scraped_at": scraped_at.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def build_error_post(url: str, error: Exception | str) -> dict:
    scraped_at = datetime.now().astimezone()
    return {
        "source_url": normalize_post_url(url) if url else "",
        "final_url": "",
        "author": extract_author_from_url(url),
        "post_time": "",
        "view_count": "",
        "main_text": "",
        "愛心": "",
        "留言": "",
        "轉發": "",
        "分享": "",
        "related_or_replies": "",
        "text": "",
        "raw_html": "",
        "error": f"{type(error).__name__}: {error}" if isinstance(error, Exception) else str(error),
        "scraped_at": scraped_at.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def make_browser_openable_html(raw_html: str) -> str:
    html = raw_html or "<!doctype html><html><body></body></html>"
    stripped = html.lstrip().lower()
    if not stripped.startswith("<!doctype") and stripped.startswith("<html"):
        html = "<!doctype html>\n" + html
    return html


def read_debugger_address(profile_dir: str | Path) -> str | None:
    devtools_file = Path(profile_dir) / "DevToolsActivePort"
    if not devtools_file.exists():
        return None

    lines = devtools_file.read_text(encoding="utf-8").splitlines()
    if not lines:
        return None
    port = lines[0].strip()
    return f"127.0.0.1:{port}" if port else None


def remove_stale_debugger_file(profile_dir: str | Path) -> None:
    devtools_file = Path(profile_dir) / "DevToolsActivePort"
    if not devtools_file.exists():
        return

    try:
        devtools_file.unlink()
    except OSError as error:
        print(f"Could not remove stale DevToolsActivePort: {error}")


def build_driver(
    headless: bool = False,
    user_data_dir: str | Path | None = None,
    debugger_address: str | None = None,
) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")

    if debugger_address:
        options.debugger_address = debugger_address

    if user_data_dir and not debugger_address:
        profile_path = Path(user_data_dir).resolve()
        profile_path.mkdir(parents=True, exist_ok=True)
        options.add_argument(f"--user-data-dir={profile_path}")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--start-maximized")
    options.add_argument("--lang=zh-TW")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=options)
    stealth(
        driver,
        languages=["zh-TW", "zh", "en-US", "en"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel(R) UHD Graphics",
        fix_hairline=True,
    )
    return driver


def start_driver(profile_dir: str | Path) -> webdriver.Chrome:
    debugger_address = read_debugger_address(profile_dir)
    if debugger_address:
        try:
            print(f"Attaching to existing Chrome: {debugger_address}")
            return build_driver(headless=False, debugger_address=debugger_address)
        except WebDriverException:
            print("Could not attach to existing Chrome. Starting a new Chrome session.")
            remove_stale_debugger_file(profile_dir)

    return build_driver(headless=False, user_data_dir=profile_dir)


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


def should_save_downloaded_post(post: dict) -> bool:
    main_text = post.get("main_text", "") or ""
    if any(phrase in main_text for phrase in EXCLUDE_PHRASES):
        return False

    return is_chinese_text(main_text)


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
        raise BrowserRecoveryFailed(
            "Could not start Chrome with chrome_profile. "
            "Close any Chrome/ChromeDriver window using this profile and try again.\n"
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
    if driver is None:
        return

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


def recover_driver(driver, profile_dir: str, keyword: str, delay: float):
    last_error = None
    for attempt in range(1, DRIVER_RESTART_RETRIES + 1):
        try:
            print(
                f"Restarting ChromeDriver for {keyword} "
                f"({attempt}/{DRIVER_RESTART_RETRIES})."
            )
            return reset_driver(driver, profile_dir)
        except Exception as error:
            last_error = error
            driver = None
            print(f"ChromeDriver restart failed for {keyword}: {error}")
            time.sleep(delay)

    print(f"Could not restart ChromeDriver for {keyword}. Last error: {last_error}")
    return None, None


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
    if not should_save_downloaded_post(downloaded_post):
        downloaded.add(normalize_post_url(url))
        return

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
    driver = None
    search_handle = None

    try:
        print(f"Saved posts: {saved_count(conn)}/{limit if limit > 0 else 'unlimited'}")
        downloaded = downloaded_source_urls(conn)
        seen = set(downloaded)

        driver, search_handle = recover_driver(driver, profile_dir, "initial browser", delay)

        for keyword in keywords:
            if reached_limit(conn, limit):
                break

            completed_keyword = False
            attempts = 0
            while attempts <= DRIVER_RESTART_RETRIES and not completed_keyword:
                attempts += 1
                mark_keyword_started(conn, keyword)
                try:
                    if driver is None or search_handle is None:
                        driver, search_handle = recover_driver(driver, profile_dir, keyword, delay)
                        if driver is None or search_handle is None:
                            error_url = f"https://www.threads.com/search_error/{quote(keyword, safe='')}"
                            record_error(
                                conn,
                                error_url,
                                "Could not restart ChromeDriver. Skipping this keyword.",
                                search_keyword=keyword,
                                notifier=notifier,
                            )
                            mark_keyword_completed(conn, keyword)
                            completed_keyword = True
                            break

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
                        driver, search_handle = recover_driver(driver, profile_dir, keyword, delay)
                        continue

                    if is_browser_connection_error(error):
                        error_message = (
                            f"Browser session kept failing for {keyword} after "
                            f"{DRIVER_RESTART_RETRIES} restart attempts. Last error: {error}"
                        )
                        print(error_message)
                        error_url = f"https://www.threads.com/search_error/{quote(keyword, safe='')}"
                        record_error(conn, error_url, error_message, search_keyword=keyword, notifier=notifier)
                        driver, search_handle = recover_driver(driver, profile_dir, keyword, delay)
                        mark_keyword_completed(conn, keyword)
                        completed_keyword = True
                        break

                    print(f"Keyword failed: {keyword}: {error}")
                    error_url = f"https://www.threads.com/search_error/{quote(keyword, safe='')}"
                    record_error(conn, error_url, error, search_keyword=keyword, notifier=notifier)
                    driver, search_handle = recover_driver(driver, profile_dir, keyword, delay)
                    mark_keyword_completed(conn, keyword)
                    completed_keyword = True

                mark_keyword_completed(conn, keyword)
    finally:
        close_driver(driver)


def main() -> None:
    load_local_env()

    parser = argparse.ArgumentParser(description="Search Threads posts containing seventeen and save them to SQLite.")
    parser.add_argument("--keyword", default=None, help="Search one keyword only. Default: use SEARCH_WORDS from SEVENETEEN_SEARCH_WORDS.py")
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
