"""Screenshot the preview app at desktop and phone sizes.

Development only (not in the Docker image). Drives a headless Chrome or Edge
over the DevTools protocol with the `websockets` package Streamlit already
installs, so it needs no new dependency. It makes no model calls: start
scripts/dev/preview_ui.py first, which answers from cached traces.

    .venv/Scripts/python.exe scripts/dev/preview_ui.py --port 8601
    .venv/Scripts/python.exe scripts/dev/screenshot_ui.py --out DIR

For each case and size it saves
  <case>-<w>x<h>.png   exactly the viewport, as a visitor first sees it
  <case>-<w>-full.png  the same width, tall enough to show the whole page
and prints any horizontal overflow of the page.

    .venv/Scripts/python.exe scripts/dev/screenshot_ui.py --compare BEFORE AFTER OUT

puts each pair of same-named screenshots side by side, labelled.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect


@dataclass(frozen=True)
class Case:
    name: str
    question: str | None
    """None: the empty conversation with the example chips."""
    expand: bool = False
    """Open the reply's collapsed sections (the steps and their SQL)."""


FUZZY = ("What was the 2024 completed revenue of our customer Idirs Rosis? "
         "Give the customer's ID and name with the revenue.")
WHY = "Why did Hardware revenue drop so sharply from Q2 to Q3 2025?"
CASES = (
    Case("chips", None),
    Case("direct", "What was total completed revenue in 2024?"),
    Case("fuzzy", FUZZY),
    Case("why", WHY),
    Case("forecast", "Using a straight-line trend fitted to monthly completed revenue "
                     "from January 2024 to December 2025, what is the forecast for "
                     "January 2026?"),
    # Not example chips: typed in, to check the line and bar charts.
    Case("series", "How many completed orders were placed in each month of the "
                   "first half of 2025? Label months as YYYY-MM."),
    Case("bars", "What were the top 3 products by completed revenue in 2024? Give "
                 "each product's ID, name and revenue."),
    Case("fuzzy-steps", FUZZY, expand=True),
    Case("why-steps", WHY, expand=True),
)
# The reply's collapsed sections, by part of their label (app.py's
# "How this was answered" and its "Step n: ..." sections inside).
EXPANDABLE = ("How this was answered", "Step ")
SIZES = ((1440, 900), (390, 844))
BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "chrome", "chromium", "msedge",
)
FIRST_CHIP = "How many customers signed up in 2025?"

# The element that scrolls the main column: the nearest scrollable ancestor
# of the page title. Streamlit scrolls an inner container, not the document,
# so a full-page capture has to grow the viewport by that container's overflow.
SCROLLER_JS = """
(() => {
  let e = document.querySelector('h1');
  while (e && e !== document.body) {
    const s = getComputedStyle(e);
    if (/(auto|scroll)/.test(s.overflowY) && e.scrollHeight > 0) return e;
    e = e.parentElement;
  }
  return document.scrollingElement;
})()
"""


class Cdp:
    """Just enough of the DevTools protocol: send a command, get its result."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.last_id = 0

    async def send(self, method: str, **params: Any) -> dict[str, Any]:
        self.last_id += 1
        await self.ws.send(json.dumps({"id": self.last_id, "method": method,
                                       "params": params}))
        while True:
            reply = json.loads(await self.ws.recv())
            if reply.get("id") != self.last_id:
                continue  # an event; none are needed here
            if "error" in reply:
                raise RuntimeError(f"{method}: {reply['error']}")
            return reply.get("result", {})

    async def js(self, expression: str) -> Any:
        result = await self.send("Runtime.evaluate", expression=expression,
                                 returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise RuntimeError(f"JS error: {result['exceptionDetails']}")
        return result["result"].get("value")

    async def wait_for(self, expression: str, timeout: float = 30.0) -> None:
        # False rather than an error while the page is still loading (no body).
        guarded = (f"(() => {{ try {{ return !!({expression}); }} "
                   "catch (e) { return false; } })()")
        deadline = time.monotonic() + timeout
        while not await self.js(guarded):
            if time.monotonic() > deadline:
                raise TimeoutError(f"timed out waiting for: {expression}")
            await asyncio.sleep(0.25)

    async def settle(self, quiet: float = 1.5, timeout: float = 40.0) -> None:
        """Wait until the page text stops changing and fonts have loaded."""
        deadline = time.monotonic() + timeout
        last, since = None, time.monotonic()
        while time.monotonic() - since < quiet:
            text = await self.js("document.body.innerText")
            if text != last:
                last, since = text, time.monotonic()
            if time.monotonic() > deadline:
                raise TimeoutError("the page never settled")
            await asyncio.sleep(0.25)
        await self.js("document.fonts.ready.then(() => true)")
        await asyncio.sleep(1.0)  # Vega draws charts after the text settles

    async def screenshot(self, path: Path) -> None:
        shot = await self.send("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(shot["data"]))


def find_browser(given: str | None) -> str:
    for candidate in ([given] if given else BROWSERS):
        found = shutil.which(candidate) or (candidate if Path(candidate).exists()
                                             else None)
        if found:
            return found
    raise SystemExit("No Chrome or Edge found; pass --browser PATH.")


def page_socket(port: int, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed localhost URL
                    f"http://127.0.0.1:{port}/json/list", timeout=2) as reply:
                pages = [t for t in json.load(reply) if t["type"] == "page"]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
        except OSError:
            pass
        time.sleep(0.25)
    raise TimeoutError("the browser's DevTools endpoint did not come up")


async def ask(cdp: Cdp, question: str) -> None:
    """Click the example chip with this text, or type it into the chat box."""
    clicked = await cdp.js(
        "(() => { const b = [...document.querySelectorAll('button')]"
        f".find(b => b.innerText.trim() === {json.dumps(question)});"
        " if (b) { b.click(); return true; } return false; })()")
    if clicked:
        return
    await cdp.wait_for("document.querySelector('textarea') !== null")
    await cdp.js("document.querySelector('textarea').focus()")
    await cdp.send("Input.insertText", text=question)
    for kind in ("keyDown", "keyUp"):
        await cdp.send("Input.dispatchKeyEvent", type=kind, key="Enter",
                       code="Enter", windowsVirtualKeyCode=13)


async def shoot(cdp: Cdp, url: str, case: Case, size: tuple[int, int],
                out: Path) -> str:
    width, height = size
    phone = width < 768
    metrics = {"width": width, "deviceScaleFactor": 2 if phone else 1,
               "mobile": phone}
    await cdp.send("Emulation.setDeviceMetricsOverride", height=height, **metrics)
    await cdp.send("Page.navigate", url=url)
    await cdp.wait_for("document.body.innerText.includes("
                       f"{json.dumps(FIRST_CHIP)})")
    if case.question:
        await ask(cdp, case.question)
        await cdp.wait_for("document.querySelector('.user-msg') !== null")
    await cdp.settle()
    if case.expand:
        # Outer section first, then the steps it reveals, one pass each.
        # "includes", not "startsWith": a summary's text begins with the name
        # of its arrow icon.
        for label in EXPANDABLE:
            await cdp.js(
                "[...document.querySelectorAll('summary')]"
                f".filter(s => s.innerText.includes({json.dumps(label)}))"
                ".forEach(s => s.click())")
            await cdp.settle(quiet=1.0)
    await cdp.screenshot(out / f"{case.name}-{width}x{height}.png")

    overflow = await cdp.js(
        "(() => { const d = document.documentElement, m = " + SCROLLER_JS +
        "; return [d.scrollWidth - d.clientWidth, m.scrollWidth - m.clientWidth]; })()")
    for _ in range(3):  # grow until the main column no longer scrolls
        extra = await cdp.js(f"(() => {{ const m = {SCROLLER_JS}; "
                             "return m.scrollHeight - m.clientHeight; })()")
        if extra <= 1:
            break
        height += extra
        await cdp.send("Emulation.setDeviceMetricsOverride", height=height,
                       **metrics)
        await asyncio.sleep(0.8)
    await cdp.screenshot(out / f"{case.name}-{width}-full.png")
    return (f"{case.name} {width}px: horizontal overflow page={overflow[0]}px "
            f"main={overflow[1]}px")


async def capture(url: str, out: Path, browser: str, port: int,
                  names: set[str] | None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    # Chrome's helper processes can outlive the main one by a moment on
    # Windows and still hold a profile file when the folder is removed.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen(  # noqa: S603 - a local browser binary
            [browser, "--headless=new", f"--remote-debugging-port={port}",
             f"--user-data-dir={profile}", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions",
             "--hide-scrollbars", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            async with connect(page_socket(port), max_size=None) as ws:
                cdp = Cdp(ws)
                for case in CASES:
                    if names and case.name not in names:
                        continue
                    for size in SIZES:
                        print(await shoot(cdp, url, case, size, out), flush=True)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def compare(before: Path, after: Path, out: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    out.mkdir(parents=True, exist_ok=True)
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except OSError:
        font = ImageFont.load_default(size=28)
    for left in sorted(before.glob("*.png")):
        right = after / left.name
        if not right.exists():
            continue
        a, b = Image.open(left), Image.open(right)
        gap, head = 40, 56
        sheet = Image.new("RGB", (a.width + gap + b.width,
                                  head + max(a.height, b.height)), "white")
        draw = ImageDraw.Draw(sheet)
        draw.text((0, 12), "Before", fill="#1E293B", font=font)
        draw.text((a.width + gap, 12), "After", fill="#1E293B", font=font)
        draw.line([(a.width + gap // 2, 0), (a.width + gap // 2, sheet.height)],
                  fill="#CBD5E1", width=2)
        sheet.paste(a, (0, head))
        sheet.paste(b, (a.width + gap, head))
        sheet.save(out / left.name, optimize=True)
        print(out / left.name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8601/")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--cases", nargs="*", help="case names (default: all)")
    parser.add_argument("--browser", help="path to Chrome or Edge")
    parser.add_argument("--port", type=int, default=9333,
                        help="DevTools port for the headless browser")
    parser.add_argument("--compare", nargs=3, type=Path,
                        metavar=("BEFORE", "AFTER", "OUT"))
    args = parser.parse_args()
    if args.compare:
        compare(*args.compare)
        return
    if not args.out:
        parser.error("--out is required unless --compare is given")
    asyncio.run(capture(args.url, args.out, find_browser(args.browser), args.port,
                        set(args.cases) if args.cases else None))


if __name__ == "__main__":
    main()
