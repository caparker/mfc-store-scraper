"""Add the purchase list to the Azure Standard cart through the site's quick-add form.

The browser runs headed with a persistent profile: the operator logs in by
hand the first time and the cookies are reused afterwards, so no Azure
credentials are stored by this tool. The tool never checks out; it leaves the
browser open on the cart for review.
"""

import re
import time
from pathlib import Path
from typing import Callable

from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from src.lib.logger import logger

SITE_URL = "https://www.azurestandard.com/"
PROFILE_DIR = Path(".playwright-profile")

# Header cart button; opens the <as-cart-slideout> drawer holding the quick-add form.
CART_BUTTON = (
    "#js-header > div > div.flex.flex-col.relative.flex-1 "
    "> div.flex.ml-auto.h-full.max-w-\\[560px\\] > div.relative.w-\\[86px\\] > button"
)
DRAWER = "as-cart-slideout"
# The form only renders when the account can shop (appState.activeOrderState.canShop).
FORM = f"{DRAWER} form[as-quick-add]"
SHOW_QUICK_ADD = re.compile(r"show quick.?add", re.I)
CODE_INPUT = "#js-quickCode"
QTY_INPUT = "#js-quickQty"
SUBMIT = f"{FORM} button[type=submit]"
# Shown while the cart is talking to the server.
BUSY_OVERLAY = f"{DRAWER} .LoadingOverlay"

# Text in the cart drawer that means the add did not go through.
ERROR_PATTERN = re.compile(
    r"not found|invalid|unavailable|out of stock|discontinued|could not|unable|error|"
    r"does not exist|no product",
    re.I,
)

FORM_TIMEOUT_MS = 15_000
SETTLE_TIMEOUT_MS = 15_000


class QuickOrderError(Exception):
    """Raised when the quick-add form cannot be found or driven."""


def _in_viewport(page: Page, locator: Locator) -> bool:
    """The closed drawer is rendered off-screen, so visibility alone is not enough."""
    box = locator.bounding_box()
    size = page.viewport_size or {"width": 0, "height": 0}
    if not box or box["width"] == 0:
        return False
    return 0 <= box["x"] and box["x"] + box["width"] <= size["width"]


def _wait_in_viewport(page: Page, locator: Locator, timeout_ms: int) -> bool:
    """Poll until the element is on screen (the drawer slides in), or give up."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if locator.count() and _in_viewport(page, locator):
            return True
        page.wait_for_timeout(100)
    return False


def _open_drawer(page: Page) -> None:
    """Open the cart drawer if it is closed and reveal the quick-add fields."""
    form = page.locator(FORM).first
    if not (form.count() and _in_viewport(page, form)):
        page.locator(CART_BUTTON).click()
        if not _wait_in_viewport(page, form, FORM_TIMEOUT_MS):
            raise QuickOrderError(
                "Cart opened but the quick-add form is missing; are you logged in to an "
                "account that can shop?"
            )
    show = form.get_by_role("button", name=SHOW_QUICK_ADD)
    if show.count() and show.first.is_visible():
        show.first.click()
    code = page.locator(CODE_INPUT).first
    if not _wait_in_viewport(page, code, FORM_TIMEOUT_MS):
        raise QuickOrderError("Quick-add fields did not appear after clicking Show Quick-Add")


def _find_form(page: Page) -> tuple[Locator, Locator, Locator]:
    _open_drawer(page)
    return (
        page.locator(CODE_INPUT).first,
        page.locator(QTY_INPUT).first,
        page.locator(SUBMIT).first,
    )


def _drawer_text(page: Page) -> str:
    try:
        return page.locator(DRAWER).first.inner_text(timeout=2_000)
    except PlaywrightTimeout:
        return ""


def _settle(page: Page) -> None:
    """Wait for the cart's busy overlay to clear and the network to go quiet."""
    overlay = page.locator(BUSY_OVERLAY)
    try:
        overlay.first.wait_for(state="visible", timeout=2_000)
    except PlaywrightTimeout:
        pass  # Fast responses never show it.
    try:
        overlay.first.wait_for(state="hidden", timeout=SETTLE_TIMEOUT_MS)
    except PlaywrightTimeout:
        logger.debug("Cart busy overlay did not clear; continuing")
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
    except PlaywrightTimeout:
        logger.debug("Page did not reach network idle; continuing")


def add_item(page: Page, code: str, quantity: int, dry_run: bool = False) -> tuple[bool, str]:
    """Type one item into the quick-add form and submit it.

    Returns (added, detail). The add is confirmed when the code shows up in
    the drawer afterwards and no error text does. With no error and no
    sighting it is still counted as added, with a detail saying so.
    """
    code_field, qty_field, button = _find_form(page)
    before = _drawer_text(page)

    code_field.fill("")
    code_field.fill(code)
    qty_field.fill("")
    qty_field.fill(str(quantity))

    if dry_run:
        time.sleep(0.5)
        return True, "dry run, not submitted"

    button.click()
    _settle(page)
    time.sleep(0.5)

    after = _drawer_text(page)
    new_text = after if after == before else after.replace(before, "").strip() or after
    error = ERROR_PATTERN.search(new_text)
    if error:
        snippet = new_text[max(0, error.start() - 60): error.end() + 60].replace("\n", " ")
        return False, snippet
    if code in after and code not in before:
        return True, "seen in cart"
    return True, "no error shown, code not seen in cart text"


def run_quick_order(
    items: list[dict],
    wait_for_user: Callable[[str], None],
    dry_run: bool = False,
    on_done: Callable[[list[dict], list[dict]], None] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Add every item ({code, quantity, name}) to the Azure cart.

    wait_for_user(message) blocks until the operator is ready; it is called
    once before adding (to log in) and once after (to review the cart before
    the browser closes). on_done(added, failed) runs while the browser is
    still open, before the final handoff, so the result is recorded even if
    the operator walks away.
    """
    PROFILE_DIR.mkdir(exist_ok=True)
    added: list[dict] = []
    failed: list[dict] = []

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, viewport={"width": 1400, "height": 950}
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(SITE_URL, wait_until="domcontentloaded")

        wait_for_user(
            "Log in to Azure Standard in the browser window if you are not already, "
            "then press Enter here to start adding items."
        )

        _find_form(page)  # Fail early, before touching anything.

        for index, item in enumerate(items, 1):
            label = (
                f"[{index}/{len(items)}] {item['code']} x{item['quantity']} {item.get('name', '')}"
            )
            try:
                ok, detail = add_item(page, item["code"], item["quantity"], dry_run=dry_run)
            except (QuickOrderError, PlaywrightTimeout) as e:
                ok, detail = False, str(e).splitlines()[0]
            if ok:
                added.append({**item, "detail": detail})
                logger.info(f"{label}: added ({detail})")
            else:
                failed.append({**item, "detail": detail})
                logger.warning(f"{label}: NOT added: {detail}")

        if on_done:
            on_done(added, failed)

        wait_for_user(
            "Review the cart in the browser and check out there if it looks right. "
            "Press Enter here to close the browser."
        )
        context.close()

    return added, failed
