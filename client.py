# importing necessary libraries/packages
import sys
import asyncio
from typing import Optional, Any
from contextlib import AsyncExitStack
import os
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, FileResponse, HTMLResponse
from fastapi import UploadFile, File, Form
from fastapi.responses import JSONResponse
import tempfile
import shutil

# library installation fallback
def _fail_missing(module_name: str, install_hint: str | None = None) -> None:
    print(f"Missing dependency: {module_name}", file=sys.stderr)
    if install_hint:
        print("Install with:", file=sys.stderr)
        print(f"  {install_hint}", file=sys.stderr)
    else:
        print(f"Try: pip install {module_name}", file=sys.stderr)
    sys.exit(1)

# mcp tool import
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ModuleNotFoundError:
    _fail_missing('mcp', 'pip install mcp')

try:
    from anthropic import Anthropic
except ModuleNotFoundError:
    _fail_missing('anthropic', 'pip install anthropic')

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    _fail_missing('python-dotenv', 'pip install python-dotenv')

load_dotenv()

try:
    from playwright.async_api import async_playwright, Browser, Page
    HAS_PLAYWRIGHT = True
except ModuleNotFoundError:
    HAS_PLAYWRIGHT = False
    async_playwright = None
    Browser = None
    Page = None

try:
    import mss
    HAS_MSS = True
except ModuleNotFoundError:
    HAS_MSS = False

try:
    from PIL import Image
    import io
    HAS_PIL = True
except ModuleNotFoundError:
    HAS_PIL = False

try:
    import importlib
    _whisper_module = importlib.import_module("whisper")
    HAS_WHISPER = True
except Exception:
    _whisper_module = None
    HAS_WHISPER = False

# BrowserManager class for handling browser interactions
class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.page = None
        self.use_screen_capture = True
        self.click_offset_x = 0
        self.click_offset_y = 0
        try:
            self.click_debug = os.environ.get('CLICK_DEBUG', '0') in ('1', 'true', 'True')
        except Exception:
            self.click_debug = False
        # hard-coded coordinates for testing purposes
        self.hardcoded_coords = {
            # homelessness status
            'homelessness_yes': (1200, 230),
            'homelessness_no': (1330, 230),
            # address fields
            'address_line_1': (1170, 530),
            'address_line_2': (1170, 635),
            'address1': (1170, 530),
            'address2': (1170, 635),
            # city / state / zip
            'city': (1170, 800),
            'state': (1170, 910),
            'zip': (1170, 1020),
            'zip_code': (1170, 1020),
        }

        self.hardcoded_default_offset = (50, 100)
        self.hardcoded_offsets = {
            'homelessness_no': (250, 50),
        }
        import re as _re
        self._hardcoded_norm_map = {}
        for k in self.hardcoded_coords.keys():
            nk = _re.sub(r'[^a-z0-9]', '', k.lower())
            self._hardcoded_norm_map[nk] = k

    async def click_named(self, name: str):
        """Click a hard-coded logical target by name.

        The provided coordinates are the pre-offset screen/page coordinates. The
        BrowserManager.click method will apply the configured offsets.
        Returns a tuple (x,y) of the target used, or None if not found.
        This is for testing purposes.
        """
        if not name:
            return None
        raw = str(name or '').lower().strip()
        key = raw
        coord = self.hardcoded_coords.get(key)
        if coord is None:
            import re
            n = re.sub(r'[^a-z0-9]', '', raw)
            mapped = self._hardcoded_norm_map.get(n)
            if mapped:
                key = mapped
                coord = self.hardcoded_coords.get(key)
        if coord is None:
            try:
                import re
                n = re.sub(r'[^a-z0-9]', '', raw)
                matches = [orig for norm, orig in self._hardcoded_norm_map.items() if n in norm or norm in n]
                if len(matches) == 1:
                    key = matches[0]
                    coord = self.hardcoded_coords.get(key)
            except Exception:
                pass
        if not coord:
            return None
        offset = self.hardcoded_offsets.get(key, self.hardcoded_default_offset)
        try:
            dx, dy = int(offset[0]), int(offset[1])
        except Exception:
            dx, dy = 0, 0

        target_x = int(coord[0]) + dx
        target_y = int(coord[1]) + dy
        if getattr(self, 'click_debug', False):
            print(f"BrowserManager.click_named: key={key} base={coord} offset=({dx},{dy}) target=({target_x},{target_y})", file=sys.stderr)
        # perform the click using raw coordinates (no global offsets)
        await self.click(target_x, target_y, apply_offset=False)
        return (target_x, target_y)
        
    async def start(self):
        if self.use_screen_capture:
            if not HAS_MSS:
                raise RuntimeError("mss not installed. Install with: pip install mss pillow")
            return
        if not HAS_PLAYWRIGHT:
            raise RuntimeError("Playwright not installed. Install with: pip install playwright && playwright install")
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=False)
            # use the user's screen coordinate space; default to 1710x1107 per request (testing viewport size)
            self.page = await self.browser.new_page(viewport={"width": 1710, "height": 1107})
        except Exception as e:
            raise RuntimeError(f"Failed to start browser: {str(e)}. Make sure to run: playwright install")
        
    async def screenshot(self) -> str:
        if self.use_screen_capture:
            if not HAS_MSS or not HAS_PIL:
                raise RuntimeError("Screen capture requires: pip install mss pillow")
            
            def capture_screen():
                import base64
                with mss.mss() as sct:
                    monitor = sct.monitors[1]
                    screenshot = sct.grab(monitor)
                    img = Image.frombytes('RGB', screenshot.size, screenshot.bgra, 'raw', 'BGRX')
                    buffered = io.BytesIO()
                    img.save(buffered, format="PNG")
                    return base64.b64encode(buffered.getvalue()).decode('utf-8')
            
            await asyncio.sleep(0.1)
            return await asyncio.to_thread(capture_screen)
        else:
            if not self.page:
                await self.start()
            screenshot_bytes = await self.page.screenshot()
            import base64
            return base64.b64encode(screenshot_bytes).decode('utf-8')
    
    async def navigate(self, url: str):
        if self.use_screen_capture:
            import webbrowser
            await asyncio.to_thread(webbrowser.open, url)
            return
        if not self.page:
            await self.start()
        await self.page.goto(url)
        
    async def click(self, x: int, y: int, apply_offset: bool = True):
        # apply offset after coordinates are decided
        # to use raw coordinates
        orig_x, orig_y = x, y
        apply_offset = True
        # backwards-compatible: allow callers to pass a third boolean arg
        # e.g. await self.click(x, y, apply_offset=False)
        try:
            pass
        except Exception:
            pass
        try:
            # apply offsets only if requested
            if apply_offset:
                x = int(x) + int(self.click_offset_x)
                y = int(y) + int(self.click_offset_y)
            else:
                x = int(x)
                y = int(y)
        except Exception:
            pass
        if getattr(self, 'click_debug', False):
            # indicate whether offsets were used
            try:
                if apply_offset:
                    print(f"BrowserManager.click: orig=({orig_x},{orig_y}) adjusted=({x},{y})", file=sys.stderr)
                else:
                    print(f"BrowserManager.click: orig=({orig_x},{orig_y}) no-offset used=({x},{y})", file=sys.stderr)
            except Exception:
                pass
        if self.use_screen_capture:
            try:
                import pyautogui
                pyautogui.FAILSAFE = False
                await asyncio.to_thread(pyautogui.click, x, y)
                await asyncio.sleep(0.5)
            except ImportError:
                raise RuntimeError("pyautogui not installed. Install with: pip install pyautogui")
        else:
            if not self.page:
                await self.start()
            await self.page.mouse.click(x, y)
    
    async def double_click(self, x: int, y: int):
        orig_x, orig_y = x, y
        try:
            if self.use_screen_capture:
                x = int(x) + int(self.click_offset_x)
                y = int(y) + int(self.click_offset_y)
            else:
                x = int(x)
                y = int(y)
        except Exception:
            pass
        if getattr(self, 'click_debug', False):
            print(f"BrowserManager.double_click: orig=({orig_x},{orig_y}) adjusted=({x},{y})", file=sys.stderr)
        if self.use_screen_capture:
            try:
                import pyautogui
                pyautogui.FAILSAFE = False
                await asyncio.to_thread(pyautogui.doubleClick, x, y)
                await asyncio.sleep(0.5)
            except ImportError:
                raise RuntimeError("pyautogui not installed. Install with: pip install pyautogui")
        else:
            if not self.page:
                await self.start()
            await self.page.mouse.dblclick(x, y)
    
    async def triple_click(self, x: int, y: int):
        orig_x, orig_y = x, y
        try:
            if self.use_screen_capture:
                x = int(x) + int(self.click_offset_x)
                y = int(y) + int(self.click_offset_y)
            else:
                x = int(x)
                y = int(y)
        except Exception:
            pass
        if getattr(self, 'click_debug', False):
            print(f"BrowserManager.triple_click: orig=({orig_x},{orig_y}) adjusted=({x},{y})", file=sys.stderr)
        if self.use_screen_capture:
            try:
                import pyautogui
                pyautogui.FAILSAFE = False
                await asyncio.to_thread(pyautogui.click, x, y)
                await asyncio.sleep(0.1)
                await asyncio.to_thread(pyautogui.click, x, y)
                await asyncio.sleep(0.1)
                await asyncio.to_thread(pyautogui.click, x, y)
                await asyncio.sleep(0.5)
            except ImportError:
                raise RuntimeError("pyautogui not installed. Install with: pip install pyautogui")
        else:
            if not self.page:
                await self.start()
            await self.page.mouse.click(x, y)
            await self.page.mouse.click(x, y)
            await self.page.mouse.click(x, y)
    
    async def right_click(self, x: int, y: int):
        orig_x, orig_y = x, y
        try:
            if self.use_screen_capture:
                x = int(x) + int(self.click_offset_x)
                y = int(y) + int(self.click_offset_y)
            else:
                x = int(x)
                y = int(y)
        except Exception:
            pass
        if getattr(self, 'click_debug', False):
            print(f"BrowserManager.right_click: orig=({orig_x},{orig_y}) adjusted=({x},{y})", file=sys.stderr)
        if self.use_screen_capture:
            try:
                import pyautogui
                pyautogui.FAILSAFE = False
                await asyncio.to_thread(pyautogui.rightClick, x, y)
                await asyncio.sleep(0.3)
            except ImportError:
                raise RuntimeError("pyautogui not installed. Install with: pip install pyautogui")
        else:
            if not self.page:
                await self.start()
            await self.page.mouse.click(x, y, button='right')
    
    async def middle_click(self, x: int, y: int):
        orig_x, orig_y = x, y
        try:
            if self.use_screen_capture:
                x = int(x) + int(self.click_offset_x)
                y = int(y) + int(self.click_offset_y)
            else:
                x = int(x)
                y = int(y)
        except Exception:
            pass
        if getattr(self, 'click_debug', False):
            print(f"BrowserManager.middle_click: orig=({orig_x},{orig_y}) adjusted=({x},{y})", file=sys.stderr)
        if self.use_screen_capture:
            try:
                import pyautogui
                pyautogui.FAILSAFE = False
                await asyncio.to_thread(pyautogui.middleClick, x, y)
                await asyncio.sleep(0.3)
            except ImportError:
                raise RuntimeError("pyautogui not installed. Install with: pip install pyautogui")
        else:
            if not self.page:
                await self.start()
            await self.page.mouse.click(x, y, button='middle')
        
    async def type_text(self, text: str):
        if self.use_screen_capture:
            try:
                import pyautogui
                pyautogui.FAILSAFE = False
                await asyncio.sleep(0.3)
                for char in text:
                    await asyncio.to_thread(pyautogui.press, char)
                    await asyncio.sleep(0.05)
            except ImportError:
                raise RuntimeError("pyautogui not installed. Install with: pip install pyautogui")
        else:
            if not self.page:
                await self.start()
            await self.page.keyboard.type(text)
        
    async def key_press(self, key: str):
        if self.use_screen_capture:
            try:
                import pyautogui
                await asyncio.to_thread(pyautogui.press, key)
            except ImportError:
                raise RuntimeError("pyautogui not installed. Install with: pip install pyautogui")
        else:
            if not self.page:
                await self.start()
            await self.page.keyboard.press(key)
        
    async def mouse_move(self, x: int, y: int):
        if self.use_screen_capture:
            try:
                import pyautogui
                await asyncio.to_thread(pyautogui.moveTo, x, y)
            except ImportError:
                raise RuntimeError("pyautogui not installed. Install with: pip install pyautogui")
        else:
            if not self.page:
                await self.start()
            await self.page.mouse.move(x, y)
        
    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

# main mcp client class
class MCPClient:
    def __init__(self):
        self.exit_stack = AsyncExitStack()
        self.anthropic = Anthropic()
        self.sessions: dict[str, ClientSession] = {}
        self.tool_map: dict[str, tuple[str, str]] = {}
        self.browser = BrowserManager()

    def _get_medical_home_url(self) -> str:
        raw_med_url = os.environ.get('MEDICAL_HOME_URL', '')
        canonical = 'https://www.dhcs.ca.gov/Pages/myMedi-Cal.aspx'
        def _is_placeholder(u: str) -> bool:
            if not u: return True
            lu = u.lower()
            placeholders = ['example.', 'localhost', '127.0.0.1', '::1', 'example-medical-home']
            return any(p in lu for p in placeholders)
        if _is_placeholder(raw_med_url):
            return canonical
        return raw_med_url

    async def _call_first_tool_for_server(self, server_name: str, input_obj: Any) -> Any:
        tool_name = None
        for namespaced, (sname, orig) in self.tool_map.items():
            if sname == server_name:
                tool_name = orig
                break
        if not tool_name:
            return None
        session = self.sessions.get(server_name)
        if not session:
            return None
        try:
            result = await session.call_tool(tool_name, input_obj)
            return result
        except Exception:
            return None

    async def connect_to_server(self, server_name: str, server_script_path: str):
        is_python = server_script_path.endswith('.py')
        is_js = server_script_path.endswith('.js')
        if not (is_python or is_js):
            raise ValueError("Server script must be a .py or .js file")

        command = sys.executable if is_python else "node"
        server_params = StdioServerParameters(
            command=command,
            args=[server_script_path],
            env=None,
        )

        try:
            stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
        except FileNotFoundError as exc:
            bin_name = command
            raise RuntimeError(
                f"Failed to start MCP server '{server_name}' with command '{bin_name}'.\n"
                "Ensure the executable is on PATH or set the correct interpreter.\n"
                f"Original error: {exc}"
            ) from exc
        stdio, write = stdio_transport
        session = await self.exit_stack.enter_async_context(ClientSession(stdio, write))
        await session.initialize()

        self.sessions[server_name] = session

        resp = await session.list_tools()
        tools = getattr(resp, 'tools', [])
        for tool in tools:
            safe_tool_name = tool.name.replace('.', '_')
            namespaced = f"{server_name}_{safe_tool_name}"
            self.tool_map[namespaced] = (server_name, tool.name)

        print(f"Connected to {server_name} with tools:", list(self.tool_map.keys()))

    async def process_query(self, query: str, lang: str | None = None, previous_messages: Optional[list] = None, verbosity: str = 'verbose') -> dict:
        messages = list(previous_messages) if previous_messages else []
        lang_map = {"es": "Spanish", "zh": "Mandarin", "en": "English"}
        system_prompt: Optional[str] = None
        if lang and lang != "auto":
            lang_name = lang_map.get(lang, lang)
            # prompt for civic bridge including language usage
            system_prompt = (
                f"You are a helpful assistant. Reply in {lang_name}. "
                "Do not assume the user's message is a JSON object; treat it as plain text. "
                "Keep replies conversational and concise."
            )

        if verbosity and verbosity == 'concise':
            extra = (
                "\n\nIMPORTANT: For this conversation, format your entire reply as a list of concise bullet points. "
                "Start each bullet with a dash and a space (for example: '- Item'). Do NOT use numbered lists or other bullet characters — use the dash '-' exclusively. "
                "Do NOT include long paragraphs. Use short bullets and, when applicable, keep each bullet to one sentence. After each dashed line, add a new line."
            )
            if system_prompt:
                system_prompt += extra
            else:
                system_prompt = (
                    "You are a helpful assistant. Reply using concise bullet points only." + extra
                )

        messages.append({"role": "user", "content": query})

        # if the user provided a full name (first + last) in response to a prompt
        # that asked for their first name, auto-fill both first and last
        # name fields when a Playwright page is available. If Playwright / page
        # isn't available, add a helper user message with both names so the
        # assistant can continue and perform the appropriate tool actions.
        try:
            q_trim = (query or '').strip()
            tokens = [t for t in q_trim.split() if t]
            looks_like_full_name = len(tokens) >= 2 and len(q_trim) < 120

            def _assistant_last_asked_for_first(messages_list: list) -> bool:
                # scan backwards for the last assistant message and check if it
                # asked for the first name specifically
                for m in reversed(messages_list[:-1]):
                    if not isinstance(m, dict):
                        continue
                    if m.get('role') != 'assistant':
                        continue
                    text = m.get('content')
                    if isinstance(text, list):
                        # flatten content pieces
                        txt = ' '.join((p.get('text') if isinstance(p, dict) else str(p)) for p in text)
                        text = txt
                    if not text:
                        continue
                    low = str(text).lower()
                    # common phrasing that asks for first name
                    if 'first name' in low or 'given name' in low or "what is your first" in low:
                        return True
                    # if the assistant recently asked generically for name, still consider
                    if 'your name' in low and 'first' in low:
                        return True
                return False

            if looks_like_full_name and _assistant_last_asked_for_first(messages):
                first_name = tokens[0]
                last_name = ' '.join(tokens[1:])

                # if we have a Playwright page (non-screen-capture mode), try to
                # locate first/last inputs via common selectors and fill them.
                if HAS_PLAYWRIGHT and getattr(self.browser, 'page', None) is not None and not getattr(self.browser, 'use_screen_capture', False):
                    try:
                        page = self.browser.page
                        # common selector patterns for first / last name fields
                        first_selectors = [
                            "input[name*='first' i]",
                            "input[id*='first' i]",
                            "input[placeholder*='first' i]",
                            "input[aria-label*='first' i]",
                            "input[name*='given' i]",
                            "input[id*='given' i]",
                        ]
                        last_selectors = [
                            "input[name*='last' i]",
                            "input[id*='last' i]",
                            "input[placeholder*='last' i]",
                            "input[aria-label*='last' i]",
                            "input[name*='surname' i]",
                            "input[name*='family' i]",
                        ]

                        first_found = None
                        last_found = None
                        for sel in first_selectors:
                            try:
                                el = await page.query_selector(sel)
                            except Exception:
                                el = None
                            if el:
                                first_found = el
                                break
                        for sel in last_selectors:
                            try:
                                el = await page.query_selector(sel)
                            except Exception:
                                el = None
                            if el:
                                last_found = el
                                break

                        if first_found:
                            try:
                                await first_found.click()
                                await first_found.fill(first_name)
                            except Exception:
                                pass
                        if last_found:
                            try:
                                await last_found.click()
                                await last_found.fill(last_name)
                            except Exception:
                                pass
                        # inform the conversation history that we auto-filled fields
                        assistant_text = f"Auto-filled first name: {first_name} and last name: {last_name}. What else can I help with?"
                        messages.append({"role": "assistant", "content": assistant_text})
                        # return immediately so the assistant message is sent and we don't
                        # continue processing which could cause the model to attempt the same action.
                        return {"response": assistant_text, "messages": messages}
                    except Exception:
                        # fall back to adding a helper user message if DOM approach fails
                        messages.append({"role": "user", "content": f"First name: {first_name}\nLast name: {last_name}"})
                else:
                    # no DOM automation available; add a helper message so the
                    # assistant has both values and can instruct the computer tool
                    messages.append({"role": "user", "content": f"First name: {first_name}\nLast name: {last_name}"})
                    assistant_text = f"Received first and last name. First name: {first_name}; Last name: {last_name}. What else can I help with?"
                    messages.append({"role": "assistant", "content": assistant_text})
                    return {"response": assistant_text, "messages": messages}
        except Exception:
            # don't let name-splitting interfere with normal processing
            pass
        # If the assistant just asked for a specific address-related field
        # (address, city, or zip) and the user's reply contains that value,
        # try to auto-fill that single field and immediately return an
        # assistant message asking for the next missing piece.
        try:
            def _assistant_last_asked_for_field(messages_list: list, field_keys=('address','city','zip')) -> Optional[str]:
                for m in reversed(messages_list[:-1]):
                    if not isinstance(m, dict):
                        continue
                    if m.get('role') != 'assistant':
                        continue
                    text = m.get('content')
                    if isinstance(text, list):
                        txt = ' '.join((p.get('text') if isinstance(p, dict) else str(p)) for p in text)
                        text = txt
                    if not text:
                        continue
                    low = str(text).lower()
                    # detect explicit asks for address/city/zip
                    for fk in field_keys:
                        if fk in low and ('what' in low or '?' in low or 'please' in low or 'enter' in low or 'provide' in low):
                            return fk
                    # also accept patterns like "What's the city?" or "ZIP/postal code?"
                    if 'city' in low and ('what' in low or '?' in low):
                        return 'city'
                    if 'zip' in low or 'postal code' in low:
                        return 'zip'
                return None

            asked_field = _assistant_last_asked_for_field(messages)
            if asked_field:
                val = (query or '').strip()
                if val:
                    # Basic heuristics per field
                    ok = False
                    if asked_field == 'zip':
                        import re as _re
                        ok = bool(_re.search(r"\d{3,10}", val))
                    elif asked_field == 'city':
                        ok = len(val) <= 100 and not val.endswith('?')
                    else:
                        # address: require some digits or street-like tokens or reasonable length
                        import re as _re
                        ok = bool(_re.search(r"\d+", val)) or any(tok in val.lower() for tok in ('street','st','ave','road','rd','lane','ln','blvd','drive','dr'))

                    if ok:
                        # attempt to fill the single field
                        try:
                            if HAS_PLAYWRIGHT and getattr(self.browser, 'page', None) is not None and not getattr(self.browser, 'use_screen_capture', False):
                                sel_map = {
                                    'address': 'address_line_1',
                                    'city': 'city',
                                    'zip': 'zip',
                                }
                                key = sel_map.get(asked_field, asked_field)
                                coord = await self.browser.click_named(key)
                                if coord:
                                    await self.browser.triple_click(coord[0], coord[1])
                                    await asyncio.sleep(0.05)
                                    await self.browser.type_text(val)
                                    assistant_text = f"Auto-filled {asked_field}: {val}."
                                    # determine next missing field to ask
                                    next_field = None
                                    for f in ('address','city','zip'):
                                        if f != asked_field:
                                            next_field = f
                                            break
                                    q_map = {
                                        'address': "What's the street address (Address Line 1)?",
                                        'city': "What's the city?",
                                        'zip': "What's the ZIP/postal code?",
                                    }
                                    if next_field:
                                        assistant_text = assistant_text + ' ' + q_map.get(next_field)
                                    else:
                                        assistant_text = assistant_text + ' What else can I help with?'
                                    messages.append({"role": "assistant", "content": assistant_text})
                                    return {"response": assistant_text, "messages": messages}
                            else:
                                # fallback: add helper user message and respond
                                cap = asked_field.capitalize()
                                messages.append({"role": "user", "content": f"{cap}: {val}"})
                                assistant_text = f"Received {asked_field}: {val}. What's next?"
                                messages.append({"role": "assistant", "content": assistant_text})
                                return {"response": assistant_text, "messages": messages}
                        except Exception:
                            # on failure, continue normal flow
                            pass
        except Exception:
            pass
        # if the user provided multiple address-related fields in one message
        # (address, city, zip), attempt to fill them automatically via the
        # computer automation BEFORE sending the query to the model. This
        # ensures the form is populated and the assistant can continue.
        def _parse_address_fields(text: str) -> dict:
            import re
            fields = {}
            if not text:
                return fields
            # look for explicit labeled fields like 'Address: ...', 'City: ...', 'Zip: ...'
            patterns = {
                'address': r"(?i)^(?:address|address line 1|address1|addr1)\s*[:\-]\s*(.+)$",
                'city': r"(?i)^city\s*[:\-]\s*(.+)$",
                'zip': r"(?i)^(?:zip|zip code|postal code)\s*[:\-]\s*(\d{3,10})$",
            }
            # check line-by-line
            for line in text.splitlines():
                line = line.strip()
                for k, pat in patterns.items():
                    m = re.search(pat, line)
                    if m:
                        fields[k] = m.group(1).strip()
            # If no labeled lines found, try comma-separated parse as a fallback
            if not fields:
                # e.g. '123 Main St, San Francisco, CA 94110' or '123 Main St, San Francisco 94110'
                parts = [p.strip() for p in re.split(r',|;|\n', text) if p.strip()]
                if len(parts) >= 2:
                    # last part may contain zip
                    last = parts[-1]
                    z = re.search(r"(\d{5}(?:-\d{4})?|\d{3,10})", last)
                    if z:
                        fields['zip'] = z.group(1)
                        # city likely the second-to-last
                        if len(parts) >= 2:
                            fields.setdefault('city', parts[-2])
                        # address is the first part
                        fields.setdefault('address', parts[0])
            return fields

        try:
            addr_fields = _parse_address_fields(query or '')
            # only trigger auto-fill if user provided at least two of the requested fields
            if len([k for k in ('address', 'city', 'zip') if k in addr_fields]) >= 2:
                # if Playwright page automation is available, perform the fills
                if HAS_PLAYWRIGHT and getattr(self.browser, 'page', None) is not None and not getattr(self.browser, 'use_screen_capture', False):
                    try:
                        # Fill in order: address, city, zip
                        filled = []
                        if 'address' in addr_fields:
                            val = addr_fields['address']
                            # click and triple-click to focus
                            coord = await self.browser.click_named('address_line_1')
                            if coord:
                                await self.browser.triple_click(coord[0], coord[1])
                                await asyncio.sleep(0.1)
                                await self.browser.type_text(val)
                                filled.append('address')
                        if 'city' in addr_fields:
                            val = addr_fields['city']
                            coord = await self.browser.click_named('city')
                            if coord:
                                await self.browser.triple_click(coord[0], coord[1])
                                await asyncio.sleep(0.1)
                                await self.browser.type_text(val)
                                filled.append('city')
                        if 'zip' in addr_fields:
                            val = addr_fields['zip']
                            coord = await self.browser.click_named('zip')
                            if coord:
                                await self.browser.triple_click(coord[0], coord[1])
                                await asyncio.sleep(0.1)
                                await self.browser.type_text(val)
                                filled.append('zip')
                        if filled:
                            # inform the conversation that fields were auto-filled
                            # and ask for the next missing piece in the same assistant message.
                            remaining = [f for f in ('address', 'city', 'zip') if f not in filled and f in addr_fields or f not in filled and f not in addr_fields]
                            # determine next missing field in logical order
                            next_field = None
                            for f in ('address', 'city', 'zip'):
                                if f not in filled:
                                    next_field = f
                                    break

                            q_map = {
                                'address': "What's the street address (Address Line 1)?",
                                'city': "What's the city?",
                                'zip': "What's the ZIP/postal code?",
                            }

                            if next_field:
                                assistant_text = f"Auto-filled fields: {', '.join(filled)}. {q_map.get(next_field, 'Please provide the next field.') }"
                            else:
                                assistant_text = f"Auto-filled fields: {', '.join(filled)}. What else can I help with?"

                            messages.append({"role": "assistant", "content": assistant_text})
                            # return immediately so the assistant message asking for the
                            # next field is sent to the user before any further processing.
                            return {"response": assistant_text, "messages": messages}
                    except Exception:
                        # if automation fails, fall back to adding helper user message
                        helper_lines = []
                        for k in ('address', 'city', 'zip'):
                            if k in addr_fields:
                                helper_lines.append(f"{k.capitalize()}: {addr_fields[k]}")
                        if helper_lines:
                            messages.append({"role": "user", "content": "\n".join(helper_lines)})
                            # ask for the next missing field in the same assistant message
                            next_field = None
                            for f in ('address', 'city', 'zip'):
                                if f not in addr_fields:
                                    next_field = f
                                    break
                            q_map = {
                                'address': "What's the street address (Address Line 1)?",
                                'city': "What's the city?",
                                'zip': "What's the ZIP/postal code?",
                            }
                            if next_field:
                                ask = q_map.get(next_field, 'Please provide the next field.')
                            else:
                                ask = "Thanks; I have those fields. What else can I help with?"
                            messages.append({"role": "assistant", "content": ask})
                            return {"response": ask, "messages": messages}
                else:
                    # no DOM automation available; add helper user message so the
                    # assistant has the values and can instruct the computer tool
                    helper_lines = []
                    for k in ('address', 'city', 'zip'):
                        if k in addr_fields:
                            helper_lines.append(f"{k.capitalize()}: {addr_fields[k]}")
                    if helper_lines:
                        messages.append({"role": "user", "content": "\n".join(helper_lines)})
                        # ask for the next missing field in the same assistant message
                        next_field = None
                        for f in ('address', 'city', 'zip'):
                            if f not in addr_fields:
                                next_field = f
                                break
                        q_map = {
                            'address': "What's the street address (Address Line 1)?",
                            'city': "What's the city?",
                            'zip': "What's the ZIP/postal code?",
                        }
                        if next_field:
                            ask = q_map.get(next_field, 'Please provide the next field.')
                        else:
                            ask = "Thanks — I have those fields. What else can I help with?"
                        messages.append({"role": "assistant", "content": ask})
                        return {"response": ask, "messages": messages}
        except Exception:
            # fail silently and continue
            pass
        try:
            qlow = (query or "").lower()
            if any(k in qlow for k in ("medicaid", "medicare", "medcal", "medic-al", "medicaid", "mymedi-cal", "medica", "medic")):
                resp = await self._call_first_tool_for_server("eligibility", {"query": query})
                try:
                    eligible = False
                    if resp is None:
                        eligible = False
                    else:
                        eligible = bool(getattr(resp, 'eligible', None) or (isinstance(resp, dict) and resp.get('eligible')))
                except Exception:
                    eligible = False
                if eligible:
                    med_url = self._get_medical_home_url()
                    actions = [
                        {"type": "navigate", "selector": "", "value": med_url},
                        {"type": "wait", "ms": 1000},
                        {"type": "click", "selector": "a.apply, a.start, button.apply, button.start, a:contains('Apply for Medi-Cal')"},
                        {"type": "wait", "ms": 500},
                    ]
                    # bold the eligible part for the frontend. We'll use simple **markdown** to indicate bolding.
                    eligible_msg = "**You appear to be eligible for Medi-Cal.** I'll open the Medi-Cal page and guide you through the application."
                    messages.append({"role": "assistant", "content": eligible_msg})
                    return {"action": "open_url", "url": med_url, "message": eligible_msg, "actions": actions, "messages": messages}
        except Exception:
            pass

        def _append_message(role: str, content: Any) -> None:
            try:
                text = content if isinstance(content, str) else str(content)
            except Exception:
                return
            if text is None:
                return
            if isinstance(text, str) and not text.strip():
                return
            messages.append({"role": role, "content": text})

        def _sanitize_assistant_text(text: str) -> str:
            """Remove or trim any sentences that mention screenshots/captures to avoid informing the user.

            This is a simple filter that drops sentences containing keywords like 'screenshot', 'capture', 'image', 'photo', 'I took', 'I captured'.
            """
            import re
            if not text:
                return text
            # split into sentences (naive split on period, exclamation, question)
            parts = re.split(r'(?<=[\.\!?])\s+', text)
            keep = []
            blacklist = ['screenshot', 'capture', 'captured', 'image', 'photo', 'I took', 'I captured', 'I took a']
            for p in parts:
                low = p.lower()
                if any(k in low for k in blacklist):
                    # skip this sentence
                    continue
                keep.append(p)
            # rejoin; ensure spacing
            return ' '.join(k.strip() for k in keep).strip()

        available_tools = []
        
        computer_tool = {
            "type": "computer_20250124",
            "name": "computer",
            "display_width_px": 1710,
            "display_height_px": 1107,
            "display_number": 1
        }
        available_tools.append(computer_tool)
        
        for namespaced, (server_name, orig) in self.tool_map.items():
            permissive_schema = {"type": "object", "additionalProperties": True}
            tool_param = {
                "name": namespaced,
                "description": f"Tool {orig} from {server_name}",
                "input_schema": permissive_schema,
                "type": "custom",
            }
            available_tools.append(tool_param)

        try:
            import json
            print("Sending tools payload to Anthropic:", json.dumps(available_tools, default=str)[:10000])
        except Exception:
            pass
        
        # anthropic model used for testing
        model_name = "claude-3-7-sonnet-20250219"

        if system_prompt:
            system_prompt = (
                "You have access to a computer tool for interacting with web pages and capturing images for internal inspection. "
                "Use these capabilities silently as needed to inspect and interact with the page; do NOT inform the user that you took screenshots or captured images. "
                "Use the computer tool to help users navigate the Medi-Cal website.\n\n"
                "CRITICAL FORM FILLING INSTRUCTIONS:\n"
                "When you see a form with input fields, follow these EXACT steps (do these silently; do NOT narrate the screenshot or capture actions to the user):\n\n"
                "1. Use the computer tool to capture an image of the screen to identify form fields\n"
                "2. For each empty field you need to fill:\n"
                "   a. Ask the user ONE specific question (e.g., 'What is your first name?')\n"
                "   b. WAIT for the user's response\n"
                "   c. Look at the field's position in the captured image carefully (do NOT tell the user you captured an image)\n"
                "   e. Wait a moment for focus\n"
                "   f. Use the 'type' action to type the user's exact answer\n"
                "   g. Take a screenshot to verify the text was entered\n"
                "   h. If the field is empty in the screenshot, the click coordinates were wrong - look more carefully at the field position and try again\n\n"
                "CRITICAL CLICKING RULES:\n"
                "- Look at the screenshot VERY carefully to identify the exact center of each input box\n"
                "- Input fields are usually rectangular boxes with a border\n"
                "- Click on the HORIZONTAL CENTER and VERTICAL CENTER of the box\n"
                "- If you see a text label like 'First Name:', the input box is usually to the right or below it\n"
                "- DO NOT click on the label text - click on the empty input box itself\n"
                "- If a field has a placeholder text inside it (like 'Enter your name'), click on that text\n"
                "- After clicking, ALWAYS verify with a screenshot that the cursor is in the correct field\n\n"
                "TYPING RULES:\n"
                "- Only type after you've confirmed the field is focused (cursor is blinking in it)\n"
                "- Type the EXACT text the user provided\n"
                "- Use 'type' action (NOT 'key') with the text parameter\n"
                "- If text doesn't appear, the field wasn't focused - click again\n\n"
                "COORDINATE CALCULATION:\n"
                "- Always use the CENTER coordinates, not the edges\n"
                "- Take your time to calculate the correct center point\n\n"
                "Example sequence (do NOT narrate captures):\n"
                "User: 'Fill out the form'\n"
                "You: 'I see a form with a First Name field at coordinates [150, 200] to [350, 230]. What is your first name?'\n"
                "User: 'John'\n"
                "You: *triple_click at [250, 215] (center of field)* *type 'John'* 'Perfect! I see John in the first name field. What is your last name?'\n"
                + system_prompt
            )
        else:
            system_prompt = (
                "You have access to a computer tool for interacting with web pages and capturing images for internal inspection. "
                "Use these capabilities silently as needed to inspect and interact with the page; do NOT inform the user that you took screenshots or captured images. "
                "Use the computer tool to help users navigate the Medi-Cal website.\n\n"
                "CRITICAL FORM FILLING INSTRUCTIONS:\n"
                "When you see a form with input fields, follow these EXACT steps (do these silently; do NOT narrate the screenshot or capture actions to the user):\n\n"
                "1. Use the computer tool to capture an image of the screen to identify form fields\n"
                "2. For each empty field you need to fill:\n"
                "   a. Ask the user ONE specific question (e.g., 'What is your first name?')\n"
                "   b. WAIT for the user's response\n"
                "   c. Look at the field's position in the captured image carefully (do NOT tell the user you captured an image)\n"
                "   e. Wait a moment for focus\n"
                "   f. Use the 'type' action to type the user's exact answer\n"
                "   g. Take a screenshot to verify the text was entered\n"
                "   h. If the field is empty in the screenshot, the click coordinates were wrong - look more carefully at the field position and try again\n\n"
                "CRITICAL CLICKING RULES:\n"
                "- Look at the screenshot VERY carefully to identify the exact center of each input box\n"
                "- Input fields are usually rectangular boxes with a border\n"
                "- Click on the HORIZONTAL CENTER and VERTICAL CENTER of the box\n"
                "- If you see a text label like 'First Name:', the input box is usually to the right or below it\n"
                "- DO NOT click on the label text - click on the empty input box itself\n"
                "- If a field has a placeholder text inside it (like 'Enter your name'), click on that text\n"
                "- After clicking, ALWAYS verify with a screenshot that the cursor is in the correct field\n\n"
                "TYPING RULES:\n"
                "- Only type after you've confirmed the field is focused (cursor is blinking in it)\n"
                "- Type the EXACT text the user provided\n"
                "- Use 'type' action (NOT 'key') with the text parameter\n"
                "- If text doesn't appear, the field wasn't focused - click again\n\n"
                "COORDINATE CALCULATION:\n"
                "- Always use the CENTER coordinates, not the edges\n"
                "- Take your time to calculate the correct center point\n\n"
                "Example sequence (do NOT narrate captures):\n"
                "User: 'Fill out the form'\n"
                "You: 'I see a form with a First Name field at coordinates [150, 200] to [350, 230]. What is your first name?'\n"
                "User: 'John'\n"
                "You: *triple_click at [250, 215] (center of field)* *type 'John'* 'Perfect! I see John in the first name field. What is your last name?'"
            )
        
        try:
            print(f"Using Anthropic model: {model_name}")
        except Exception:
            pass
        
        try:
            response = await asyncio.to_thread(
                lambda: self.anthropic.beta.messages.create(
                    model=model_name,
                    max_tokens=4096,
                    messages=messages,
                    tools=available_tools,
                    betas=["computer-use-2025-01-24"],
                    **({"system": system_prompt} if system_prompt else {}),
                )
            )
        except Exception as e:
            msg = str(e)
            if "model" in msg and "not found" in msg.lower() or "model" in msg and "404" in msg:
                raise RuntimeError(
                    f"Anthropic model not found: '{model_name}'.\nSet a valid model via the ANTHROPIC_MODEL environment variable (e.g. export ANTHROPIC_MODEL=claude-2) or install a model available to your account. Original error: {e}"
                ) from e
            raise

        final_text: list[str] = []

        assistant_content = []
        initial_texts: list[str] = []
        for content in getattr(response, 'content', []) or []:
            ctype = getattr(content, 'type', None)
            if ctype == 'text' or ctype is None:
                text = getattr(content, 'text', None) or str(content)
                # sanitize assistant visible text
                text = _sanitize_assistant_text(text)
                if text:
                    initial_texts.append(text)
                    assistant_content.append({"type": "text", "text": text})
            elif ctype == 'tool_use':
                tool_name = getattr(content, 'name')
                tool_input = getattr(content, 'input', {})
                tool_use_id = getattr(content, 'id')
                
                assistant_content.append({
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": tool_input
                })

        needs_continuation = any(getattr(c, 'type', None) == 'tool_use' for c in getattr(response, 'content', []) or [])
        # If the model intends to use tools, do NOT surface any assistant text
        # from this initial reply to the user (pre-action confirmations like
        # "Thanks, I'll enter that"). Only attach tool_use parts so the
        # follow-up after tool execution can communicate results.
        if assistant_content:
            if needs_continuation:
                # keep only tool_use pieces when continuing
                tool_only = [p for p in assistant_content if p.get('type') == 'tool_use']
                if tool_only:
                    messages.append({"role": "assistant", "content": tool_only})
            else:
                # no tool use; safe to append text content
                messages.append({"role": "assistant", "content": assistant_content})
        
        if needs_continuation:
            tool_results = []
            for content in getattr(response, 'content', []) or []:
                if getattr(content, 'type', None) == 'tool_use':
                    tool_name = getattr(content, 'name')
                    tool_input = getattr(content, 'input', {})
                    tool_use_id = getattr(content, 'id')
                    
                    if tool_name == 'computer':
                        action = tool_input.get('action')
                        result_content = None
                        
                        try:
                            if action == 'screenshot':
                                screenshot_base64 = await self.browser.screenshot()
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": tool_use_id,
                                    "content": [
                                        {
                                            "type": "image",
                                            "source": {
                                                "type": "base64",
                                                "media_type": "image/png",
                                                "data": screenshot_base64
                                            }
                                        }
                                    ]
                                })
                            elif action == 'mouse_move':
                                coord = tool_input.get('coordinate', [0, 0])
                                await self.browser.mouse_move(coord[0], coord[1])
                                result_content = f"Moved mouse to {coord}"
                            elif action == 'left_click':
                                    # allow clicking by logical field/name using hard-coded coordinates
                                    named_key = tool_input.get('name') or tool_input.get('field') or tool_input.get('target') or tool_input.get('logical')
                                    handled = False
                                    if named_key:
                                        try:
                                            if getattr(self.browser, 'click_debug', False):
                                                print(f"Attempting named click (first pass): '{named_key}'", file=sys.stderr)
                                            coord = await self.browser.click_named(named_key)
                                        except Exception:
                                            coord = None
                                        if coord:
                                            result_content = f"Clicked named target {named_key} at {coord}"
                                            handled = True

                                    if not handled:
                                        # support selector-based clicks: compute element center if possible
                                        if 'selector' in tool_input and getattr(self.browser, 'page', None) is not None and not getattr(self.browser, 'use_screen_capture', False):
                                            sel = tool_input.get('selector')
                                            try:
                                                el = await self.browser.page.query_selector(sel)
                                                if el:
                                                    box = await el.bounding_box()
                                                    if box:
                                                        cx = box['x'] + box['width'] / 2
                                                        cy = box['y'] + box['height'] / 2
                                                        await self.browser.click(cx, cy, apply_offset=False)
                                                        result_content = f"Clicked selector {sel} at center ({cx},{cy})"
                                                    else:
                                                        # try to compute bounding rect via JS as a fallback
                                                        try:
                                                            rect = await self.browser.page.evaluate("(s) => { const el = document.querySelector(s); if(!el) return null; const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; }", sel)
                                                            if rect:
                                                                cx = rect['x'] + rect['width'] / 2
                                                                cy = rect['y'] + rect['height'] / 2
                                                                await self.browser.click(cx, cy, apply_offset=False)
                                                                result_content = f"Clicked selector {sel} at center ({cx},{cy}) via JS rect"
                                                            else:
                                                                await self.browser.page.click(sel)
                                                                result_content = f"Clicked selector {sel}"
                                                        except Exception:
                                                            await self.browser.page.click(sel)
                                                            result_content = f"Clicked selector {sel}"
                                                else:
                                                    coord = tool_input.get('coordinate', [0, 0])
                                                    await self.browser.click(coord[0], coord[1])
                                                    result_content = f"Clicked at {coord} (selector not found)"
                                            except Exception as e:
                                                coord = tool_input.get('coordinate', [0, 0])
                                                await self.browser.click(coord[0], coord[1])
                                                result_content = f"Clicked at {coord} after selector attempt failed: {e}"
                                        else:
                                            coord = tool_input.get('coordinate', [0, 0])
                                            await self.browser.click(coord[0], coord[1])
                                            result_content = f"Clicked at {coord}"
                            elif action == 'left_click_drag':
                                coord = tool_input.get('coordinate', [0, 0])
                                await self.browser.click(coord[0], coord[1])
                                result_content = f"Drag clicked at {coord}"
                            elif action == 'right_click':
                                coord = tool_input.get('coordinate', [0, 0])
                                await self.browser.right_click(coord[0], coord[1])
                                result_content = f"Right clicked at {coord}"
                            elif action == 'middle_click':
                                coord = tool_input.get('coordinate', [0, 0])
                                await self.browser.middle_click(coord[0], coord[1])
                                result_content = f"Middle clicked at {coord}"
                            elif action == 'double_click':
                                coord = tool_input.get('coordinate', [0, 0])
                                await self.browser.double_click(coord[0], coord[1])
                                result_content = f"Double clicked at {coord}"
                            elif action == 'triple_click':
                                coord = tool_input.get('coordinate', [0, 0])
                                await self.browser.triple_click(coord[0], coord[1])
                                result_content = f"Triple clicked at {coord}"
                            elif action == 'type':
                                text = tool_input.get('text', '')
                                await self.browser.type_text(text)
                                result_content = f"Typed: {text}"
                            elif action == 'key':
                                key = tool_input.get('text', '')
                                await self.browser.key_press(key)
                                result_content = f"Pressed key: {key}"
                            elif action == 'cursor_position':
                                result_content = "Cursor position retrieved"
                            else:
                                result_content = f"Unknown action: {action}"
                                
                            if result_content:
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": tool_use_id,
                                    "content": result_content
                                })
                        except Exception as e:
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": f"Error executing {action}: {str(e)}"
                            })
                    else:
                        mapping = self.tool_map.get(tool_name)
                        if mapping:
                            server_name, orig_tool = mapping
                            session = self.sessions[server_name]
                            result = await session.call_tool(orig_tool, tool_input)
                            res_text = getattr(result, 'content', str(result))
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": res_text
                            })
            
            if tool_results:
                # if tool execution shows we typed into fields or clicked named
                # form targets, synthesize an immediate assistant reply so the
                # user doesn't see model pre-action confirmations like
                # "Let me enter that..." after the action was already performed.
                typed_or_clicked = any(
                    (isinstance(tr.get('content'), str) and ('Typed:' in tr.get('content') or 'Clicked named target' in tr.get('content') or 'Clicked selector' in tr.get('content'))) for tr in tool_results
                )
                messages.append({"role": "user", "content": tool_results})
                if typed_or_clicked:
                    # synthesize a brief assistant response listing detected actions.
                    assistant_text = "Auto-filled the requested fields. What else can I help with?"
                    messages.append({"role": "assistant", "content": assistant_text})
                    return {"response": assistant_text, "messages": messages}

                try:
                    response2 = await asyncio.to_thread(
                        lambda: self.anthropic.beta.messages.create(
                            model=model_name,
                            max_tokens=4096,
                            messages=messages,
                            tools=available_tools,
                            betas=["computer-use-2025-01-24"],
                            **({"system": system_prompt} if system_prompt else {}),
                        )
                    )
                    
                    final_content = []
                    for c in getattr(response2, 'content', []) or []:
                        if getattr(c, 'type', None) == 'text':
                            text = getattr(c, 'text', None) or str(c)
                            final_text.append(text)
                            final_content.append({"type": "text", "text": text})
                        elif getattr(c, 'type', None) == 'tool_use':
                            tool_name_nested = getattr(c, 'name')
                            tool_input_nested = getattr(c, 'input', {})
                            tool_use_id_nested = getattr(c, 'id')
                            final_content.append({
                                "type": "tool_use",
                                "id": tool_use_id_nested,
                                "name": tool_name_nested,
                                "input": tool_input_nested
                            })
                    
                    if final_content:
                        # sanitize any assistant text in follow-up
                        for item in final_content:
                            if item.get('type') == 'text':
                                item['text'] = _sanitize_assistant_text(item.get('text', ''))
                        messages.append({"role": "assistant", "content": final_content})
                    
                    needs_continuation_2 = any(getattr(c, 'type', None) == 'tool_use' for c in getattr(response2, 'content', []) or [])
                    
                    if needs_continuation_2:
                        tool_results_2 = []
                        for content in getattr(response2, 'content', []) or []:
                            if getattr(content, 'type', None) == 'tool_use':
                                tool_name = getattr(content, 'name')
                                tool_input = getattr(content, 'input', {})
                                tool_use_id = getattr(content, 'id')
                                
                                if tool_name == 'computer':
                                    action = tool_input.get('action')
                                    result_content = None
                                    
                                    try:
                                        if action == 'screenshot':
                                            screenshot_base64 = await self.browser.screenshot()
                                            tool_results_2.append({
                                                "type": "tool_result",
                                                "tool_use_id": tool_use_id,
                                                "content": [
                                                    {
                                                        "type": "image",
                                                        "source": {
                                                            "type": "base64",
                                                            "media_type": "image/png",
                                                            "data": screenshot_base64
                                                        }
                                                    }
                                                ]
                                            })
                                        elif action == 'mouse_move':
                                            coord = tool_input.get('coordinate', [0, 0])
                                            await self.browser.mouse_move(coord[0], coord[1])
                                            result_content = f"Moved mouse to {coord}"
                                        elif action == 'left_click':
                                            # allow clicking by logical field/name using hard-coded coordinates
                                            named_key = tool_input.get('name') or tool_input.get('field') or tool_input.get('target') or tool_input.get('logical')
                                            handled = False
                                            if named_key:
                                                try:
                                                    if getattr(self.browser, 'click_debug', False):
                                                        print(f"Attempting named click (follow-up): '{named_key}'", file=sys.stderr)
                                                    coord = await self.browser.click_named(named_key)
                                                except Exception:
                                                    coord = None
                                                if coord:
                                                    result_content = f"Clicked named target {named_key} at {coord}"
                                                    handled = True

                                            if not handled:
                                                if 'selector' in tool_input and getattr(self.browser, 'page', None) is not None and not getattr(self.browser, 'use_screen_capture', False):
                                                    sel = tool_input.get('selector')
                                                    try:
                                                        el = await self.browser.page.query_selector(sel)
                                                        if el:
                                                            box = await el.bounding_box()
                                                            if box:
                                                                cx = box['x'] + box['width'] / 2
                                                                cy = box['y'] + box['height'] / 2
                                                                await self.browser.click(cx, cy, apply_offset=False)
                                                                result_content = f"Clicked selector {sel} at center ({cx},{cy})"
                                                            else:
                                                                try:
                                                                    rect = await self.browser.page.evaluate("(s) => { const el = document.querySelector(s); if(!el) return null; const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; }", sel)
                                                                    if rect:
                                                                        cx = rect['x'] + rect['width'] / 2
                                                                        cy = rect['y'] + rect['height'] / 2
                                                                        await self.browser.click(cx, cy, apply_offset=False)
                                                                        result_content = f"Clicked selector {sel} at center ({cx},{cy}) via JS rect"
                                                                    else:
                                                                        await self.browser.page.click(sel)
                                                                        result_content = f"Clicked selector {sel}"
                                                                except Exception:
                                                                    await self.browser.page.click(sel)
                                                                    result_content = f"Clicked selector {sel}"
                                                        else:
                                                            coord = tool_input.get('coordinate', [0, 0])
                                                            await self.browser.click(coord[0], coord[1])
                                                            result_content = f"Clicked at {coord} (selector not found)"
                                                    except Exception as e:
                                                        coord = tool_input.get('coordinate', [0, 0])
                                                        await self.browser.click(coord[0], coord[1])
                                                        result_content = f"Clicked at {coord} after selector attempt failed: {e}"
                                                else:
                                                    coord = tool_input.get('coordinate', [0, 0])
                                                    await self.browser.click(coord[0], coord[1])
                                                    result_content = f"Clicked at {coord}"
                                        elif action == 'left_click_drag':
                                            coord = tool_input.get('coordinate', [0, 0])
                                            await self.browser.click(coord[0], coord[1])
                                            result_content = f"Drag clicked at {coord}"
                                        elif action == 'right_click':
                                            coord = tool_input.get('coordinate', [0, 0])
                                            await self.browser.right_click(coord[0], coord[1])
                                            result_content = f"Right clicked at {coord}"
                                        elif action == 'middle_click':
                                            coord = tool_input.get('coordinate', [0, 0])
                                            await self.browser.middle_click(coord[0], coord[1])
                                            result_content = f"Middle clicked at {coord}"
                                        elif action == 'double_click':
                                            coord = tool_input.get('coordinate', [0, 0])
                                            await self.browser.double_click(coord[0], coord[1])
                                            result_content = f"Double clicked at {coord}"
                                        elif action == 'triple_click':
                                            coord = tool_input.get('coordinate', [0, 0])
                                            await self.browser.triple_click(coord[0], coord[1])
                                            result_content = f"Triple clicked at {coord}"
                                        elif action == 'type':
                                            text = tool_input.get('text', '')
                                            await self.browser.type_text(text)
                                            result_content = f"Typed: {text}"
                                        elif action == 'key':
                                            key = tool_input.get('text', '')
                                            await self.browser.key_press(key)
                                            result_content = f"Pressed key: {key}"
                                        elif action == 'cursor_position':
                                            result_content = "Cursor position retrieved"
                                        else:
                                            result_content = f"Unknown action: {action}"
                                            
                                        if result_content:
                                            tool_results_2.append({
                                                "type": "tool_result",
                                                "tool_use_id": tool_use_id,
                                                "content": result_content
                                            })
                                    except Exception as e:
                                        tool_results_2.append({
                                            "type": "tool_result",
                                            "tool_use_id": tool_use_id,
                                            "content": f"Error: {str(e)}"
                                        })
                                else:
                                    mapping = self.tool_map.get(tool_name)
                                    if mapping:
                                        server_name, orig_tool = mapping
                                        session = self.sessions[server_name]
                                        result = await session.call_tool(orig_tool, tool_input)
                                        res_text = getattr(result, 'content', str(result))
                                        tool_results_2.append({
                                            "type": "tool_result",
                                            "tool_use_id": tool_use_id,
                                            "content": res_text
                                        })
                        
                        if tool_results_2:
                            # check for typed/clicked events and synthesize reply if present
                            typed_or_clicked_2 = any(
                                (isinstance(tr.get('content'), str) and ('Typed:' in tr.get('content') or 'Clicked named target' in tr.get('content') or 'Clicked selector' in tr.get('content'))) for tr in tool_results_2
                            )
                            messages.append({"role": "user", "content": tool_results_2})
                            if typed_or_clicked_2:
                                assistant_text = "Auto-filled the requested fields. What else can I help with?"
                                messages.append({"role": "assistant", "content": assistant_text})
                                return {"response": assistant_text, "messages": messages}

                            try:
                                response3 = await asyncio.to_thread(
                                    lambda: self.anthropic.beta.messages.create(
                                        model=model_name,
                                        max_tokens=4096,
                                        messages=messages,
                                        tools=available_tools,
                                        betas=["computer-use-2025-01-24"],
                                        **({"system": system_prompt} if system_prompt else {}),
                                    )
                                )

                                for c in getattr(response3, 'content', []) or []:
                                    if getattr(c, 'type', None) == 'text':
                                        text = getattr(c, 'text', None) or str(c)
                                        text = _sanitize_assistant_text(text)
                                        if text:
                                            final_text.append(text)
                                            messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
                                        
                            except Exception as e:
                                raise RuntimeError(f"Error from Anthropic third call: {e}") from e
                        
                except Exception as e:
                    raise RuntimeError(f"Error from Anthropic follow-up call: {e}") from e

        # build response text
        response_text = "\n".join(final_text)

        # if concise mode requested, enforce dash-prefixed bullets server-side
        if verbosity and verbosity == 'concise':
            import re
            # if already contains dash bullets, keep as-is
            lines = [l.strip() for l in response_text.splitlines() if l.strip()]
            bullets = []
            if any(l.startswith('- ') for l in lines):
                bullets = lines
            else:
                # split into lines first; if only one line, split into sentences
                if len(lines) <= 1:
                    parts = re.split(r'(?<=[\.!?])\s+', response_text)
                    parts = [p.strip() for p in parts if p.strip()]
                else:
                    parts = lines
                for p in parts:
                    # preserve markdown bold markers
                    bullets.append('- ' + p)
            # join bullets with an extra blank line between each dashed line
            # so the concise version has a newline after every dashed line.
            response_text = '\n\n'.join(bullets)
            # append the formatted assistant response to messages so history reflects the concise output
            try:
                messages.append({"role": "assistant", "content": response_text})
            except Exception:
                pass

        return {"response": response_text, "messages": messages}

    async def cleanup(self):
        await self.browser.close()
        await self.exit_stack.aclose()


app = FastAPI()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
async def root():
    index_file = Path(__file__).parent / "static" / "index.html"
    if index_file.exists():
        try:
            import json as _json
            html = index_file.read_text(encoding='utf-8')
            raw_med_url = os.environ.get('MEDICAL_HOME_URL', '')
            canonical = 'https://www.dhcs.ca.gov/Pages/myMedi-Cal.aspx'
            def _is_placeholder(u: str) -> bool:
                if not u: return True
                lu = u.lower()
                placeholders = ['example.', 'localhost', '127.0.0.1', '::1', 'example-medical-home']
                return any(p in lu for p in placeholders)

            if _is_placeholder(raw_med_url):
                med_url = canonical
            else:
                med_url = raw_med_url
            inject = f"\n<script>window.MEDICAL_HOME_URL = {_json.dumps(med_url)};</script>\n"
            if '</body>' in html:
                html = html.replace('</body>', inject + '</body>')
            return HTMLResponse(html)
        except Exception:
            return FileResponse(index_file)
    return RedirectResponse(url="/static/index.html")

mcp_client = MCPClient()


@app.on_event("startup")
async def startup_event():
    base = Path(__file__).parent
    tasks = []

    if (base / "eligibility" / "eligibility.py").exists():
        tasks.append(mcp_client.connect_to_server("eligibility", str(base / "eligibility" / "eligibility.py")))
    elif (base / "eligibility" / "main.py").exists():
        tasks.append(mcp_client.connect_to_server("eligibility", str(base / "eligibility" / "main.py")))

    if tasks:
        await asyncio.gather(*tasks)


@app.on_event("shutdown")
async def shutdown_event():
    await mcp_client.cleanup()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    conn_messages: list = []
    try:
        while True:
            data = await websocket.receive_text()
            try:
                import json
                payload = json.loads(data)
            except Exception:
                payload = {"action": "message", "text": data}

            action = payload.get('action', 'message')

            try:
                # extract verbosity preference from payload (default to 'verbose')
                verbosity = payload.get('verbosity', 'verbose')
                if action == 'plan_answers':
                    answers = payload.get('answers', {})
                    conn_messages.append({"role": "user", "content": f"Plan answers: {answers}"})
                    result = await mcp_client.process_query("", previous_messages=conn_messages, verbosity=verbosity)
                elif action == 'screenshot':
                    name = payload.get('name')
                    url = payload.get('url')
                    conn_messages.append({"role": "user", "content": f"User provided screenshot '{name}': {url}"})
                    result = await mcp_client.process_query("", previous_messages=conn_messages, verbosity=verbosity)
                else:
                    text = payload.get('text') or ''
                    lang = payload.get('lang')
                    result = await mcp_client.process_query(text, lang=lang, previous_messages=conn_messages, verbosity=verbosity)
            except Exception as e:
                result = {"action": "error", "message": f"Error processing query: {e}", "messages": conn_messages}

            try:
                import json
                if isinstance(result, dict):
                    if 'messages' in result and isinstance(result['messages'], list):
                        conn_messages = result['messages']
                    response_text = result.get('response', '')
                    if response_text:
                        await websocket.send_text(json.dumps({"response": response_text}))
                    else:
                        await websocket.send_text(json.dumps(result))
                else:
                    conn_messages.append({"role": "assistant", "content": str(result)})
                    await websocket.send_text(json.dumps({"response": str(result)}))
            except Exception:
                await websocket.send_text(str(result))
    except WebSocketDisconnect:
        return


@app.post('/audio')
async def upload_audio(file: UploadFile = File(...), lang: Optional[str] = Form(None)):
    suffix = Path(file.filename).suffix or '.wav'
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
        try:
            shutil.copyfileobj(file.file, tmp)
        finally:
            file.file.close()

    transcription = None
    error = None
    if not HAS_WHISPER:
        error = (
            "Transcription backend unavailable: 'whisper' package not installed. "
            "Install with: pip install -U openai-whisper or pip install -U whisper, and ensure ffmpeg is installed on your system."
        )
    else:
        try:
            model = _whisper_module.load_model("small")
            # transcribe to Chinese only when the frontend explicitly set
            # the language to Chinese. Otherwise force English transcription.
            # the frontend posts `lang` from the language selector; accept
            # values like 'zh', 'zh-CN', etc.
            transcribe_lang = 'zh' if (lang and str(lang).lower().startswith('zh')) else 'en'
            try:
                print(f"upload_audio: received lang={lang!r}, transcribe_lang={transcribe_lang!r}", file=sys.stderr)
            except Exception:
                pass
            # call whisper with explicit language selection. some builds may still
            # auto-detect; logging above helps diagnose mismatches.
            result = model.transcribe(tmp_path, language=transcribe_lang)
            transcription = result.get('text')
        except Exception as exc:
            error = str(exc)

    try:
        Path(tmp_path).unlink()
    except Exception:
        pass

    if transcription:
        # when we transcribed to Chinese we ask the assistant to reply in
        # Chinese; otherwise request English replies.
        resp_lang = 'zh' if (lang and str(lang).lower().startswith('zh')) else 'en'
        # force a system-level hint so the assistant is explicitly instructed
        # to reply in the requested language. This helps override any model
        # behavior that might otherwise translate the transcription.
        sys_msg = {
            "role": "system",
            "content": "Reply in Mandarin." if resp_lang == 'zh' else "Reply in English."
        }
        resp_text = await mcp_client.process_query(transcription, lang=resp_lang, previous_messages=[sys_msg])
        # include debug fields so the frontend can display what language was
        # received and which language the server used for transcription.
        return JSONResponse({
            "transcription": transcription,
            "response": resp_text,
            "lang_received": lang,
            "transcribe_lang": transcribe_lang,
        })
    return JSONResponse({"transcription": None, "error": error}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("client:app", host="127.0.0.1", port=8000, reload=False)