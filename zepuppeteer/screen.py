"""Screen-only Brave control: OS launch, X11 focus, screenshots, OCR and input.

No browser protocol, injected JavaScript, accessibility tree, or DOM is used.
Coordinates are measured again from the current browser-window screenshot.
"""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import time
import zipfile
from uuid import uuid4

from .models import NeedsAttention


class ScreenError(RuntimeError):
    """The desktop cannot be operated reliably."""


class TargetNotFound(ScreenError):
    pass


class AmbiguousTarget(ScreenError):
    pass


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self):
        return self.x + self.width // 2, self.y + self.height // 2


@dataclass(frozen=True)
class Word:
    text: str
    box: Box
    line: tuple


def _union(boxes):
    left, top = min(b.x for b in boxes), min(b.y for b in boxes)
    return Box(left, top, max(b.x + b.width for b in boxes) - left,
               max(b.y + b.height for b in boxes) - top)


def text_boxes(words, target):
    """Match whole OCR words on one line; never choose an arbitrary duplicate."""
    lines = {}
    for word in words:
        lines.setdefault(word.line, []).append(word)
    found = []
    for line in lines.values():
        line.sort(key=lambda word: word.box.x)
        text, spans = "", []
        for word in line:
            start = len(text)
            text += word.text + " "
            spans.append((start, len(text) - 1, word.box))
        if isinstance(target, str):
            pattern = re.compile(r"(?<!\w)" + re.escape(target.strip()) + r"(?!\w)", re.I)
        else:
            pattern = target
        for match in pattern.finditer(text.rstrip()):
            boxes = [box for start, end, box in spans if end > match.start() and start < match.end()]
            if boxes:
                found.append(_union(boxes))
    return found


def validate_desktop(*, ocr=True):
    """Validate before launching a browser or installing system packages."""
    if os.name != "posix" or not os.environ.get("DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        raise ScreenError("Screen automation requires a Linux X11 desktop. Log into an Xorg session and run there; headless and Wayland sessions are unsupported.")
    if ocr and not shutil.which("tesseract"):
        raise ScreenError("Tesseract OCR is missing. On Ubuntu run: sudo apt-get install tesseract-ocr tesseract-ocr-eng xclip")
    try:
        import pyautogui
        import pyperclip
        import mss
        import pytesseract
        from Xlib import display
        from PIL import Image
        import cv2
    except ImportError as error:
        raise ScreenError("Screen dependencies are missing. Run: python -m pip install -e .") from error
    except Exception as error:
        raise ScreenError(f"Cannot access the X11 desktop: {error}") from error
    connection = display.Display()
    try:
        if not connection.has_extension("XTEST"):
            raise ScreenError("The X11 server must enable the XTEST mouse/keyboard extension.")
    finally:
        connection.close()
    if ocr:
        try:
            pyperclip.paste()
        except pyperclip.PyperclipException as error:
            raise ScreenError("No desktop clipboard backend. Install xclip or xsel.") from error


class Desktop:
    """Bind input and captures to the visible window owned by our process."""

    def __init__(self, process):
        from Xlib import display
        import pyautogui
        self.connection = display.Display()
        self.process = process
        self.window = None
        self.gui = pyautogui
        # Keep PyAutoGUI's corner fail-safe enabled.
        self.gui.FAILSAFE = True

    def _property(self, window, name):
        from Xlib import X
        prop = window.get_full_property(self.connection.intern_atom(name), X.AnyPropertyType)
        return prop.value if prop is not None else None

    def _windows(self, root):
        # EWMH is preferred; recurse for minimal X11 desktops without a WM list.
        clients = self._property(root, "_NET_CLIENT_LIST")
        if clients is not None:
            return [self.connection.create_resource_object("window", int(value)) for value in clients]
        result, pending = [], [root]
        while pending:
            parent = pending.pop()
            children = parent.query_tree().children
            result.extend(children)
            pending.extend(children)
        return result

    async def wait_for_window(self, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.returncode is not None:
                raise ScreenError(f"Brave exited before a window appeared (exit {self.process.returncode}).")
            candidates = []
            for window in self._windows(self.connection.screen().root):
                try:
                    pid = self._property(window, "_NET_WM_PID")
                    geometry = window.get_geometry()
                    if (pid is not None and os.getpgid(int(pid[0])) == self.process.pid
                            and window.get_attributes().map_state == 2
                            and geometry.width > 200 and geometry.height > 150):
                        candidates.append((geometry.width * geometry.height, window))
                except Exception:
                    continue  # A window can disappear during enumeration.
            if candidates:
                self.window = max(candidates, key=lambda item: item[0])[1]
                self.pid = int(self._property(self.window, "_NET_WM_PID")[0])
                self.focus()
                return
            await asyncio.sleep(0.2)
        raise ScreenError("Could not identify the launched Brave window by its OS process ID.")

    def focus(self):
        from Xlib import X, protocol
        root = self.connection.screen().root
        event = protocol.event.ClientMessage(window=self.window, client_type=self.connection.intern_atom("_NET_ACTIVE_WINDOW"),
                                             data=(32, [1, X.CurrentTime, 0, 0, 0]))
        root.send_event(event, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        self.window.configure(stack_mode=X.Above)
        self.window.set_input_focus(X.RevertToParent, X.CurrentTime)
        self.connection.sync()
        self.guard()

    def guard(self):
        if self.process.returncode is not None or self.window is None:
            raise ScreenError("The launched Brave window is closed.")
        focused = self.connection.get_input_focus().focus
        for _ in range(15):
            if getattr(focused, "id", None) == self.window.id:
                return
            if not hasattr(focused, "query_tree"):
                break
            focused = focused.query_tree().parent
        raise ScreenError("Brave lost keyboard focus. Bring the automation window to the foreground, then retry.")

    def bounds(self):
        self.guard()
        geometry = self.window.get_geometry()
        position = self.connection.screen().root.translate_coords(self.window, 0, 0)
        box = Box(position.x, position.y, geometry.width, geometry.height)
        width, height = self.gui.size()
        if box.x < 0 or box.y < 0 or box.x + box.width > width or box.y + box.height > height:
            raise ScreenError("Keep the entire Brave window visible on the primary monitor.")
        return box

    def title(self):
        self.guard()
        value = self._property(self.window, "_NET_WM_NAME")
        title = (value if isinstance(value, str) else bytes(value).decode("utf-8", errors="replace")) if value is not None else self.window.get_wm_name() or ""
        return re.sub(r"\s+[-–]\s+Brave$", "", title)

    def capture(self):
        import mss
        from PIL import Image
        box = self.bounds()
        # Create MSS in this thread; X11 capture handles are thread-local.
        with mss.MSS() as capture:
            shot = capture.grab({"left": box.x, "top": box.y, "width": box.width, "height": box.height})
            picture = Image.frombytes("RGB", shot.size, shot.rgb)
        return picture, box

    def hotkey(self, *keys):
        self.guard()
        self.gui.hotkey(*keys)

    def press(self, key):
        self.guard()
        self.gui.press(key)

    def click(self, box, captured_bounds):
        if self.bounds() != captured_bounds:
            raise ScreenError("Brave moved or resized after capture; retry using a fresh screenshot.")
        x, y = box.center
        self.gui.moveTo(captured_bounds.x + x, captured_bounds.y + y, duration=0.15)
        self.guard()
        self.gui.click()

    @contextmanager
    def clipboard(self):
        import pyperclip
        self.guard()
        original = pyperclip.paste()
        try:
            yield pyperclip
        finally:
            pyperclip.copy(original)

    async def paste(self, text):
        # Clipboard input handles Unicode and alternate keyboard layouts.
        with self.clipboard() as clipboard:
            clipboard.copy(text)
            # Xsel can return before its forked selection owner is ready.
            await asyncio.sleep(0.15)
            self.hotkey("ctrl", "v")
            await asyncio.sleep(0.3)

    async def address(self):
        with self.clipboard() as clipboard:
            deadline = time.monotonic() + 3
            try:
                while time.monotonic() < deadline:
                    self.hotkey("ctrl", "l")
                    self.hotkey("ctrl", "a")
                    await asyncio.sleep(0.15)
                    self.hotkey("ctrl", "c")
                    await asyncio.sleep(0.25)
                    url = clipboard.paste().strip()
                    # Never treat copied notebook text as an address. Avoid
                    # replacing clipboard ownership while Chrome handles Copy.
                    if re.match(r"^(?:https?://|file://|about:|brave:|chrome:)", url):
                        return url
                raise ScreenError("Could not read the browser address bar through Copy.")
            finally:
                self.press("esc")

    def close(self):
        self.connection.close()


def crop_region(image, region):
    if region is None:
        return image, 0, 0
    if (not isinstance(region, (list, tuple)) or len(region) != 4
            or any(type(n) not in (int, float) or not math.isfinite(n) for n in region)):
        raise ValueError("A screen region must be [left, top, width, height] fractions.")
    x, y, width, height = region
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
        raise ValueError("Screen region fractions must fit inside the browser window.")
    left, top = round(image.width * x), round(image.height * y)
    return image.crop((left, top, round(image.width * (x + width)), round(image.height * (y + height)))), left, top


def read_words(image, region=None, psm=3, scale=2, clean=False):
    import pytesseract
    picture, left, top = crop_region(image, region)
    if clean:
        from PIL import ImageOps, ImageStat
        picture = ImageOps.grayscale(picture)
        if ImageStat.Stat(picture).mean[0] < 128:
            picture = ImageOps.invert(picture)
        # Remove grey button outlines while preserving dark text strokes.
        picture = picture.point(lambda value: 0 if value < 100 else 255)
    data = pytesseract.image_to_data(picture.resize((picture.width * scale, picture.height * scale)),
                                     config=f"--psm {psm}", output_type=pytesseract.Output.DICT, timeout=10)
    words = []
    for index, text in enumerate(data["text"]):
        if text.strip() and float(data["conf"][index]) >= 55:
            words.append(Word(text.strip(), Box(left + round(data["left"][index] / scale), top + round(data["top"][index] / scale),
                                               round(data["width"][index] / scale), round(data["height"][index] / scale)),
                              tuple(data[key][index] for key in ("block_num", "par_num", "line_num"))))
    return words


def locate_text(image, text, region=None):
    # Try clean glyphs as well as the original pixels: button borders can be
    # mistaken for letters by Tesseract even at the correct resolution.
    for scale, psm, clean in ((1, 3, False), (1, 11, False), (1, 11, True),
                              (2, 3, False), (2, 11, False), (2, 11, True)):
        boxes = text_boxes(read_words(image, region, psm=psm, scale=scale, clean=clean), text)
        if boxes:
            return boxes
    return []


def image_boxes(image, template_path, region=None, confidence=0.9):
    import cv2
    import numpy as np
    if type(confidence) not in (int, float) or not 0.5 <= confidence <= 1:
        raise ValueError("Template confidence must be between 0.5 and 1.")
    template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
    if template is None or max(template.std(axis=(0, 1))) < 1:
        raise ValueError(f"Template is missing, unreadable, or blank: {template_path}")
    picture, left, top = crop_region(image, region)
    pixels = cv2.cvtColor(np.asarray(picture), cv2.COLOR_RGB2BGR)
    height, width = template.shape[:2]
    if height > pixels.shape[0] or width > pixels.shape[1]:
        return []
    scores = cv2.matchTemplate(pixels, template, cv2.TM_CCOEFF_NORMED)
    boxes = []
    for _ in range(30):
        _, best, _, (x, y) = cv2.minMaxLoc(scores)
        if best < confidence:
            break
        boxes.append(Box(left + x, top + y, width, height))
        # Suppress nearby responses for the same instance, preserving duplicates.
        scores[max(0, y - height // 2):y + height // 2 + 1,
               max(0, x - width // 2):x + width // 2 + 1] = -1
    return boxes


class ScreenPage:
    def __init__(self, browser):
        self.browser = browser
        self.url = "about:blank"
        self.closed = False

    async def goto(self, url, **_):
        if not isinstance(url, str) or not re.match(r"^(?:https?://|file://)", url):
            raise ValueError("Screen navigation requires an http(s) or local file URL.")
        await self.hotkey("ctrl", "l")
        await self.fill_text(url)
        await asyncio.sleep(0.2)
        await self.press("enter")
        self.url = url
        await asyncio.sleep(0.5)

    async def current_url(self):
        self.url = await self.browser.desktop.address()
        return self.url

    async def title(self):
        return self.browser.desktop.title()

    async def screenshot(self, *, path, **_):
        picture, _ = self.browser.desktop.capture()
        picture.save(path)

    async def words(self, region=None):
        picture, _ = self.browser.desktop.capture()
        words = await asyncio.to_thread(read_words, picture, region)
        return words or await asyncio.to_thread(read_words, picture, region, 11, 1, True)

    async def read_text(self, region=None):
        lines = {}
        for word in await self.words(region):
            lines.setdefault(word.line, []).append(word.text)
        return "\n".join(" ".join(line) for line in lines.values())

    async def find_text(self, text, *, region=None):
        picture, _ = self.browser.desktop.capture()
        return await asyncio.to_thread(locate_text, picture, text, region)

    async def wait_for_text(self, text, *, timeout=15, region=None):
        deadline = time.monotonic() + timeout
        while True:
            boxes = await self.find_text(text, region=region)
            if boxes:
                return boxes
            if time.monotonic() >= deadline:
                raise TargetNotFound(f"Text did not appear on screen: {text!r}")
            await asyncio.sleep(0.3)

    async def click_text(self, text, *, region=None, timeout=10):
        return await self.click_target(None, text=text, region=region, timeout=timeout)

    async def click_target(self, name, *, text=None, region=None, timeout=10):
        target = self.browser.targets.get(name, {}) if name else {}
        if not isinstance(target, dict):
            raise ValueError(f"Screen target {name} must be a TOML table.")
        region = target.get("region", region)
        text = target.get("text", text)
        image = target.get("image")
        if image:
            image = Path(image).expanduser()
            if not image.is_absolute():
                image = self.browser.target_root / image
        if not image and text is None:
            raise NeedsAttention(f"Configure settings.screen.targets.{name}.image with a cropped screenshot of this icon, and optionally a region. No click was made.")
        deadline = time.monotonic() + timeout
        while True:
            picture, bounds = self.browser.desktop.capture()
            if image:
                boxes = await asyncio.to_thread(image_boxes, picture, image, region, target.get("confidence", 0.9))
            else:
                boxes = await asyncio.to_thread(locate_text, picture, text, region)
            if len(boxes) > 1:
                raise AmbiguousTarget(f"Multiple matches for {name or text!r}; narrow its screen region.")
            if boxes:
                # Recheck the target after recognition so a loading page cannot
                # move a button between OCR and mouse input.
                fresh, current_bounds = self.browser.desktop.capture()
                if current_bounds != bounds:
                    continue
                if image:
                    confirmed = await asyncio.to_thread(image_boxes, fresh, image, region, target.get("confidence", 0.9))
                else:
                    confirmed = await asyncio.to_thread(locate_text, fresh, text, region)
                if len(confirmed) == 1 and confirmed[0] == boxes[0]:
                    self.browser.desktop.click(boxes[0], bounds)
                    await asyncio.sleep(0.2)
                    return
            if time.monotonic() >= deadline:
                raise TargetNotFound(f"Could not uniquely locate {name or text!r} on the current screen.")
            await asyncio.sleep(0.2)

    async def wait_for_target(self, name, *, timeout=15, region=None):
        target = self.browser.targets.get(name, {})
        if not target.get("image") and not target.get("text"):
            raise NeedsAttention(f"Configure settings.screen.targets.{name} with an image or text and a region to verify this visual state.")
        image = Path(target["image"]).expanduser() if target.get("image") else None
        if image is not None and not image.is_absolute():
            image = self.browser.target_root / image
        deadline = time.monotonic() + timeout
        while True:
            picture, _ = self.browser.desktop.capture()
            area = target.get("region", region)
            boxes = (await asyncio.to_thread(image_boxes, picture, image, area, target.get("confidence", 0.9))
                     if image else await asyncio.to_thread(locate_text, picture, target["text"], area))
            if len(boxes) > 1:
                raise AmbiguousTarget(f"Multiple matches for {name!r}; narrow its screen region.")
            if boxes:
                return boxes[0]
            if time.monotonic() >= deadline:
                raise TargetNotFound(f"Visual state did not appear: {name}")
            await asyncio.sleep(0.3)

    async def hotkey(self, *keys):
        self.browser.desktop.hotkey(*keys)
        await asyncio.sleep(0.1)

    async def press(self, key):
        self.browser.desktop.press(key)
        await asyncio.sleep(0.1)

    async def type_text(self, text):
        if text.isascii() and "\n" not in text and "\t" not in text:
            # Ordinary fields use real keystrokes. Chunking keeps cancellation
            # and focus checks responsive during longer entries.
            for offset in range(0, len(text), 64):
                self.browser.desktop.guard()
                self.browser.desktop.gui.write(text[offset:offset + 64], interval=0.015)
                await asyncio.sleep(0)
        else:
            await self.browser.desktop.paste(text)

    async def fill_text(self, text):
        """Replace a focused input and verify its value through normal Copy."""
        await self.hotkey("ctrl", "a")
        if text:
            await self.type_text(text)
        else:
            await self.press("backspace")
        if await self.copy_editor_text() != text:
            raise ScreenError("The focused input did not contain the requested text after pasting.")
        await self.press("right")

    async def copy_editor_text(self):
        with self.browser.desktop.clipboard() as clipboard:
            sentinel = uuid4().hex
            clipboard.copy(sentinel)
            await asyncio.sleep(0.15)
            self.browser.desktop.hotkey("ctrl", "a")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                self.browser.desktop.hotkey("ctrl", "c")
                await asyncio.sleep(0.15)
                copied = clipboard.paste()
                if copied != sentinel:
                    return copied
            return ""  # Empty selections do not replace the clipboard in Brave.

    async def scroll(self, clicks):
        box = self.browser.desktop.bounds()
        self.browser.desktop.gui.moveTo(box.x + box.width // 2, box.y + box.height * 3 // 4)
        self.browser.desktop.guard()
        self.browser.desktop.gui.scroll(clicks)
        await asyncio.sleep(0.3)

    async def close(self):
        if not self.closed:
            await self.hotkey("ctrl", "w")
            self.closed = True
            self.browser.pages.remove(self)


class ScreenTrace:
    """Failure archive with start/end screenshots; contains no browser internals."""
    def __init__(self, browser):
        self.browser = browser
        self.start_image = None

    async def start(self, **_):
        self.start_image, _ = self.browser.desktop.capture()

    async def stop(self, path=None):
        try:
            if path is not None:
                import io
                pictures = [("start.png", self.start_image)]
                metadata = {"backend": "screen", "capture": "browser window"}
                try:
                    pictures.append(("end.png", self.browser.desktop.capture()[0]))
                except Exception as error:
                    metadata["end_capture_error"] = str(error)
                with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name, picture in pictures:
                        if picture is not None:
                            stream = io.BytesIO()
                            picture.save(stream, format="PNG")
                            archive.writestr(name, stream.getvalue())
                    archive.writestr("metadata.json", json.dumps(metadata))
        finally:
            self.start_image = None


class ScreenBrowser:
    def __init__(self, process, desktop, profile):
        self.process, self.desktop = process, desktop
        self.pages = []
        settings = profile.settings.get("screen", {})
        if not isinstance(settings, dict) or not isinstance(settings.get("targets", {}), dict):
            raise ValueError("settings.screen and its targets must be TOML tables.")
        self.targets = settings.get("targets", {})
        self.target_root = profile.manifest_path.parent if profile.manifest_path else Path.cwd()
        self.tracing = ScreenTrace(self)

    async def new_page(self):
        await self.desktop_new_tab()
        page = ScreenPage(self)
        self.pages.append(page)
        return page

    async def desktop_new_tab(self):
        self.desktop.hotkey("ctrl", "t")
        await asyncio.sleep(1)

    async def wait_closed(self):
        await self.process.wait()

    async def close(self):
        try:
            if self.process.returncode is None:
                # SIGTERM lets Brave flush profile data, even after focus is lost.
                try:
                    os.kill(self.desktop.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass  # A user can close the window just before cleanup.
                try:
                    await asyncio.wait_for(self.process.wait(), 15)
                except asyncio.TimeoutError:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
                    raise ScreenError("Brave did not exit gracefully; inspect its profile before retrying.")
        finally:
            self.desktop.close()


async def _stop_process(process):
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 15)
        except asyncio.TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


async def launch_browser(executable, profile, *, headless=False, proxy=None, interactive=False):
    from .brave import native_command, browser_environment, prepare_profile_for_launch
    if headless:
        raise ScreenError("--headless is unsupported: screen automation requires a visible X11 desktop.")
    validate_desktop(ocr=not interactive)
    prepare_profile_for_launch(profile)
    command = native_command(executable, profile, proxy=proxy)
    startup = asyncio.create_task(asyncio.create_subprocess_exec(*command, env=browser_environment(),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True))
    try:
        process = await asyncio.shield(startup)
    except asyncio.CancelledError:
        process = await startup
        await _stop_process(process)
        raise
    desktop = None
    try:
        desktop = Desktop(process)
        await desktop.wait_for_window()
        return ScreenBrowser(process, desktop, profile)
    except BaseException:
        try:
            if process.returncode is None:
                await _stop_process(process)
        finally:
            if desktop is not None:
                desktop.close()
        raise
