import asyncio
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TimeElapsedColumn, TextColumn
from rich.table import Table
from tenacity import retry, stop_after_delay, wait_fixed
from fingerprints import random_profile
import contextlib
import subprocess
import shutil
import stat

console = Console()

MAX_WAIT = 8000  # ms, per step
POLL_MS = 100  # element polling interval
MAX_FIND_SEC = 60  # per element
HEADLESS = True
INSTANCES = 3  # default; will be overridden by user prompt
VERBOSE = False  # suppress all prints except the live dashboard


@dataclass
class StepResult:
    step: str
    status: str
    detail: str = ""


@dataclass
class InstanceState:
    id: int
    runs: int = 0
    successes: int = 0
    failures: int = 0
    current_step: str = "idle"
    last_url: str = ""
    last_detail: str = ""
    status: str = "idle"
    started_at: float = field(default_factory=time.time)
    last_duration: float = 0.0


def nice_table(title: str, rows: list[tuple[str, str]]):
    table = Table(title=title, show_header=True, header_style="bold magenta")
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Value", style="white")
    for k, v in rows:
        table.add_row(k, v)
    console.print(table)


async def hard_click(page, selector: str, description: str, many_selectors: list[str] | None = None, double: bool = False) -> None:
    """Hard, real-like click with many strategies and live logs.

    - Poll every 100ms up to 60s using multiple selectors
    - Handle hidden elements with no_display class
    - Wait for elements to become visible or click them when found
    - Scroll into view, focus, mouse move, down/up, force click
    - JS click + dispatch pointer events as fallback
    - Optional double click for stubborn buttons
    """
    selectors = [selector] + (many_selectors or [])
    deadline = time.time() + MAX_FIND_SEC
    last_err: Optional[Exception] = None
    last_log = 0.0

    while time.time() < deadline:
        for sel in selectors:
            now = time.time()
            if VERBOSE and now - last_log > 1.0:
                console.print(f"[yellow]Searching[/yellow] for [cyan]{description}[/cyan] using selector: [white]{sel}[/white] t={now:.0f}")
                last_log = now
            
            try:
                # First try to find visible elements
                try:
                    el = await page.wait_for_selector(sel, timeout=POLL_MS, state="visible")
                    if el:
                        if VERBOSE:
                            console.print(f"[green]Found visible element[/green] for {description}")
                        success = await attempt_click_element(page, el, description, double)
                        if success:
                            return
                except PlaywrightTimeout:
                    pass
                
                # If no visible element found, try to find any element (including hidden)
                try:
                    el = await page.wait_for_selector(sel, timeout=POLL_MS, state="attached")
                    if el:
                        # Check if element has no_display class
                        classes = await el.get_attribute("class") or ""
                        if "no_display" in classes:
                            if VERBOSE:
                                console.print(f"[yellow]Found hidden element[/yellow] for {description}, waiting for it to become visible...")
                            
                            # Wait for the element to become visible (remove no_display class)
                            try:
                                # First try with a shorter wait (2 seconds)
                                await page.wait_for_function(
                                    f"document.querySelector('{sel}') && !document.querySelector('{sel}').classList.contains('no_display')",
                                    timeout=2000
                                )
                                if VERBOSE:
                                    console.print(f"[green]Element became visible quickly[/green] for {description}")
                                success = await attempt_click_element(page, el, description, double)
                                if success:
                                    return
                            except PlaywrightTimeout:
                                # Try waiting a bit longer (5 more seconds)
                                try:
                                    await page.wait_for_function(
                                        f"document.querySelector('{sel}') && !document.querySelector('{sel}').classList.contains('no_display')",
                                        timeout=5000
                                    )
                                    if VERBOSE:
                                        console.print(f"[green]Element became visible after delay[/green] for {description}")
                                    success = await attempt_click_element(page, el, description, double)
                                    if success:
                                        return
                                except PlaywrightTimeout:
                                    # Element didn't become visible in 7 seconds total, try alternative approaches
                                    if VERBOSE:
                                        console.print(f"[yellow]Element still hidden after 7s, trying alternative methods[/yellow] for {description}")
                                    
                                    # Try to remove no_display class manually
                                    try:
                                        await page.evaluate(f"document.querySelector('{sel}')?.classList.remove('no_display')")
                                        await asyncio.sleep(0.5)  # Give time for the change to take effect
                                        success = await attempt_click_element(page, el, description, double)
                                        if success:
                                            return
                                    except Exception:
                                        pass
                                    
                                    # Try to click the hidden element directly with JS
                                    try:
                                        await page.evaluate(f"document.querySelector('{sel}')?.click()")
                                        if VERBOSE:
                                            console.print(f"[green]Clicked hidden element via JS[/green] for {description}")
                                        return
                                    except Exception:
                                        pass
                        else:
                            # Element exists and doesn't have no_display class
                            success = await attempt_click_element(page, el, description, double)
                            if success:
                                return
                except PlaywrightTimeout:
                    pass
                    
            except Exception as e:
                last_err = e
                
        await asyncio.sleep(POLL_MS / 1000)
    
    raise last_err or RuntimeError(f"Element not found/clickable: {description}")


async def attempt_click_element(page, el, description: str, double: bool = False) -> bool:
    """Attempt to click an element using various methods. Returns True if successful."""
    try:
        await el.scroll_into_view_if_needed()
    except Exception:
        pass
    
    # Try element.click with different forces
    for force in (True, False):
        try:
            await el.click(force=force, timeout=MAX_WAIT)
            if VERBOSE:
                console.print(f"[green]Clicked[/green] {description} via element.click(force={force})")
            return True
        except Exception:
            continue
    
    # Try double click if requested
    if double:
        try:
            await el.dblclick(timeout=MAX_WAIT)
            if VERBOSE:
                console.print(f"[green]Double-clicked[/green] {description} via element.dblclick()")
            return True
        except Exception:
            pass
    
    # Try mouse interaction at element center with human-like behavior
    try:
        box = await el.bounding_box()
    except Exception:
        box = None
    
    if box:
        try:
            # Add small random offset for more human-like clicking
            x = box["x"] + box["width"] * (0.3 + random.random() * 0.4)
            y = box["y"] + box["height"] * (0.3 + random.random() * 0.4)
            
            # Human-like mouse movement and click timing
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.05, 0.15))  # Brief pause
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.05, 0.12))  # Hold time
            await page.mouse.up()
            
            if VERBOSE:
                console.print(f"[green]Human-like mouse click[/green] on {description} at ({x:.0f},{y:.0f})")
            return True
        except Exception:
            pass
    
    # Try JS click and dispatch events
    try:
        await page.evaluate(
            "el => { el.dispatchEvent(new MouseEvent('mousedown',{bubbles:true})); el.dispatchEvent(new MouseEvent('mouseup',{bubbles:true})); el.click(); }",
            el,
        )
        if VERBOSE:
            console.print(f"[green]Clicked[/green] {description} via JS dispatch")
        return True
    except Exception:
        pass
    
    return False


async def search_and_click_close_svg(page) -> bool:
    """Search for close SVG element every 100ms for up to 5 seconds and click if found"""
    deadline = time.time() + 5.0  # 5 seconds timeout
    
    # SVG selector based on the path content
    svg_selector = 'svg path[d="M38 12.83L35.17 10 24 21.17 12.83 10 10 12.83 21.17 24 10 35.17 12.83 38 24 26.83 35.17 38 38 35.17 26.83 24z"]'
    
    while time.time() < deadline:
        try:
            # Look for the SVG element
            svg_element = await page.query_selector(svg_selector)
            if svg_element:
                # Check if the element is visible
                is_visible = await svg_element.is_visible()
                if is_visible:
                    try:
                        # Click on the SVG element (or its parent)
                        svg_parent = await svg_element.query_selector('xpath=..')
                        if svg_parent:
                            await svg_parent.click(timeout=2000)
                        else:
                            await svg_element.click(timeout=2000)
                        
                        if VERBOSE:
                            console.print(f"[green]Clicked close SVG element[/green]")
                        return True
                    except Exception as e:
                        if VERBOSE:
                            console.print(f"[yellow]Failed to click SVG element: {e}[/yellow]")
                        # Try alternative clicking methods
                        try:
                            # Get the SVG element or its clickable parent
                            clickable_element = svg_parent if svg_parent else svg_element
                            box = await clickable_element.bounding_box()
                            if box:
                                await page.mouse.click(
                                    box["x"] + box["width"] / 2,
                                    box["y"] + box["height"] / 2
                                )
                                if VERBOSE:
                                    console.print(f"[green]Clicked close SVG element via mouse[/green]")
                                return True
                        except Exception:
                            pass
        except Exception:
            pass
        
        # Wait 100ms before next attempt
        await asyncio.sleep(0.1)
    
    # Element not found within 5 seconds
    if VERBOSE:
        console.print(f"[yellow]Close SVG element not found within 5 seconds[/yellow]")
    return False


async def wait_for_load(page, max_ms: int = 8000):
    """Wait for page load with 8 second timeout, continue if fails"""
    try:
        # Wait for DOM content loaded with 8 second limit
        await page.wait_for_load_state("domcontentloaded", timeout=max_ms)
    except Exception:
        if VERBOSE:
            console.print(f"[yellow]DOM load timeout after {max_ms/1000}s, continuing...[/yellow]")
        pass
    try:
        # Wait for network idle but with shorter timeout
        await page.wait_for_load_state("networkidle", timeout=2000)
    except Exception:
        if VERBOSE:
            console.print(f"[yellow]Network idle timeout, continuing...[/yellow]")
        pass


def _on_rm_error(func, path, exc_info):
    # On Windows, remove read-only flag and retry
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def wipe_dir(path: Path, attempts: int = 3, delay: float = 0.3):
    for _ in range(attempts):
        if not path.exists():
            return
        try:
            shutil.rmtree(path, onerror=_on_rm_error)
            return
        except Exception:
            time.sleep(delay)
    # Fallback: best-effort empty contents
    if path.exists():
        for p in sorted(path.rglob('*'), reverse=True):
            try:
                if p.is_file() or p.is_symlink():
                    os.chmod(p, stat.S_IWRITE)
                    p.unlink(missing_ok=True)
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass

def read_random_url(file_path: str) -> str:
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"Input file not found: {file_path}")
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    if not lines:
        raise ValueError("No URLs found in the file")
    return random.choice(lines)


async def run_once(url: str, instance_id: int, _unused_user_data_dir: Path, state: InstanceState) -> list[StepResult]:
    results: list[StepResult] = []
    start_time = time.time()
    async with async_playwright() as p:
        state.current_step = "launching"
        browser = None
        context = None
        page = None
        try:
            try:
                browser = await p.chromium.launch(
                    headless=HEADLESS, 
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-extensions",
                        "--disable-plugins",
                        "--disable-images",
                        "--disable-javascript-harmony-shipping",
                        "--disable-background-timer-throttling",
                        "--disable-renderer-backgrounding",
                        "--disable-backgrounding-occluded-windows",
                        "--disable-ipc-flooding-protection",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--no-pings",
                        "--password-store=basic",
                        "--use-mock-keychain",
                        "--disable-component-extensions-with-background-pages",
                        "--disable-default-apps",
                        "--mute-audio",
                        "--no-zygote",
                        "--disable-background-networking",
                        "--disable-web-security",
                        "--disable-features=TranslateUI,BlinkGenPropertyTrees",
                        "--hide-scrollbars",
                        "--disable-gpu"
                    ]
                )  # type: ignore
            except Exception:
                subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
                browser = await p.chromium.launch(
                    headless=HEADLESS, 
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-extensions",
                        "--disable-plugins",
                        "--disable-images",
                        "--disable-javascript-harmony-shipping",
                        "--disable-background-timer-throttling",
                        "--disable-renderer-backgrounding",
                        "--disable-backgrounding-occluded-windows",
                        "--disable-ipc-flooding-protection",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--no-pings",
                        "--password-store=basic",
                        "--use-mock-keychain",
                        "--disable-component-extensions-with-background-pages",
                        "--disable-default-apps",
                        "--mute-audio",
                        "--no-zygote",
                        "--disable-background-networking",
                        "--disable-web-security",
                        "--disable-features=TranslateUI,BlinkGenPropertyTrees",
                        "--hide-scrollbars",
                        "--disable-gpu"
                    ]
                )  # type: ignore
            
            fp = random_profile()
            context = await browser.new_context(
                user_agent=fp["user_agent"],
                viewport=fp["viewport"],
                device_scale_factor=fp["device_scale_factor"],
                is_mobile=fp["is_mobile"],
                has_touch=fp["has_touch"],
                locale=fp["locale"],
                timezone_id=fp["timezone_id"],
                color_scheme=fp["color_scheme"],
                reduced_motion=fp["reduced_motion"],
                extra_http_headers=fp["headers"],
                java_script_enabled=True,
                ignore_https_errors=True,
                permissions=['geolocation', 'notifications'],
                geolocation={
                    "latitude": random.uniform(25.0, 49.0), 
                    "longitude": random.uniform(-125.0, -66.0)
                }
            )
            
            # Inject comprehensive stealth script on every page
            await context.add_init_script(fp["stealth_script"])
            
            # Add behavioral simulation for human-like interaction
            await context.add_init_script(f"""
                // Realistic mouse movement simulation
                let mouseX = {random.randint(100, 800)};
                let mouseY = {random.randint(100, 600)};
                
                function simulateHumanMouse() {{
                    const deltaX = (Math.random() - 0.5) * 20;
                    const deltaY = (Math.random() - 0.5) * 20;
                    mouseX = Math.max(0, Math.min(window.innerWidth, mouseX + deltaX));
                    mouseY = Math.max(0, Math.min(window.innerHeight, mouseY + deltaY));
                    
                    const moveEvent = new MouseEvent('mousemove', {{
                        clientX: mouseX,
                        clientY: mouseY,
                        bubbles: true
                    }});
                    document.dispatchEvent(moveEvent);
                }}
                
                // Random mouse movements every 1-3 seconds
                setInterval(simulateHumanMouse, {random.randint(1000, 3000)});
                
                // Simulate scroll behavior
                let scrollPos = 0;
                function simulateScrollPattern() {{
                    if (Math.random() < 0.3) {{ // 30% chance to scroll
                        const direction = Math.random() < 0.7 ? 1 : -1; // 70% down, 30% up
                        const scrollAmount = Math.random() * 100 + 50;
                        scrollPos += direction * scrollAmount;
                        scrollPos = Math.max(0, Math.min(document.body.scrollHeight, scrollPos));
                        window.scrollTo(0, scrollPos);
                    }}
                }}
                setInterval(simulateScrollPattern, {random.randint(2000, 5000)});
                
                console.log('🤖 Human behavior simulation active');
            """)
            
            page = await context.new_page()
            
            # Additional page-level stealth enhancements
            await page.evaluate("""
                // Override common automation detection points
                delete window.navigator.webdriver;
                
                // Safely handle chrome.runtime
                if (window.chrome && window.chrome.runtime) {
                    delete window.chrome.runtime.onConnect;
                }
                
                // Simulate realistic timing
                const originalSetTimeout = window.setTimeout;
                window.setTimeout = function(fn, delay, ...args) {
                    const humanDelay = delay + Math.random() * 50 - 25; // ±25ms variation
                    return originalSetTimeout(fn, Math.max(0, humanDelay), ...args);
                };
            """)

            state.current_step = "open url"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=8000)
            except Exception as e:
                if VERBOSE:
                    console.print(f"[yellow]Page load timeout for {url}, continuing... Error: {e}[/yellow]")
                # Try to continue even if page load fails
                pass
            await wait_for_load(page)
            results.append(StepResult("Open URL", "OK", url))

            # Step 1: div.start_btn
            state.current_step = "step 1"
            await hard_click(page, "div.start_btn", "div.start_btn", [
                ".start_btn", 
                "div[class*='start_btn']", 
                "button.start_btn",
                "[class*='start_btn']:not(.no_display)",
                "div.btn:contains('Click here to verify')",
                "div:contains('Click here to verify')"
            ])
            await wait_for_load(page)
            results.append(StepResult("Step 1", "OK"))

            # Step 2: div.btn:nth-child(1)
            state.current_step = "step 2"
            await hard_click(page, "div.btn:nth-child(1)", "div.btn:nth-child(1)", [
                "div.btn:first-child",
                ".btn:nth-child(1)",
                "div[class*='btn']:nth-child(1)",
                "div.btn:not(.no_display):nth-child(1)"
            ])
            await wait_for_load(page)
            results.append(StepResult("Step 2", "OK"))

            # Step 3: a.btn:nth-child(1)
            state.current_step = "step 3"
            await hard_click(page, "a.btn:nth-child(1)", "a.btn:nth-child(1)", [
                "a.btn:first-child",
                "a[class*='btn']:nth-child(1)",
                "a.btn:not(.no_display):nth-child(1)",
                "a:nth-child(1)[class*='btn']"
            ])
            await wait_for_load(page)
            results.append(StepResult("Step 3", "OK"))
            
            # Search for close SVG element after step 3
            await search_and_click_close_svg(page)

            # Step 4: div.start_btn (after redirect)
            state.current_step = "step 4"
            await hard_click(page, "div.start_btn", "div.start_btn", [
                ".start_btn", 
                "div[class*='start_btn']", 
                "button.start_btn",
                "[class*='start_btn']:not(.no_display)",
                "div.btn:contains('Click here to verify')",
                "div:contains('Click here to verify')"
            ])
            await wait_for_load(page)
            results.append(StepResult("Step 4", "OK"))

            # Step 5: div.btn:nth-child(1)
            state.current_step = "step 5"
            await hard_click(page, "div.btn:nth-child(1)", "div.btn:nth-child(1)", [
                "div.btn:first-child",
                ".btn:nth-child(1)",
                "div[class*='btn']:nth-child(1)",
                "div.btn:not(.no_display):nth-child(1)"
            ])
            await wait_for_load(page)
            results.append(StepResult("Step 5", "OK"))

            # Step 6: a.btn:nth-child(1)
            state.current_step = "step 6"
            await hard_click(page, "a.btn:nth-child(1)", "a.btn:nth-child(1)", [
                "a.btn:first-child",
                "a[class*='btn']:nth-child(1)",
                "a.btn:not(.no_display):nth-child(1)",
                "a:nth-child(1)[class*='btn']"
            ])
            await wait_for_load(page)
            results.append(StepResult("Step 6", "OK"))
            
            # Search for close SVG element after step 6
            await search_and_click_close_svg(page)

            # Step 7: div.start_btn
            state.current_step = "step 7"
            await hard_click(page, "div.start_btn", "div.start_btn", [
                ".start_btn", 
                "div[class*='start_btn']", 
                "button.start_btn",
                "[class*='start_btn']:not(.no_display)",
                "div.btn:contains('Click here to verify')",
                "div:contains('Click here to verify')"
            ])
            await wait_for_load(page)
            results.append(StepResult("Step 7", "OK"))

            # Step 8: div.btn:nth-child(2)
            state.current_step = "step 8"
            await hard_click(page, "div.btn:nth-child(2)", "div.btn:nth-child(2)", [
                "div.btn:nth-of-type(2)",
                ".btn:nth-child(2)",
                "div[class*='btn']:nth-child(2)",
                "div.btn:not(.no_display):nth-child(2)"
            ])
            await wait_for_load(page)
            results.append(StepResult("Step 8", "OK"))

            # Step 9: a.btn:nth-child(2)
            state.current_step = "step 9"
            await hard_click(page, "a.btn:nth-child(2)", "a.btn:nth-child(2)", [
                "a.btn:nth-of-type(2)",
                "a[class*='btn']:nth-child(2)",
                "a.btn:not(.no_display):nth-child(2)",
                "a:nth-child(2)[class*='btn']"
            ])
            await wait_for_load(page)
            results.append(StepResult("Step 9", "OK"))
            
            # Search for close SVG element after step 9
            await search_and_click_close_svg(page)

            # Step 10: a.get-link (wait 5 seconds)
            state.current_step = "step 10"
            await wait_for_load(page)
            await asyncio.sleep(5)
            await hard_click(page, "a.get-link", "a.get-link", [
                ".get-link",
                "a[class*='get-link']",
                "a:contains('Get Link')",
                "a[href*='link']"
            ])
            await asyncio.sleep(1)
            results.append(StepResult("Step 10", "OK", "Waited 5s before click"))

            # Step 9: close browser after 1s post-click wait
            state.current_step = "closing"
            return results
        finally:
            with contextlib.suppress(Exception):
                if context is not None:
                    await context.close()
            with contextlib.suppress(Exception):
                if browser is not None:
                    await browser.close()
            elapsed = time.time() - start_time
            results.append(StepResult("Done", "OK", f"Elapsed: {elapsed:.1f}s"))


async def main():
    file_path = r"urls.txt"
    try:
        urls_list = [ln.strip() for ln in Path(file_path).read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    except Exception as e:
        console.print(f"[bold red]Failed to read URL file:[/bold red] {e}")
        sys.exit(1)
    if not urls_list:
        console.print("[bold red]No URLs found in the file[/bold red]")
        sys.exit(1)

    # Ask for number of instances (1-50)
    try:
        user_in = console.input("[bold cyan]How many instances to run (1-50)? [/bold cyan]")
        n = int(user_in.strip())
        if n < 1 or n > 50:
            raise ValueError
        global INSTANCES
        INSTANCES = n
    except Exception:
        INSTANCES = 1

    states: list[InstanceState] = [InstanceState(id=i+1) for i in range(INSTANCES)]
    total_done = 0

    def pick_url() -> str:
        return random.choice(urls_list)

    def make_dashboard() -> Table:
        table = Table(title="Automation Dashboard", header_style="bold magenta")
        table.add_column("Inst", style="cyan", no_wrap=True)
        table.add_column("Status", style="white")
        table.add_column("Current Step", style="yellow")
        table.add_column("Runs", style="white")
        table.add_column("OK", style="green")
        table.add_column("Fail", style="red")
        table.add_column("Last URL", style="bright_blue")
        table.add_column("Last Detail", style="white")
        for s in states:
            table.add_row(
                str(s.id), s.status, s.current_step, str(s.runs), str(s.successes), str(s.failures), s.last_url[:40] + ("…" if len(s.last_url) > 40 else ""), s.last_detail
            )
        # Totals row
        table.add_row(
            "Totals",
            "",
            "",
            str(sum(s.runs for s in states)),
            str(sum(s.successes for s in states)),
            str(sum(s.failures for s in states)),
            "Ongoing: " + str(sum(1 for s in states if s.status == 'running')),
            "",
        )
        return table

    async def runner_task(state: InstanceState):
        nonlocal total_done
        while True:
            state.status = "running"
            state.current_step = "pick url"
            url = pick_url()
            state.last_url = url
            user_data_dir = Path('.')  # unused (no persistence)
            state.started_at = time.time()
            try:
                results = await run_once(url, state.id, user_data_dir, state)
                state.runs += 1
                state.successes += 1
                total_done += 1
                state.last_detail = results[-1].detail if results else ""
                state.status = "idle"
                state.current_step = "sleep 1s"
                await asyncio.sleep(1)
            except Exception as e:
                state.runs += 1
                state.failures += 1
                state.status = "error/restarting"
                state.last_detail = str(e)
                # No persistence; nothing to clean
                await asyncio.sleep(0.5)

    # Start runners
    runners = [asyncio.create_task(runner_task(s)) for s in states]

    # Live dashboard loop only (no extra prints)
    from rich.live import Live
    try:
        with Live(make_dashboard(), console=console, refresh_per_second=4) as live:
            while True:
                live.update(make_dashboard(), refresh=True)
                await asyncio.sleep(0.25)
    except (KeyboardInterrupt, SystemExit):
        # Graceful shutdown: cancel runners
        for t in runners:
            t.cancel()
    finally:
        with contextlib.suppress(Exception):
            await asyncio.gather(*runners, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Graceful shutdown without stack traces
        pass
