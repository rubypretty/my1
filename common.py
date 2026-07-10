from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium_stealth import stealth


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
