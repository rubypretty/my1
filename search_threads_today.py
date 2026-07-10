import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import urlopen

from common import build_driver
from openpyxl import Workbook
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from websocket import create_connection


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DEFAULT_PROFILE_DIR = "chrome_profile"
DEFAULT_OUTPUT = "threads_search_today.json"
DEFAULT_URL_TABLE = BASE_DIR / "url_table.xlsx"
DEFAULT_SEARCH_WORD = "淨漢"
DEFAULT_S_DATE = datetime.now().astimezone().strftime("%Y%m%d")
DEFAULT_E_DATE = datetime.now().astimezone().strftime("%Y%m%d")
DEFAULT_MAX_N = 3


def load_local_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_search_settings() -> tuple[str, str, str, int]:
    search_word = os.getenv("SEARCH_WORD", DEFAULT_SEARCH_WORD)
    s_date = os.getenv("S_DATE", DEFAULT_S_DATE)
    e_date = os.getenv("E_DATE", DEFAULT_E_DATE)

    try:
        max_n = int(os.getenv("MAX_N", str(DEFAULT_MAX_N)))
    except ValueError as error:
        raise SystemExit("MAX_N in .env must be an integer.") from error

    return search_word, s_date, e_date, max_n


def normalize_post_url(url: str) -> str:
    parts = urlsplit(url.replace("https://www.threads.net/", "https://www.threads.com/"))
    path = parts.path.rstrip("/")
    return urlunsplit(("https", "www.threads.com", path, "hl=zh-tw", ""))


def parse_date(date_text: str) -> datetime:
    for date_format in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(date_text, date_format)
        except ValueError:
            pass
    raise ValueError(f"Invalid date: {date_text}. Use YYYYMMDD, YYYY-MM-DD, YYYY/MM/DD, or YYYY.MM.DD.")


def date_markers(day: datetime) -> set[str]:
    return {
        day.strftime("%Y-%m-%d"),
        day.strftime("%Y%m%d"),
        day.strftime("%Y/%m/%d"),
        day.strftime("%Y.%m.%d"),
        f"{day.year}-{day.month}-{day.day}",
        f"{day.year}/{day.month}/{day.day}",
        f"{day.year}.{day.month}.{day.day}",
    }


def date_markers_in_range(start_date: datetime, end_date: datetime) -> set[str]:
    markers = set()
    current = start_date
    while current <= end_date:
        markers.update(date_markers(current))
        current += timedelta(days=1)
    return markers


def looks_in_date_range(text: str, start_date: datetime, end_date: datetime) -> bool:
    if any(marker in text for marker in date_markers_in_range(start_date, end_date)):
        return True

    today = datetime.now().astimezone().replace(tzinfo=None)
    includes_today = start_date.date() <= today.date() <= end_date.date()
    if not includes_today:
        return False

    relative_patterns = [
        r"\b\d+\s*s\b",
        r"\b\d+\s*m\b",
        r"\b\d+\s*h\b",
        r"\b\d+\s*sec\b",
        r"\b\d+\s*min\b",
        r"\b\d+\s*hr\b",
        r"\d+\s*秒",
        r"\d+\s*分鐘",
        r"\d+\s*分",
        r"\d+\s*小時",
        r"剛剛",
        r"現在",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in relative_patterns)


def wait_for_body(driver: webdriver.Chrome, timeout: int = 20) -> str:
    wait = WebDriverWait(driver, timeout)
    wait.until(lambda current: current.find_elements(By.TAG_NAME, "body"))
    wait.until(lambda current: len((current.find_element(By.TAG_NAME, "body").text or "").strip()) > 20)
    return driver.find_element(By.TAG_NAME, "body").text or ""


def search_url(keyword: str) -> str:
    return f"https://www.threads.com/search?q={quote(keyword)}"


def collect_post_urls(driver: webdriver.Chrome, keyword: str, max_scrolls: int, delay: float) -> list[str]:
    driver.get(search_url(keyword))
    time.sleep(delay)

    urls: list[str] = []
    seen = set()
    for _ in range(max_scrolls):
        try:
            wait_for_body(driver)
        except TimeoutException:
            pass

        for link in driver.find_elements(By.CSS_SELECTOR, "a[href*='/post/']"):
            href = link.get_attribute("href")
            if not href or "threads." not in href:
                continue
            normalized = normalize_post_url(href)
            if normalized in seen:
                continue
            seen.add(normalized)
            urls.append(normalized)

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(delay)

    return urls


def extract_post(
    driver: webdriver.Chrome,
    url: str,
    keyword: str,
    start_date: datetime,
    end_date: datetime,
    delay: float,
) -> dict | None:
    driver.get(url.replace("https://www.threads.com/", "https://www.threads.net/"))
    time.sleep(delay)

    try:
        body_text = wait_for_body(driver)
    except TimeoutException:
        return None

    if keyword not in body_text:
        return None
    if not looks_in_date_range(body_text, start_date, end_date):
        return None

    return {
        "search_word": keyword,
        "url": normalize_post_url(url),
        "final_url": driver.current_url,
        "text": body_text,
        "matched_date_range": f"{start_date:%Y-%m-%d}~{end_date:%Y-%m-%d}",
        "scraped_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def save_results(posts: list[dict], output: Path) -> None:
    output.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")


def save_url_table(posts: list[dict], output: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "urls"
    headers = ["num", "url", "downloaded", "status", "found_from", "matched_date", "note", "updated_at"]
    sheet.append(headers)

    for index, post in enumerate(posts, start=1):
        sheet.append(
            [
                index,
                post.get("url", ""),
                "no",
                "open",
                f"search:{post.get('search_word', '')}",
                post.get("matched_date_range", post.get("matched_as_today", "")),
                "Matched search date range.",
                post.get("scraped_at", ""),
            ]
        )

    for column in sheet.columns:
        max_length = max(len(str(cell.value or "")) for cell in column)
        sheet.column_dimensions[column[0].column_letter].width = min(max(max_length + 2, 12), 80)

    workbook.save(output)


def read_debugger_address(profile_dir: str | Path) -> str | None:
    devtools_file = Path(profile_dir) / "DevToolsActivePort"
    if not devtools_file.exists():
        return None

    lines = devtools_file.read_text(encoding="utf-8").splitlines()
    if not lines:
        return None
    port = lines[0].strip()
    return f"127.0.0.1:{port}" if port else None


class CdpPage:
    def __init__(self, debugger_address: str):
        self.debugger_address = debugger_address
        self.ws = create_connection(self.get_page_websocket_url())
        self.next_id = 0
        self.call("Page.enable")
        self.call("Runtime.enable")

    def get_page_websocket_url(self) -> str:
        with urlopen(f"http://{self.debugger_address}/json/list", timeout=5) as response:
            targets = json.loads(response.read().decode("utf-8"))

        pages = [target for target in targets if target.get("type") == "page"]
        threads_pages = [target for target in pages if "threads." in target.get("url", "")]
        target = threads_pages[0] if threads_pages else pages[0]
        return target["webSocketDebuggerUrl"]

    def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        message = {"id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.ws.send(json.dumps(message))

        while True:
            response = json.loads(self.ws.recv())
            if response.get("id") == self.next_id:
                if "error" in response:
                    raise RuntimeError(response["error"])
                return response.get("result", {})

    def eval(self, expression: str):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        return result.get("result", {}).get("value")

    def navigate(self, url: str, delay: float) -> None:
        self.call("Page.navigate", {"url": url})
        time.sleep(delay)

    def wait_for_body(self, timeout: int = 20) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = self.eval("document.body ? document.body.innerText : ''") or ""
            if len(text.strip()) > 20:
                return text
            time.sleep(0.5)
        raise TimeoutException("Timed out waiting for body text.")

    def close(self) -> None:
        self.ws.close()


def collect_post_urls_cdp(page: CdpPage, keyword: str, max_scrolls: int, delay: float) -> list[str]:
    page.navigate(search_url(keyword), delay=delay)

    urls: list[str] = []
    seen = set()
    for _ in range(max_scrolls):
        try:
            page.wait_for_body()
        except TimeoutException:
            pass

        hrefs = page.eval(
            "Array.from(document.querySelectorAll(\"a[href*='/post/']\")).map((a) => a.href)"
        ) or []
        for href in hrefs:
            if not href or "threads." not in href:
                continue
            normalized = normalize_post_url(href)
            if normalized in seen:
                continue
            seen.add(normalized)
            urls.append(normalized)

        page.eval("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(delay)

    return urls


def extract_post_cdp(
    page: CdpPage,
    url: str,
    keyword: str,
    start_date: datetime,
    end_date: datetime,
    delay: float,
) -> dict | None:
    page.navigate(url.replace("https://www.threads.com/", "https://www.threads.net/"), delay=delay)

    try:
        body_text = page.wait_for_body()
    except TimeoutException:
        return None

    if keyword not in body_text:
        return None
    if not looks_in_date_range(body_text, start_date, end_date):
        return None

    final_url = page.eval("location.href") or url
    return {
        "search_word": keyword,
        "url": normalize_post_url(url),
        "final_url": final_url,
        "text": body_text,
        "matched_date_range": f"{start_date:%Y-%m-%d}~{end_date:%Y-%m-%d}",
        "scraped_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def run_with_cdp(
    debugger_address: str,
    keyword: str,
    start_date: datetime,
    end_date: datetime,
    limit: int,
    max_scrolls: int,
    delay: float,
    output: Path,
    url_table: Path,
) -> bool:
    try:
        page = CdpPage(debugger_address)
    except Exception as error:
        print(f"Could not use existing Chrome through CDP: {error}")
        return False

    try:
        print(f"Using existing Chrome through CDP: {debugger_address}")
        candidate_urls = collect_post_urls_cdp(page, keyword, max_scrolls=max_scrolls, delay=delay)
        print(f"Candidate posts: {len(candidate_urls)}")

        posts = []
        for url in candidate_urls:
            if len(posts) >= limit:
                break
            print(f"Checking: {url}")
            post = extract_post_cdp(
                page,
                url,
                keyword=keyword,
                start_date=start_date,
                end_date=end_date,
                delay=delay,
            )
            if post:
                posts.append(post)
                print(f"  kept: {len(posts)}/{limit}")

        save_results(posts, output)
        save_url_table(posts, url_table)
        print(f"Saved {len(posts)} post(s): {output.resolve()}")
        print(f"Saved URL table: {url_table.resolve()}")
        return True
    finally:
        page.close()


def start_driver(profile_dir: str | Path) -> webdriver.Chrome:
    debugger_address = read_debugger_address(profile_dir)
    if debugger_address:
        try:
            print(f"Attaching to existing Chrome: {debugger_address}")
            return build_driver(headless=False, debugger_address=debugger_address)
        except WebDriverException:
            print("Could not attach to existing Chrome. Starting a new Chrome session.")

    return build_driver(headless=False, user_data_dir=profile_dir)


def main() -> None:
    load_local_env()
    search_word, s_date, e_date, max_n = get_search_settings()

    parser = argparse.ArgumentParser(description="Search Threads posts by keyword and date range.")
    parser.add_argument("--keyword", default=search_word, help=f"Search keyword. Default: {search_word}")
    parser.add_argument("--s-date", default=s_date, help=f"Search start date. Default: {s_date}")
    parser.add_argument("--e-date", default=e_date, help=f"Search end date. Default: {e_date}")
    parser.add_argument("--limit", "--max-n", dest="limit", type=int, default=max_n, help=f"Maximum posts to keep. Default: {max_n}")
    parser.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR, help=f"Chrome profile directory. Default: {DEFAULT_PROFILE_DIR}")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"JSON output path. Default: {DEFAULT_OUTPUT}")
    parser.add_argument("--url-table", default=DEFAULT_URL_TABLE, help=f"Excel URL table path. Default: {DEFAULT_URL_TABLE}")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay after page actions. Default: 2")
    parser.add_argument("--max-scrolls", type=int, default=12, help="Maximum search result scrolls. Default: 12")
    args = parser.parse_args()

    start_date = parse_date(args.s_date)
    end_date = parse_date(args.e_date)
    if start_date > end_date:
        raise SystemExit("s_date must be earlier than or equal to e_date.")

    output = Path(args.output)
    url_table = Path(args.url_table)
    debugger_address = read_debugger_address(args.profile_dir)

    print(f"Search word: {args.keyword}")
    print(f"Date range: {start_date:%Y-%m-%d} ~ {end_date:%Y-%m-%d}")
    print(f"Max posts: {args.limit}")
    if debugger_address and run_with_cdp(
        debugger_address,
        keyword=args.keyword,
        start_date=start_date,
        end_date=end_date,
        limit=args.limit,
        max_scrolls=args.max_scrolls,
        delay=args.delay,
        output=output,
        url_table=url_table,
    ):
        return

    try:
        driver = start_driver(args.profile_dir)
    except WebDriverException as error:
        raise SystemExit(
            "Could not start Chrome with chrome_profile. Close any Chrome window using this profile and try again.\n"
            f"{error}"
        ) from error

    try:
        candidate_urls = collect_post_urls(driver, args.keyword, max_scrolls=args.max_scrolls, delay=args.delay)
        print(f"Candidate posts: {len(candidate_urls)}")

        posts = []
        for url in candidate_urls:
            if len(posts) >= args.limit:
                break
            print(f"Checking: {url}")
            post = extract_post(
                driver,
                url,
                keyword=args.keyword,
                start_date=start_date,
                end_date=end_date,
                delay=args.delay,
            )
            if post:
                posts.append(post)
                print(f"  kept: {len(posts)}/{args.limit}")

        save_results(posts, output)
        save_url_table(posts, url_table)
        print(f"Saved {len(posts)} post(s): {output.resolve()}")
        print(f"Saved URL table: {url_table.resolve()}")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
