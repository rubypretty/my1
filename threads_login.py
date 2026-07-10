import argparse
import os
import time
from pathlib import Path

from common import build_driver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


ENV_FILE = Path(".env")
DEFAULT_LOGIN_URL = "https://www.threads.com/login"
DEFAULT_PROFILE_DIR = "chrome_profile"


def load_local_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_credentials() -> tuple[str, str]:
    account = os.getenv("THREADS_ACCOUNT") or os.getenv("my_account")
    password = os.getenv("THREADS_PASSWORD") or os.getenv("my_password")

    if not account:
        raise SystemExit("Missing account in .env. Add THREADS_ACCOUNT or my_account.")
    if not password:
        raise SystemExit("Missing password in .env. Add THREADS_PASSWORD or my_password.")

    return account, password


def find_first_visible(driver, selectors: list[str], timeout: int = 20):
    wait = WebDriverWait(driver, timeout)
    last_error = None
    for selector in selectors:
        try:
            return wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, selector)))
        except TimeoutException as error:
            last_error = error
    raise last_error or TimeoutException("No visible element found.")


def open_login_form(driver, login_url: str) -> None:
    driver.get(login_url)

    try:
        find_first_visible(driver, ["input[name='username']", "input[autocomplete='username']"], timeout=8)
        return
    except TimeoutException:
        pass

    login_buttons = driver.find_elements(By.XPATH, "//a[contains(., 'Instagram') or contains(., 'Log in') or contains(., '登入')] | //button[contains(., 'Instagram') or contains(., 'Log in') or contains(., '登入')]")
    for button in login_buttons:
        if button.is_displayed() and button.is_enabled():
            button.click()
            break


def submit_login(driver, account: str, password: str) -> None:
    username_input = find_first_visible(
        driver,
        [
            "input[name='username']",
            "input[autocomplete='username']",
            "input[type='text']",
            "input[type='email']",
        ],
    )
    password_input = find_first_visible(
        driver,
        [
            "input[name='password']",
            "input[autocomplete='current-password']",
            "input[type='password']",
        ],
    )

    username_input.clear()
    username_input.send_keys(account)
    password_input.clear()
    password_input.send_keys(password)
    password_input.send_keys(Keys.ENTER)


def wait_for_login_result(driver, timeout: int = 120) -> None:
    wait = WebDriverWait(driver, timeout)
    wait.until(
        lambda current: (
            "accounts/login" not in current.current_url
            and "/login" not in current.current_url
        )
        or current.find_elements(By.CSS_SELECTOR, "input[name='verificationCode'], input[autocomplete='one-time-code']")
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Log in to Threads and save the browser session in a Chrome profile.")
    parser.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR, help=f"Chrome profile directory. Default: {DEFAULT_PROFILE_DIR}")
    parser.add_argument("--login-url", default=DEFAULT_LOGIN_URL, help=f"Login URL. Default: {DEFAULT_LOGIN_URL}")
    parser.add_argument("--keep-open", type=int, default=300, help="Seconds to keep Chrome open after login. Default: 300")
    args = parser.parse_args()

    load_local_env()
    account, password = read_credentials()

    driver = build_driver(headless=False, user_data_dir=args.profile_dir)
    try:
        print(f"Opening Threads login for account: {account}")
        open_login_form(driver, args.login_url)
        submit_login(driver, account, password)
        print("Login submitted. Complete any verification in the browser if it appears.")

        try:
            wait_for_login_result(driver)
            print(f"Current URL: {driver.current_url}")
        except TimeoutException:
            print("Still waiting on login or verification. The browser will stay open.")

        print(f"Session profile saved in: {Path(args.profile_dir).resolve()}")
        time.sleep(max(args.keep_open, 0))
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
