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


async def close_google_ads(page):
    """Enhanced Google Vignette and general ad closing logic"""
    try:
        # Wait a moment for ads to load
        await asyncio.sleep(0.5)
        
        # Comprehensive list of ad close selectors
        ad_close_selectors = [
            # Google Vignette specific
            "#google_vignette .close-button",
            "#google_vignette button[aria-label*='close']",
            "#google_vignette button[aria-label*='Close']",
            "#google_vignette [role='button']",
            "#google_vignette .close",
            "#google_vignette .x",
            "#google_vignette button",
            ".google_vignette .close-button",
            ".google_vignette button[aria-label*='close']",
            ".google_vignette button[aria-label*='Close']",
            ".google_vignette [role='button']",
            ".google_vignette .close",
            ".google_vignette .x",
            ".google_vignette button",
            
            # General ad close patterns
            "div[id*='google_vignette'] button",
            "div[class*='google_vignette'] button",
            "div[id*='vignette'] button",
            "div[class*='vignette'] button",
            "[data-google-vignette] button",
            "[data-vignette] button",
            "button[onclick*='google_vignette']",
            "button[onclick*='vignette']",
            
            # Common ad overlay patterns
            ".ad-overlay button",
            ".popup-overlay button",
            ".modal-overlay button",
            ".interstitial button",
            "[class*='overlay'] button[aria-label*='close']",
            "[class*='popup'] button[aria-label*='close']",
            "[class*='modal'] button[aria-label*='close']",
            
            # Generic close button patterns
            "button[title*='Close']",
            "button[title*='close']",
            "button[alt*='Close']",
            "button[alt*='close']",
            "a[title*='Close']",
            "a[title*='close']",
            ".close-btn",
            ".close-button",
            ".btn-close",
            ".popup-close",
            ".modal-close",
            
            # X close buttons
            "button:has-text('×')",
            "button:has-text('✕')",
            "button:has-text('X')",
            "a:has-text('×')",
            "a:has-text('✕')",
            "a:has-text('X')",
            
            # XPath patterns for more complex detection
            "xpath=//div[contains(@id,'google_vignette')]//button",
            "xpath=//div[contains(@class,'google_vignette')]//button",
            "xpath=//div[contains(@id,'vignette')]//button",
            "xpath=//div[contains(@class,'vignette')]//button",
            "xpath=//button[contains(@aria-label,'close') or contains(@aria-label,'Close')]",
            "xpath=//button[contains(@title,'close') or contains(@title,'Close')]",
            "xpath=//button[text()='×' or text()='✕' or text()='X']",
            "xpath=//a[text()='×' or text()='✕' or text()='X']",
        ]
        
        # Try to close ads with multiple attempts
        ads_closed = 0
        max_attempts = 3
        
        for attempt in range(max_attempts):
            found_ad = False
            
            for selector in ad_close_selectors:
                try:
                    # Check if ad close button exists (very short timeout)
                    elements = await page.query_selector_all(selector)
                    
                    for element in elements:
                        try:
                            # Check if element is visible
                            is_visible = await element.is_visible()
                            if is_visible:
                                # Try multiple click methods
                                try:
                                    await element.click(timeout=2000, force=True)
                                    found_ad = True
                                    ads_closed += 1
                                    if VERBOSE:
                                        console.print(f"[yellow]Closed ad (method 1)[/yellow] using: {selector}")
                                except Exception:
                                    try:
                                        # Force JS click if normal click fails
                                        await page.evaluate("element => element.click()", element)
                                        found_ad = True
                                        ads_closed += 1
                                        if VERBOSE:
                                            console.print(f"[yellow]Closed ad (method 2)[/yellow] using: {selector}")
                                    except Exception:
                                        try:
                                            # Try mouse click at element position
                                            box = await element.bounding_box()
                                            if box:
                                                await page.mouse.click(
                                                    box["x"] + box["width"] / 2,
                                                    box["y"] + box["height"] / 2
                                                )
                                                found_ad = True
                                                ads_closed += 1
                                                if VERBOSE:
                                                    console.print(f"[yellow]Closed ad (method 3)[/yellow] using: {selector}")
                                        except Exception:
                                            pass
                        except Exception:
                            pass
                except Exception:
                    pass
            
            if found_ad:
                # Wait for ad to close and page to settle
                await asyncio.sleep(1)
                
                # Check if more ads appeared
                continue
            else:
                # No more ads found, break the loop
                break
        
        # Additional check for any remaining overlay elements
        try:
            overlay_selectors = [
                "[style*='position: fixed']",
                "[style*='z-index: 999']",
                "[style*='z-index: 9999']",
                ".overlay",
                ".popup",
                ".modal",
                ".interstitial"
            ]
            
            for overlay_sel in overlay_selectors:
                overlays = await page.query_selector_all(overlay_sel)
                for overlay in overlays:
                    try:
                        is_visible = await overlay.is_visible()
                        if is_visible:
                            # Look for close button within overlay
                            close_btn = await overlay.query_selector("button, a[href='#'], .close, .x")
                            if close_btn:
                                await close_btn.click(force=True)
                                ads_closed += 1
                                if VERBOSE:
                                    console.print(f"[yellow]Closed overlay ad[/yellow]")
                    except Exception:
                        pass
        except Exception:
            pass
        
        if ads_closed > 0 and VERBOSE:
            console.print(f"[green]Successfully closed {ads_closed} ad(s)[/green]")
            
    except Exception as e:
        if VERBOSE:
            console.print(f"[red]Ad closing error: {e}[/red]")
        pass  # Silently handle any ad-closing errors


async def hard_click(page, selector: str, description: str, many_selectors: list[str] | None = None, double: bool = False) -> None:
    """Hard, real-like click with many strategies and live logs.

    - Poll every 100ms up to 60s using multiple selectors
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
                el = await page.wait_for_selector(sel, timeout=POLL_MS, state="visible")
                if not el:
                    continue
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
                        # Wait briefly for any ads to load, then close them
                        await asyncio.sleep(0.8)
                        await close_google_ads(page)
                        return
                    except Exception as e:
                        last_err = e
                # Try double click if requested
                if double:
                    try:
                        await el.dblclick(timeout=MAX_WAIT)
                        if VERBOSE:
                            console.print(f"[green]Double-clicked[/green] {description} via element.dblclick()")
                        # Wait briefly for any ads to load, then close them
                        await asyncio.sleep(0.8)
                        await close_google_ads(page)
                        return
                    except Exception as e:
                        last_err = e
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
                        # Wait briefly for any ads to load, then close them
                        await asyncio.sleep(0.8)
                        await close_google_ads(page)
                        return
                    except Exception as e:
                        last_err = e
                # Try page.click selector directly
                try:
                    await page.click(sel, timeout=MAX_WAIT, force=True)
                    if VERBOSE:
                        console.print(f"[green]Clicked[/green] {description} via page.click(force=True)")
                    # Wait briefly for any ads to load, then close them
                    await asyncio.sleep(0.8)
                    await close_google_ads(page)
                    return
                except Exception as e:
                    last_err = e
                # Try JS click and dispatch events
                try:
                    await page.evaluate(
                        "el => { el.dispatchEvent(new MouseEvent('mousedown',{bubbles:true})); el.dispatchEvent(new MouseEvent('mouseup',{bubbles:true})); el.click(); }",
                        el,
                    )
                    if VERBOSE:
                        console.print(f"[green]Clicked[/green] {description} via JS dispatch")
                    # Wait briefly for any ads to load, then close them
                    await asyncio.sleep(0.8)
                    await close_google_ads(page)
                    return
                except Exception as e:
                    last_err = e
            except PlaywrightTimeout as e:
                last_err = e
                await asyncio.sleep(POLL_MS / 1000)
                continue
            except Exception as e:
                last_err = e
                await asyncio.sleep(POLL_MS / 1000)
                continue
        await asyncio.sleep(POLL_MS / 1000)
    raise last_err or RuntimeError(f"Element not found/clickable: {description}")


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

            # Proactively check for ads on page load
            await close_google_ads(page)

            # Step 1: div.start_btn
            state.current_step = "step 1"
            await close_google_ads(page)  # Check before clicking
            await hard_click(page, "div.start_btn", "div.start_btn")
            await wait_for_load(page)
            results.append(StepResult("Step 1", "OK"))

            # Step 2: div.btn:nth-child(1)
            state.current_step = "step 2"
            await close_google_ads(page)  # Check before clicking
            await hard_click(page, "div.btn:nth-child(1)", "div.btn:nth-child(1)")
            await wait_for_load(page)
            results.append(StepResult("Step 2", "OK"))

            # Step 3: a.btn:nth-child(1)
            state.current_step = "step 3"
            await close_google_ads(page)  # Check before clicking
            await hard_click(page, "a.btn:nth-child(1)", "a.btn:nth-child(1)")
            await wait_for_load(page)
            results.append(StepResult("Step 3", "OK"))

            # Step 4: div.start_btn (after redirect)
            state.current_step = "step 4"
            await close_google_ads(page)  # Check before clicking
            await hard_click(page, "div.start_btn", "div.start_btn")
            await wait_for_load(page)
            results.append(StepResult("Step 4", "OK"))

            # Step 5: div.btn:nth-child(1)
            state.current_step = "step 5"
            await close_google_ads(page)  # Check before clicking
            await hard_click(page, "div.btn:nth-child(1)", "div.btn:nth-child(1)")
            await wait_for_load(page)
            results.append(StepResult("Step 5", "OK"))

            # Step 6: a.btn:nth-child(1)
            state.current_step = "step 6"
            await close_google_ads(page)  # Check before clicking
            await hard_click(page, "a.btn:nth-child(1)", "a.btn:nth-child(1)")
            await wait_for_load(page)
            results.append(StepResult("Step 6", "OK"))

            # Step 7: div.start_btn
            state.current_step = "step 7"
            await close_google_ads(page)  # Check before clicking
            await hard_click(page, "div.start_btn", "div.start_btn")
            await wait_for_load(page)
            results.append(StepResult("Step 7", "OK"))

            # Step 8: div.btn:nth-child(2)
            state.current_step = "step 8"
            await close_google_ads(page)  # Check before clicking
            await hard_click(page, "div.btn:nth-child(2)", "div.btn:nth-child(2)")
            await wait_for_load(page)
            results.append(StepResult("Step 8", "OK"))

            # Step 9: a.btn:nth-child(2)
            state.current_step = "step 9"
            await close_google_ads(page)  # Check before clicking
            await hard_click(page, "a.btn:nth-child(2)", "a.btn:nth-child(2)")
            await wait_for_load(page)
            results.append(StepResult("Step 9", "OK"))

            # Step 10: a.get-link (wait 5 seconds)
            state.current_step = "step 10"
            await close_google_ads(page)  # Check before clicking
            await wait_for_load(page)
            await asyncio.sleep(5)
            await hard_click(page, "a.get-link", "a.get-link")
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
