"""
WhatsApp Web automation (headless Chromium via Selenium).

Every public function blocks — call it through asyncio.to_thread.
One browser per process, one job at a time, so no locking beyond driver
creation is needed.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

import config

log = logging.getLogger("wa")

# ---------------------------------------------------------------- selectors
COMPOSE_XPATHS = [
    "//footer//div[@contenteditable='true']",
    "//div[@contenteditable='true'][@data-tab='10']",
    "//div[@contenteditable='true'][@data-tab='6']",
]

MENU_BUTTON_XPATHS = [
    "//header//div[@aria-label='Menu']",
    "//header//div[@title='Menu']",
    "//header//div[@aria-label='Menu options']",
    "//header//div[@role='button'][.//span[@data-icon='menu']]",
    "//header//span[@data-icon='menu']/ancestor::div[@role='button'][1]",
]

MENU_READY_XPATHS = [
    "//div[@role='menu']",
    "//div[@role='menuitem']",
    "//ul//li",
]

REPORT_ITEM_XPATHS = [
    "//div[@role='menuitem'][normalize-space(.)='Report']",
    "//div[@role='menuitem'][contains(.,'Report')]",
    "//ul//li[normalize-space(.)='Report']",
    "//ul//li[contains(.,'Report')]",
]

PANEL_REPORT_XPATHS = [
    "//div[@role='button'][normalize-space(.)='Report']",
    "//div[@role='button'][contains(.,'Report')]",
    "//span[normalize-space(.)='Report']/ancestor::div[@role='button'][1]",
]

DIALOG_XPATHS = ["//div[@role='dialog']"]

CONFIRM_XPATHS = [
    "//div[@role='dialog']//div[@role='button'][normalize-space(.)='Report']",
    "//div[@role='dialog']//button[normalize-space(.)='Report']",
    "//div[@role='dialog']//div[@role='button'][contains(.,'Report')]",
    "//div[@role='dialog']//button[contains(.,'Report')]",
]

LINK_PHONE_XPATHS = [
    "//div[@role='button'][contains(.,'Link with phone number')]",
    "//a[contains(.,'Link with phone number')]",
    "//*[contains(text(),'Link with phone number')]",
]

NEXT_XPATHS = [
    "//div[@role='button'][normalize-space(.)='Next']",
    "//button[normalize-space(.)='Next']",
    "//div[@role='button'][contains(.,'Next')]",
    "//button[contains(.,'Next')]",
]

QR_XPATHS = ["//div[@data-ref]", "//canvas"]


class LoginRequired(RuntimeError):
    """The WhatsApp session is gone."""


class DriverBroken(RuntimeError):
    """The browser is dead and must be rebuilt."""


# ------------------------------------------------------------------ driver
_lock = threading.Lock()
_driver = None


def _alive(driver) -> bool:
    try:
        driver.current_url
        return True
    except WebDriverException:
        return False


def _clean_locks() -> None:
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(config.DATA_DIR, name)
        try:
            if os.path.islink(path) or os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass


def _build():
    opts = Options()
    opts.page_load_strategy = "eager"
    for arg in (
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=Translate,MediaRouter,OptimizationHints,CalculateNativeWinOcclusion",
        "--hide-scrollbars",
        "--mute-audio",
        "--window-size=1280,800",
        "--lang=en-US",
        f"--user-agent={config.UA}",
        f"--user-data-dir={config.DATA_DIR}",
    ):
        opts.add_argument(arg)

    if config.CHROME_LOW_MEMORY:
        for arg in (
            "--single-process",
            "--no-zygote",
            "--renderer-process-limit=1",
            "--js-flags=--max-old-space-size=192",
        ):
            opts.add_argument(arg)

    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    path = os.environ.get("CHROMEDRIVER_PATH")
    driver = webdriver.Chrome(service=Service(path) if path else Service(), options=opts)
    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
        )
    except WebDriverException:
        pass
    return driver


def get_driver():
    global _driver
    with _lock:
        if _driver is not None and _alive(_driver):
            return _driver
        _quit_locked()
        os.makedirs(config.DATA_DIR, exist_ok=True)
        _clean_locks()
        try:
            _driver = _build()
        except Exception as exc:
            raise DriverBroken(f"browser failed to start: {exc}") from exc
        log.info("browser started")
        return _driver


def _quit_locked() -> None:
    global _driver
    if _driver is not None:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None


def reset_driver() -> None:
    with _lock:
        _quit_locked()
        _clean_locks()


def recover_driver():
    reset_driver()
    return get_driver()


def shutdown() -> None:
    reset_driver()


# ---------------------------------------------------------------- helpers
def _visible(driver, by, selector):
    try:
        for el in driver.find_elements(by, selector):
            if el.is_displayed():
                return el
    except WebDriverException:
        pass
    return None


def _click(driver, el) -> bool:
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except Exception:
        pass
    try:
        el.click()
        return True
    except WebDriverException:
        pass
    try:
        driver.execute_script("arguments[0].click();", el)
        return True
    except Exception:
        return False


def _wait_click(driver, xpaths, timeout) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for xp in xpaths:
            el = _visible(driver, By.XPATH, xp)
            if el is not None and _click(driver, el):
                return True
        time.sleep(0.3)
    return False


def _wait_present(driver, xpaths, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for xp in xpaths:
            el = _visible(driver, By.XPATH, xp)
            if el is not None:
                return el
        time.sleep(0.25)
    return None


def _wait_gone(driver, xpaths, timeout) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _wait_present(driver, xpaths, 0.2) is None:
            return True
        time.sleep(0.2)
    return False


def _body_text(driver) -> str:
    try:
        return driver.find_element(By.TAG_NAME, "body").text.lower()
    except WebDriverException:
        return ""


def screenshot(driver) -> bytes | None:
    try:
        return driver.get_screenshot_as_png()
    except Exception:
        return None


def _qr_png(driver) -> bytes | None:
    for xp in QR_XPATHS:
        el = _visible(driver, By.XPATH, xp)
        if el is not None:
            try:
                png = el.screenshot_as_png
                if png:
                    return png
            except Exception:
                pass
    return screenshot(driver)


def is_logged_in(driver) -> bool:
    try:
        if driver.find_elements(By.ID, "pane-side"):
            return True
        if driver.find_elements(By.CSS_SELECTOR, "div[aria-label='Chat list']"):
            return True
    except WebDriverException:
        pass
    return False


def _login_screen(driver) -> bool:
    return _visible(driver, By.XPATH, QR_XPATHS[0]) is not None or _visible(
        driver, By.XPATH, QR_XPATHS[1]
    ) is not None


def _wait_session(driver, seconds):
    """True = logged in, False = login screen, None = neither."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if is_logged_in(driver):
            return True
        if _login_screen(driver):
            return False
        time.sleep(0.4)
    return None


def _open(driver, url) -> None:
    try:
        driver.get(url)
    except TimeoutException:
        pass


# -------------------------------------------------------------------- QR
def qr_login(driver, send_qr) -> bool:
    """Drives the QR flow. send_qr(png, attempt) is called for each fresh code."""
    for attempt in range(1, config.QR_ATTEMPTS + 1):
        _open(driver, config.WA_URL)
        if _wait_session(driver, config.PAGE_LOAD_TIMEOUT) is True:
            return True

        if _wait_present(driver, QR_XPATHS, 20) is None:
            if is_logged_in(driver):
                return True
            continue

        time.sleep(1.2)
        png = _qr_png(driver)
        if png:
            send_qr(png, attempt)

        deadline = time.time() + config.QR_WAIT
        while time.time() < deadline:
            if is_logged_in(driver):
                return True
            time.sleep(1)
        log.info("QR attempt %s expired", attempt)
    return False


# ------------------------------------------------------------- pair code
def _phone_input(driver):
    fallback = None
    try:
        for el in driver.find_elements(By.XPATH, "//input"):
            if not el.is_displayed():
                continue
            label = " ".join(
                filter(
                    None,
                    (
                        el.get_attribute("aria-label"),
                        el.get_attribute("placeholder"),
                        el.get_attribute("title"),
                    ),
                )
            ).lower()
            if any(k in label for k in ("phone", "number", "whatsapp")):
                return el
            fallback = el
    except WebDriverException:
        pass
    return fallback


def _read_pair_code(driver):
    try:
        for el in driver.find_elements(By.XPATH, "//div|//span"):
            try:
                if not el.is_displayed():
                    continue
                text = el.text.strip()
            except WebDriverException:
                continue
            if re.fullmatch(r"[A-Z0-9]{4}-?[A-Z0-9]{4}", text):
                return text
    except WebDriverException:
        pass
    return None


def pair_code(driver, number):
    """Returns the 8-char code, 'ALREADY_LINKED', or None."""
    _open(driver, config.WA_URL)
    if _wait_session(driver, 20) is True:
        return "ALREADY_LINKED"

    if not _wait_click(driver, LINK_PHONE_XPATHS, 20):
        log.warning("'Link with phone number' not found")
        return None
    time.sleep(1.5)

    field = None
    deadline = time.time() + 12
    while time.time() < deadline and field is None:
        field = _phone_input(driver)
        if field is None:
            time.sleep(0.5)
    if field is None:
        log.warning("phone field not found")
        return None

    try:
        field.click()
        field.send_keys(Keys.CONTROL, "a")
        field.send_keys(Keys.DELETE)
        field.send_keys(number)
    except WebDriverException as exc:
        log.warning("typing the number failed: %s", exc)
        return None
    time.sleep(1)

    if not _wait_click(driver, NEXT_XPATHS, 12):
        log.warning("Next button not found")
        return None

    deadline = time.time() + 35
    while time.time() < deadline:
        code = _read_pair_code(driver)
        if code:
            return code
        time.sleep(1.5)
    log.warning("pair code never appeared")
    return None


def wait_linked(driver, seconds) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if is_logged_in(driver):
            return True
        time.sleep(1)
    return False


# ---------------------------------------------------------------- report
def _open_chat_menu(driver) -> bool:
    if _wait_click(driver, MENU_BUTTON_XPATHS, 6):
        if _wait_present(driver, MENU_READY_XPATHS, 4) is not None:
            return True
    try:
        buttons = [
            b for b in driver.find_elements(By.XPATH, "//header//div[@role='button']")
            if b.is_displayed()
        ]
        for button in reversed(buttons):
            if _click(driver, button):
                if _wait_present(driver, MENU_READY_XPATHS, 3) is not None:
                    return True
    except WebDriverException:
        pass
    return False


def _close_menu(driver) -> None:
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except WebDriverException:
        pass
    time.sleep(0.4)


def _report_from_panel(driver) -> bool:
    """Fallback route: open the contact drawer and hit Report there."""
    try:
        for xp in ("//header//div[@role='button'][@title]", "//header//span[@title]"):
            el = _visible(driver, By.XPATH, xp)
            if el is not None and _click(driver, el):
                break
        time.sleep(1.2)
        button = _wait_present(driver, PANEL_REPORT_XPATHS, 6)
        if button is not None and _click(driver, button):
            return True
    except WebDriverException:
        pass
    return False


def _report_once(driver, number):
    _open(driver, f"{config.WA_URL}/send/?phone={number}&type=phone_number&app_absent=0")

    deadline = time.time() + config.PAGE_LOAD_TIMEOUT
    opened = False
    while time.time() < deadline:
        if is_logged_in(driver):
            opened = True
            break
        if _login_screen(driver):
            raise LoginRequired("login screen shown")
        time.sleep(0.4)
    if not opened:
        if not _alive(driver):
            raise DriverBroken("browser died loading the chat")
        return False, "app did not load"

    if _wait_present(driver, COMPOSE_XPATHS, config.ACTION_TIMEOUT) is None:
        body = _body_text(driver)
        if "invalid" in body:
            return False, "invalid number"
        return False, "chat did not open"

    time.sleep(0.6)

    if not _open_chat_menu(driver):
        if not _report_from_panel(driver):
            return False, "chat menu not found"
    else:
        item = _wait_present(driver, REPORT_ITEM_XPATHS, 8)
        if item is None:
            _close_menu(driver)
            if not _report_from_panel(driver):
                return False, "Report item missing from menu"
        else:
            if not _click(driver, item):
                return False, "Report item not clickable"

    if _wait_present(driver, DIALOG_XPATHS, 8) is None:
        return False, "no report dialog"

    if not _wait_click(driver, CONFIRM_XPATHS, 8):
        return False, "confirm button missing"

    if _wait_gone(driver, DIALOG_XPATHS, 8):
        return True, "reported"
    return False, "dialog did not close"


def report_once(driver, number):
    """(success, reason). Raises LoginRequired or DriverBroken."""
    try:
        return _report_once(driver, number)
    except (LoginRequired, DriverBroken):
        raise
    except WebDriverException as exc:
        if not _alive(driver):
            raise DriverBroken(str(exc)) from exc
        log.warning("report error: %s", exc)
        return False, "browser error"
