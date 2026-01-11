"""
HoppieBridge
X-Plane plugin

This X-Plane plugin exposes eleven string datarefs used to integrate with Hoppie’s ACARS:
    hoppiebridge/send_queue — clients write a JSON string to send a message.
    hoppiebridge/send_message_to — clients write destination callsign for structured message.
    hoppiebridge/send_message_type — clients write message type for structured message.
    hoppiebridge/send_message_packet — clients write message packet for structured message.
    hoppiebridge/send_callsign — clients write callsign.
    hoppiebridge/poll_queue — read the latest received message as a JSON string.
    hoppiebridge/poll_message_origin — read the origin of the latest message received ("poll" or "response").
    hoppiebridge/poll_message_from — read the source callsign of the latest message received.
    hoppiebridge/poll_message_type — read the type of the latest message received.
    hoppiebridge/poll_message_packet — read the packet content of the latest message received.
    hoppiebridge/callsign — read the current callsign.
    hoppiebridge/poll_queue_clear — write 1 (or any non-zero value) to clear the inbox datarefs when message is received from client.
    hoppiebridge/comm_ready — write 1 (or any non-zero value) to notify unit has all conditions to work:
                                - avionics on
                                - callsign set
                                - poll success.

Messages are dictionaries serialized to JSON with fields:
{
    "logon":  string,   # your Hoppie logon
    "from":   string,   # your callsign
    "to":     string,   # destination callsign or "all"
    "type":   string,   # one of: progress, cpdlc, telex, ping, inforeq,
                        #          posreq, position, datareq, poll, peek
    "packet": string    # message content
}

PythonInterface periodically polls (type="poll") and sends any pending outbound message.
Inbox/outbox datarefs carry JSON strings; if upstream provides single-quoted dicts, we fall back to a safe literal parse.

further information can be found at https://www.hoppie.nl/acars/system/tech.html

Notes:
- inbox/outbox datarefs carry JSON-like strings due to X-Plane string limitations
- legacy single-quoted dicts are accepted via literal_eval fallback

Examples:
    {"to": "SERVER", "type": "inforeq", "packet": "METAR LIPE"}
    {'response': 'ok {acars info {...}}'}

Copyright (c) 2026, Antonio Golfari
All rights reserved.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree. 
"""

from __future__ import annotations

import os
import json
import ast
import threading
import requests
import operator
import random
import re

from pathlib import Path
from typing import Optional, Dict, Any
from collections import deque
from datetime import datetime, timezone
from time import perf_counter

try:
    import xp
    from XPPython3.utils.datarefs import find_dataref, create_dataref, DataRef
except ImportError:
    print('xp module not found')
    pass

# Version
__VERSION__ = 'v1.0'

# Plugin parameters required from XPPython3
plugin_name = 'HoppieBridge'
plugin_sig = 'xppython3.hoppiebridge'
plugin_desc = 'Simple Python script to add drefs for Hoppie\'s ACARS'

# Other parameters
DEFAULT_SCHEDULE = 5  # positive numbers are seconds, 0 disabled, negative numbers are cycles
POLL_SCHEDULE = 65  # seconds
URL = 'https://www.hoppie.nl/acars/system/connect.html'

# debug 
DEBUG = False

def log(msg: str) -> None:
    xp.log(msg)

def debug(msg: str, tag: str = "DEBUG") -> None:
    if DEBUG:
        xp.log(f"[{tag}] {msg}")

# widget parameters
MONITOR_WIDTH = 240

HOPPIE_PATTERN = re.compile(
    r'\{([^\s]+)\s+([^\s]+)\s+\{(.+?)\}\}',
    re.DOTALL
)

try:
    FONT = xp.Font_Proportional
    FONT_WIDTH, FONT_HEIGHT, _ = xp.getFontDimensions(FONT)
    PREF_PATH = Path(xp.getPrefsPath()).parent
    debug(f"font width: {FONT_WIDTH} | height: {FONT_HEIGHT}", "INIT")
except NameError:
    FONT_WIDTH, FONT_HEIGHT = 10, 10
    PREF_PATH = Path(os.path.dirname(__file__)).parent

# aliases
Message = dict[str, Any]
ParsedMessage = tuple[Optional[str], Optional[str], Optional[str], Optional[str]]

# safe attrgetter with default value
def safe_attrgetter(path, default=None):
    def getter(obj):
        try:
            return operator.attrgetter(path)(obj)
        except Exception as e:
            log(f"**** {path} Error: {e}")
            return default
    return getter


def random_connection_time(min: int = 45, max: int = 75) -> int:
    """Calculate a random connection time between 45 and 75 seconds."""
    return random.randint(min, max)


def looks_like_json(raw: str) -> bool:
    """
    Cheap heuristic:
    - starts with '{'
    - contains at least one double-quoted key
    """
    if not raw:
        return False

    s = raw.lstrip()
    return s.startswith('{') and ':' in s and '"' in s


def parse_message(raw: str) -> Message:
    """
    Best-effort decoder for inbox/outbox payloads.

    Tries:
    1) strict JSON (only if payload looks like JSON)
    2) Python literal_eval as a safe fallback

    Never raises; returns empty dict on failure.
    """

    if not raw or not raw.strip():
        return {}

    raw = raw.strip()

    # 1) Try JSON only if it actually looks like JSON
    if looks_like_json(raw):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass  # fall through

    # 2) Try Python literal (safe)
    try:
        value = ast.literal_eval(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, SyntaxError):
        log(f"**** Cannot parse message: {raw!r}")
        return {}


def format_message(msg: dict | str) -> str:
    """Convert Python dict or string into a string suitable for ACARS/X-Plane."""
    if isinstance(msg, dict):
        try:
            return json.dumps(msg)   # valid JSON
        except (TypeError, ValueError):
            return str(msg)          # last resort
    return str(msg)


def parse_hoppie_message(data: dict) -> ParsedMessage:
    """
    Parse a Hoppie ACARS response payload.

    Returns a 4-tuple:
        origin   -> 'poll' | 'response' | None
        source   -> sender callsign or None
        msg_type -> message type or None
        packet   -> payload or None
    """

    # Detect origin first (politics matter)
    if 'poll' in data:
        origin = 'poll'
        raw = data.get('poll')
    elif 'response' in data:
        origin = 'response'
        raw = data.get('response')
    else:
        return None, None, None, None

    if not raw:
        return origin, None, None, None

    raw = raw.strip()

    # Case 1: plain "ok"
    if raw == "ok":
        return origin, None, None, "ok"

    # Case 2: ok {SOURCE TYPE {PACKET}}
    match = HOPPIE_PATTERN.search(raw)
    if match:
        source = match.group(1)
        msg_type = match.group(2)
        packet = match.group(3).strip()
        return origin, source, msg_type, packet

    # Case 3: unexpected but non-fatal content
    return origin, None, None, raw


class Dref:
    """Adapter around XPPython3 DataRefs used by HoppieBridge."""

    def __init__(self) -> None:
        # created datarefs
        self._send_queue = create_dataref('hoppiebridge/send_queue', 'string')  # legacy raw queue
        self._send_message_to = create_dataref('hoppiebridge/send_message_to', 'string')
        self._send_message_type = create_dataref('hoppiebridge/send_message_type', 'string')
        self._send_message_packet = create_dataref('hoppiebridge/send_message_packet', 'string')
        self._send_callsign = create_dataref('hoppiebridge/send_callsign', 'string')
        self._poll_queue = create_dataref('hoppiebridge/poll_queue', 'string')  # legacy raw queue
        self._poll_message_origin = create_dataref('hoppiebridge/poll_message_origin', 'string')
        self._poll_message_from = create_dataref('hoppiebridge/poll_message_from', 'string')
        self._poll_message_type = create_dataref('hoppiebridge/poll_message_type', 'string')
        self._poll_message_packet = create_dataref('hoppiebridge/poll_message_packet', 'string')
        self._poll_queue_clear = create_dataref('hoppiebridge/poll_queue_clear', 'number')
        self._callsign = create_dataref('hoppiebridge/callsign', 'string')
        self._comm_ready = create_dataref('hoppiebridge/comm_ready', 'number')
        # standard datarefs
        self._avionics = find_dataref('sim/cockpit/electrical/avionics_on')

    @property
    def avionics_powered(self) -> bool:
        """True if avionics are powered on."""
        return self._avionics.value == 1

    @property
    def callsign(self) -> str:
        """Get the callsign"""
        debug(f'  ** _callsign: {self._callsign.value} | type: {type(self._callsign.value)} | len: {len(self._callsign.value)}', "DREF")
        return str(self._callsign.value or "").strip()

    @callsign.setter
    def callsign(self, value: str) -> None:
        """Set the callsign"""
        debug(f'  ** set _callsign: {value} | type: {type(value)}', "DREF")
        self._callsign.value = value

    @property
    def send_callsign(self) -> str:
        """Return send callsign request status"""
        debug(f'  ** _send_callsign: {self._send_callsign.value} | type: {type(self._send_callsign.value)}', "DREF")
        return str(self._send_callsign.value or "").strip()

    @send_callsign.setter
    def send_callsign(self, value: str) -> None:
        """Set send callsign request status"""
        self._send_callsign.value = value

    @property
    def inbox(self) -> dict:
        """Return decoded inbox messages"""
        debug(f'  ** _poll_queue: {self._poll_queue.value} | type: {type(self._poll_queue.value)} | dim: {self._poll_queue._dim} | len: {len(self._poll_queue.value)}', "DREF")
        return parse_message(self._poll_queue.value)

    @inbox.setter
    def inbox(self, message: dict | str) -> None:
        """Set inbox with a message (encoded before storing)"""
        debug(f'  ** add_to_inbox: {message} | type: {type(message)}', "DREF")
        formatted = format_message(message)
        self._poll_queue.value = formatted
        # parse message and set subfields
        if message == "" or not formatted.strip():
            origin, source, msg_type, packet = "", "", "", ""
        else:
            try:
                data = parse_message(formatted)
            except ValueError:
                data = {}
            origin, source, msg_type, packet = parse_hoppie_message(data)
        self._poll_message_origin.value = origin or ""
        self._poll_message_from.value = source or ""
        self._poll_message_type.value = msg_type or ""
        self._poll_message_packet.value = packet or ""

    @property
    def outbox(self) -> dict:
        """Return decoded outbox message, if any"""

        to_ = self._send_message_to.value.strip()
        type_ = self._send_message_type.value.strip()
        packet = self._send_message_packet.value.strip()


        # 1. Structured message has priority
        if to_ and type_ and packet:
            return {
                "to": to_,
                "type": type_,
                "packet": packet,
            }
        elif to_ or type_ or packet:
            # incomplete structured message
            debug("ACARS outbox: incomplete structured message, ignoring", "DREF")

        # 2. Legacy raw queue
        raw = self._send_queue.value.strip()
        if raw:
            return parse_message(raw)

        # 3. Nothing to send
        return {}

    @outbox.setter
    def outbox(self, value: dict | None) -> None:
        """Clear outbox after send completion (legacy and structured) - no value needed"""

        # Clear structured fields
        self._send_message_to.value = ""
        self._send_message_type.value = ""
        self._send_message_packet.value = ""

        # Clear legacy queue
        self._send_queue.value = ""

    @property
    def clear_inbox(self) -> bool:
        """Return clear inbox request status"""
        debug(f'  ** _poll_queue_clear: {self._poll_queue_clear.value} | type: {type(self._poll_queue_clear.value)}', "DREF")
        return bool(self._poll_queue_clear.value)

    @clear_inbox.setter
    def clear_inbox(self, value: bool | int) -> None:
        """Set clear inbox request status"""
        self._poll_queue_clear.value = int(bool(value))

    @property
    def comm_ready(self) -> bool:
        """Return communication ready status"""
        return bool(self._comm_ready.value)

    @comm_ready.setter
    def comm_ready(self, value: bool | int) -> None:
        """Set communication ready status"""
        self._comm_ready.value = int(bool(value))


class Async(threading.Thread):
    """Async thread to handle connection to Hoppie's ACARS"""

    def __init__(self, task, *args, **kwargs) -> None:
        super().__init__(daemon=True)
        self.task = task
        self.args = args
        self.kwargs = kwargs
        self.cancel = threading.Event()
        self.elapsed = 0.0
        self.result = None

    @property
    def pending(self) -> bool:
        return self.is_alive()

    def run(self) -> None:
        start = perf_counter()
        try:
            self.result = self.task(*self.args, **self.kwargs)
        except Exception as e:
            self.result = e
        finally:
            self.elapsed = perf_counter() - start
            debug(f"Async task {self.task.__name__} completed in {self.elapsed:.3f} seconds", "ASYNC")

    def stop(self) -> None:
        """Stop the async task (not really, best effort only)"""
        self.cancel.set()
        if self.is_alive():
            self.join(3)


class Bridge:
    """Connection to Hoppie's ACARS"""
    url = URL
    _session: requests.Session | None = None

    @classmethod
    def session(cls) -> requests.Session:
        if cls._session is None:
            s = requests.Session()
            s.headers.update({'User-Agent': f'HoppieBridge/{__VERSION__}'})
            cls._session = s
        return cls._session

    def __init__(self, message: dict, poll_payload: dict) -> None:
        self.message = message
        self.poll_payload = poll_payload

    @staticmethod
    def run(message: Optional[dict] = None, poll_payload: Optional[dict] = None) -> dict:
        bridge = Bridge(message or {}, poll_payload or {})
        response = {}
        try:
            if message:
                return bridge.query(message)
            if poll_payload:
                return bridge.poll()
        except requests.Timeout:
            response = {'error': "Connection Timeout"}
        except requests.RequestException as e:
            response = {'error': f"Connection Error: {str(e)}"}
        return response

    def query(self, message: dict) -> dict:
        if not isinstance(message, dict):
            return {'error': 'Message must be a dictionary'}
        try:
            response = self.session().post(self.url, data=message, timeout=(15, 15))
            if response.status_code != 200:
                return {'error': f"Failed to send message: {response.status_code} {response.reason}"}
            if 'ok' not in response.text.lower():
                return {'error': f"Message Error: {response.text}"}
            return {'response': response.text}
        except requests.Timeout:
            return {'error': "Timeout occurred while sending message"}
        except requests.RequestException as e:
            return {'error': f"Request Error: {str(e)}"}

    def poll(self) -> dict:
        try:
            response = self.session().post(self.url, data=self.poll_payload, timeout=(15, 15))
            if response.status_code != 200:
                return {'error': f"Failed to poll data: {response.status_code} {response.reason}"}
            return {'poll': response.text}
        except requests.Timeout:
            return {'error': "Timeout occurred while polling data"}
        except requests.RequestException as e:
            return {'error': f"Request Error: {str(e)}"}


class FloatingWidget:

    LINE = FONT_HEIGHT + 4
    WIDTH = 240
    HEIGHT = 320
    HEIGHT_MIN = 100
    MARGIN = 10
    HEADER = 16

    left, top, right, bottom = 0, 0, 0, 0

    def __init__(self, title: str, x: int, y: int, width: int = WIDTH, height: int = HEIGHT) -> None:

        # main window internal margins
        self.left, self.top, self.right, self.bottom = (
            x + self.MARGIN,
            y - self.HEADER,
            x + width - self.MARGIN,
            y - height + self.MARGIN
        )
        self.pilot_info_subwindow = None
        self.info_line = None
        self.content_widget = {
            'subwindow': None,
            'title': None,
            'lines': []
        }

        # main widget
        self.widget = xp.createWidget(
            x, y, x + width, y - height, 
            1, title, 1, 0, xp.WidgetClass_MainWindow
        )
        xp.setWidgetProperty(self.widget, xp.Property_MainWindowHasCloseBoxes, 1)
        xp.setWidgetProperty(self.widget, xp.Property_MainWindowType, xp.MainWindowStyle_Translucent)

        # window popout button
        self.popout_button = xp.createWidget(
            self.right - FONT_WIDTH, self.top, self.right, self.top - FONT_HEIGHT,
            1, "", 0, self.widget, xp.WidgetClass_Button
        )
        xp.setWidgetProperty(self.popout_button, xp.Property_ButtonType, xp.LittleUpArrow)

        # set underlying window
        self.window = xp.getWidgetUnderlyingWindow(self.widget)
        xp.setWindowTitle(self.window, title)

        self.top -= 26

    @property
    def content_width(self) -> int:
        l, _, r, _ = self.get_subwindow_margins()
        return r - l

    @staticmethod
    def cr() -> int:
        return FloatingWidget.LINE + FloatingWidget.MARGIN

    @staticmethod
    def check_widget_descriptor(widget, text: str) -> None:
        if text not in xp.getWidgetDescriptor(widget):
            xp.setWidgetDescriptor(widget, text)
            xp.showWidget(widget)

    @classmethod
    def create_window(cls, title: str, x: int, y: int, width: int = WIDTH, height: int = HEIGHT) -> FloatingWidget:
        return cls(title, x, y, width, height)

    def get_height(self, lines: Optional[int] = None) -> int:
        if not lines:
            return self.top - self.bottom
        else:
            return self.LINE*lines + 2*self.MARGIN

    def get_subwindow_margins(self, lines: Optional[int] = None) -> tuple[int, int, int, int]:
        height = self.get_height(lines)
        return self.left + self.MARGIN, self.top - self.MARGIN, self.right - self.MARGIN, self.top - height + self.MARGIN

    def add_info_line(self) -> None:
        if not self.info_line:
            self.info_line = xp.createWidget(
                self.left, self.top, self.right, self.top - self.LINE,
                1, "TEST", 0, self.widget, xp.WidgetClass_Caption
            )
            xp.setWidgetProperty(self.info_line, xp.Property_CaptionLit, 1)
            self.top -= self.cr()

    def check_info_line(self, message: str = "TEST") -> None:
        if xp.getWidgetDescriptor(self.info_line) != message:
            xp.setWidgetDescriptor(self.info_line, message)

    def add_button(self, text: str, subwindow: bool = False, align: str = 'left'):
        width = int(xp.measureString(FONT, text)) + FONT_WIDTH*4
        if align == 'left':
            l, r = self.left + subwindow*self.MARGIN, self.left + width + subwindow*self.MARGIN
        else:
            l, r = self.right - width - subwindow*self.MARGIN, self.right - subwindow*self.MARGIN
        return xp.createWidget(
            l, self.top, r, self.top - self.LINE,
            0, text, 0, self.widget, xp.WidgetClass_Button
        )

    def add_subwindow(self, lines: Optional[int] = None):
        height = self.get_height(lines)
        return xp.createWidget(
            self.left, self.top, self.right, self.top - height,
            1, "", 0, self.widget, xp.WidgetClass_SubWindow
        )

    def add_user_info_widget(self) -> None:
        # user info subwindow
        self.pilot_info_subwindow = self.add_subwindow(lines=2)
        l, t, r, b = self.get_subwindow_margins(lines=2)
        # user info widgets
        caption = xp.createWidget(
            l, t, l + 90, t - self.LINE,
            1, 'Hoppie LOGON:', 0, self.widget, xp.WidgetClass_Caption
        )
        t -= self.cr()
        self.logon_input = xp.createWidget(
            l, t, l + 145, b,
            1, "", 0, self.widget, xp.WidgetClass_TextField
        )
        xp.setWidgetProperty(self.logon_input, xp.Property_MaxCharacters, 24)
        self.logon_caption = xp.createWidget(
            l, t, l + 145, b,
            1, "", 0, self.widget, xp.WidgetClass_Caption
        )
        self.save_button = xp.createWidget(
            l + 148, t, r, b,
            1, "SAVE", 0, self.widget, xp.WidgetClass_Button
        )
        self.edit_button = xp.createWidget(
            l + 148, t, r, b,
            1, "CHANGE", 0, self.widget, xp.WidgetClass_Button
        )
        self.top = b - self.cr()

    def add_content_widget(self, title: str = "", lines: Optional[int] = None) -> None:
        self.content_widget['subwindow'] = self.add_subwindow(lines=lines)
        l, t, r, b = self.get_subwindow_margins()
        if len(title):
            # add title line
            self.content_widget['title'] = xp.createWidget(
                l, t, r, t - self.LINE,
                1, title, 0, self.widget, xp.WidgetClass_Caption
            )
            t -= self.cr()
        # add content lines
        while t > b:
            self.content_widget['lines'].append(
                xp.createWidget(l, t, r, t - self.LINE,
                                1, '--', 0, self.widget, xp.WidgetClass_Caption)
            )
            t -= self.LINE

    def show_content_widget(self) -> None:
        if not xp.isWidgetVisible(self.content_widget['subwindow']):
            xp.showWidget(self.content_widget['subwindow'])
            if self.content_widget['title']:
                xp.showWidget(self.content_widget['title'])
            for el in self.content_widget['lines']:
                xp.showWidget(el)

    def hide_content_widget(self) -> None:
        if xp.isWidgetVisible(self.content_widget['subwindow']):
            xp.hideWidget(self.content_widget['subwindow'])
            if self.content_widget['title']:
                xp.hideWidget(self.content_widget['title'])
            for el in self.content_widget['lines']:
                xp.hideWidget(el)

    def check_content_widget(self, lines: list[str]) -> None:
        content = self.content_widget['lines']
        for i, el in enumerate(lines):
            if i < len(content):
                text = str(el) if not isinstance(el, tuple) else  f"{el[0].upper()}: {el[1]}"
                if not text in xp.getWidgetDescriptor(content[i]):
                    xp.setWidgetDescriptor(content[i], text)

    def populate_content_widget(self, lines: list[tuple[str, str] | str]) -> None:
        content = self.content_widget['lines']
        for i, el in enumerate(lines):
            text = str(el) if not isinstance(el, tuple) else  f"{el[0].upper()}: {el[1]}"
            xp.setWidgetDescriptor(content[i], text)

    def clear_content_widget(self) -> None:
        content = self.content_widget['lines']
        for el in content:
            xp.setWidgetDescriptor(el, "--")

    def switch_window_position(self) -> None:
        if xp.windowIsPoppedOut(self.window):
            xp.setWindowPositioningMode(self.window, xp.WindowPositionFree)
            xp.setWidgetProperty(self.popout_button, xp.Property_ButtonType, xp.LittleUpArrow)
            xp.setWidgetProperty(self.widget, xp.Property_MainWindowHasCloseBoxes, 1)
        else:
            xp.setWindowPositioningMode(self.window, xp.WindowPopOut)
            xp.setWidgetProperty(self.popout_button, xp.Property_ButtonType, xp.LittleDownArrow)
            xp.setWidgetProperty(self.widget, xp.Property_MainWindowHasCloseBoxes, 0)

    def set_window_visible(self) -> None:
        if not xp.getWindowIsVisible(self.window):
            xp.setWidgetProperty(self.widget, xp.Property_MainWindowHasCloseBoxes, 1)
            xp.setWindowIsVisible(self.window, 1)

    def toggle_window(self) -> None:
        if not xp.getWindowIsVisible(self.window):
            self.set_window_visible()
        else:
            xp.setWindowIsVisible(self.window, 0)

    def setup_widget(self, logon: Optional[str] = None) -> None:
        if logon:
            xp.hideWidget(self.logon_input)
            xp.hideWidget(self.save_button)
            text = f"***{logon[-4:]}"
            xp.setWidgetDescriptor(self.logon_caption, text)
            xp.showWidget(self.logon_caption)
            xp.showWidget(self.edit_button)
        else:
            xp.hideWidget(self.logon_caption)
            xp.hideWidget(self.edit_button)
            xp.showWidget(self.logon_input)
            xp.showWidget(self.save_button)
            xp.setKeyboardFocus(self.logon_input)

    def destroy(self) -> None:
        xp.destroyWidget(self.widget)
        # xp.destroyWindow(self.window)


class PythonInterface:
    """Python Interface for HoppieBridge plugin"""

    config_file = Path(PREF_PATH, 'hoppiebridge.prf')

    def __init__(self) -> None:
        self.plugin_name = f"{plugin_name} - {__VERSION__}"
        self.plugin_sig = plugin_sig
        self.plugin_desc = plugin_desc

        # Dref init
        self.dref = None  # dataref initialization in XPluginEnable to avoid issues with string type

        # app init
        self.logon = ''  # logon string
        self.last_poll_time = 0  # last poll time
        self.async_task = False
        self.pending_inbox = deque()  # pending inbox messages

        # load settings
        self.load_settings()

        # widget and windows
        self.monitor = None  # monitor window
        self.status_text = ''  # text displayed in widget info_line
        self.message_content = []  # content of the messages widget
        # self.create_monitor_window(100, 400)

        # create main menu and widget
        self.main_menu = self.create_main_menu()

        # status
        self.waiting_response = False
        self.next_poll_time = 0

    callsign = property(
        safe_attrgetter("dref.callsign", default=''),
        lambda self, value: setattr(self.dref, "callsign", value)
    )

    send_callsign = property(
        safe_attrgetter("dref.send_callsign", default=''),
        lambda self, value: setattr(self.dref, "send_callsign", value)
    )

    inbox = property(
        safe_attrgetter("dref.inbox", default={}),
        lambda self, value: setattr(self.dref, "inbox", value)
    )

    outbox = property(
        safe_attrgetter("dref.outbox", default={}),
        lambda self, value: setattr(self.dref, "outbox", value)
    )

    clear_inbox = property(
        safe_attrgetter("dref.clear_inbox", default=0),
        lambda self, value: setattr(self.dref, "clear_inbox", value)
    )

    comm_ready = property(
        safe_attrgetter("dref.comm_ready", default=0),
        lambda self, value: setattr(self.dref, "comm_ready", value)
    )

    @property
    def avionics_powered(self) -> bool:
        """Check if avionics are on"""
        try:
            return self.dref.avionics_powered
        except Exception as e:
            log(f'**** avionics_powered Error: {e}')
        return False

    @property
    def poll_payload(self) -> dict:
        """Build the ACARS poll request payload for the current session."""
        try:
            return {
                'logon': self.logon,
                'from': self.callsign,
                'to': self.callsign,
                'type': 'poll'
            }
        except Exception as e:
            log(f'**** poll_payload Error: {e}')
            return {}

    @property
    def time_to_poll(self) -> bool:
        """Check if it's time to poll messages"""
        return perf_counter() >= self.next_poll_time

    def calculate_next_poll_time(self) -> None:
        """Calculate the next poll time."""
        if self.waiting_response:
            self.next_poll_time = perf_counter() + random_connection_time(18, 24)
        else:
            self.next_poll_time = perf_counter() + random_connection_time(45, 75)

    def dref_init(self) -> None:
        try:
            self.dref = Dref()
            # reset dref values
            self.inbox = {}
            self.outbox = {}
            self.clear_inbox = False
            self.comm_ready = False
        except Exception as e:
            log(f'**** dref_init Error: {e}')
        # check datarefs creation and availability
        try:
            # read datarefs test
            assert isinstance(self.dref._callsign, DataRef)
            assert isinstance(self.dref._avionics, DataRef)
        except AssertionError as e:
            log(f'**** dref Error: {e}')
            self.dref = None

    def create_main_menu(self):
        # create Menu
        menu = xp.createMenu('HoppieBridge', handler=self.main_menu_callback)
        # add Menu Items
        xp.appendMenuItem(menu, 'Monitor', 1)
        return menu

    def main_menu_callback(self, menuRef, menuItem) -> None:
        """Main menu Callback"""
        if menuItem == 1:
            if not self.monitor:
                self.create_monitor_window(100, 400)
            else:
                self.monitor.set_window_visible()

    def create_monitor_window(self, x: int = 100, y: int = 400) -> None:

        # main window
        self.monitor = FloatingWidget.create_window(f"HoppieBridge {__VERSION__}", x, y, width=MONITOR_WIDTH)

        # LOGON sub window
        self.monitor.add_user_info_widget()

        # info message line
        self.monitor.add_info_line()

        # Messages sub window
        self.monitor.add_content_widget(title='Messages:')

        self.monitor.setup_widget(self.logon)

        # Register our widget handler
        self.monitor_callback = self.monitor_widget_handler
        xp.addWidgetCallback(self.monitor.widget, self.monitor_callback)

    def monitor_widget_handler(self, inMessage, inWidget, inParam1, inParam2) -> int:
        if not self.monitor:
            return 1

        self.monitor.check_info_line(self.status_text)

        if self.message_content:
            self.monitor.clear_content_widget()
            self.monitor.populate_content_widget(self.message_content)
            self.monitor.show_content_widget()
            self.message_content = []

        if inMessage == xp.Message_CloseButtonPushed:
            if self.monitor.window:
                xp.setWindowIsVisible(self.monitor.window, 0)
                return 1

        if inMessage == xp.Msg_PushButtonPressed:
            if inParam1 == self.monitor.popout_button:
                self.monitor.switch_window_position()
            if inParam1 == self.monitor.save_button:
                self.save_settings()
                return 1
            if inParam1 == self.monitor.edit_button:
                xp.setWidgetDescriptor(self.monitor.logon_input, f"{self.logon}")
                self.logon = None
                self.monitor.setup_widget()
                return 1
        return 0

    def dict_to_lines(self, data: dict) -> list[str]:
        if not self.monitor:
            return []
        width = self.monitor.content_width
        result = []
        for k, v in data.items():
            string = f"{k}: {v}"
            lines = string.split('\n')
            for line in lines:
                result.append('-')
                words = line.split(' ')
                for word in words:
                    if xp.measureString(FONT, result[-1] + ' ' + word) + self.monitor.MARGIN * 2 < width:
                        result[-1] += ' ' + word
                    else:
                        result.append(word)
        return result

    def load_settings(self) -> bool:
        if self.config_file.is_file():
            # read file
            with open(self.config_file, 'r', encoding='utf-8') as f:
                data = f.read()
            # parse file
            settings = json.loads(data)
            debug(f"Settings loaded: {settings}", "SETTINGS")
            # check if we have a logon
            self.logon = settings.get('settings').get('logon', '')
            if self.logon:
                debug(f"Logon found: {self.logon}")
            return True
        else:
            # open settings window
            return False

    def save_settings(self) -> None:
        if not self.monitor:
            # sanity check
            return
        logon = xp.getWidgetDescriptor(self.monitor.logon_input).strip()
        debug(f"logon: {logon}", "SETTINGS")
        if logon:
            # save settings
            settings = {'settings': {'logon': logon}}
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f)
            # check file
            self.load_settings()
            self.status_text = 'settings saved'
            self.monitor.setup_widget(self.logon)

    def check_async_task(self) -> None:
        """Check the status of the async task"""
        debug("  ** checking async task ...", "ASYNC")

        if not isinstance(self.async_task, Async):
            # sanity check
            return

        if self.async_task.pending:
            debug("   * async task still pending ...", "ASYNC")
            return

        # async task completed
        debug("   * async task completed ...", "ASYNC")
        result = self.async_task.result
        elapsed = self.async_task.elapsed
        self.async_task = None

        if isinstance(result, Exception):
            log(f" **** Async task failed: {result}")
            self.status_text = "Connection task failed"
            return

        debug(f"   * async task result: {result} | elapsed: {round(elapsed, 3)} sec", "ASYNC")
        if not isinstance(result, dict):
            debug(" **** ACARS Invalid response", "ASYNC")
            self.status_text = "ACARS Invalid response"
            return

        # process result
        if 'error' in result:
            log(f" **** ACARS Error: {result['error']}")
            self.status_text = f"ACARS Error"
            return

        if not ('poll' in result or 'response' in result):
            debug(" **** ACARS Invalid response", "ASYNC")
            self.status_text = "ACARS Invalid response"
            return

        # process received message
        debug(f"Received message: {result}", "ASYNC")
        self.waiting_response = False
        debug(f"comm_ready: {self.comm_ready}", "ASYNC")
        if not self.comm_ready and result.get('poll', '').strip().lower() == 'ok':
            # first successful poll {'poll': 'ok '}
            self.comm_ready = True
            debug("Communication ready", "ASYNC")
            self.status_text = "ACARS ready"
        elif not self.inbox:
            self.inbox = result
            debug("Message added to inbox", "ASYNC")
            self.status_text = "New Message received ..."
            self.message_content = self.dict_to_lines(result)
        else:
            # inbox is not empy, we need to wait for client to clean it
            self.pending_inbox.append(result)
            self.status_text = "Message received but inbox not empty"

    def check_poll_or_send(self) -> None:
        """Check if we need to poll or send messages"""
        debug("  ** checking poll/send ...", "ASYNC")
        debug(f"   * comm_ready: {self.comm_ready} | outbox: {self.outbox} | time_to_poll: {self.time_to_poll}", "ASYNC")
        message = None
        poll_payload = None
        if self.comm_ready and self.outbox:
            # we have a message to send
            try:
                message = self.outbox
                if isinstance(message, dict):
                    # self.outbox: '{"to": "value", "type": "value", "packet": "value"}'
                    message['logon'] = self.logon
                    message['from'] = self.callsign
                    self.outbox = None
                    self.waiting_response = True
            except Exception as e:
                log(f" *** Invalid message format, Error: {e}")

        elif not self.comm_ready or self.time_to_poll:
            # it's time to poll messages or to establish initial communication
            poll_payload = self.poll_payload
            self.last_poll_time = perf_counter()
        else:
            debug("   * nothing to send or poll ...", "ASYNC")
            self.status_text = "ACARS idle"

        if message or poll_payload:
            # we have messages to send or it's time to poll
            debug("  ** starting a new job ...", "ASYNC")
            debug(f"   * message: {message}", "ASYNC")
            debug(f"   * poll_payload: {poll_payload}", "ASYNC")
            self.async_task = Async(
                Bridge.run,
                message=message,
                poll_payload=poll_payload,
            )
            self.async_task.start()
            self.calculate_next_poll_time()

    def loopCallback(self, lastCall, elapsedTime, counter, refCon) -> int:
        """Loop Callback"""

        start = perf_counter()

        # --- Hard blockers -------------------------------------------------

        if not self.dref:
            debug("**** Dref not set, aborting ...", "loopCallback")
            self.status_text = "System Error"
            self.comm_ready = False
            return DEFAULT_SCHEDULE

        if not self.avionics_powered:
            debug("**** Avionics off, aborting ...", "loopCallback")
            self.status_text = "System off"
            self.comm_ready = False
            return DEFAULT_SCHEDULE

        if not self.logon:
            debug(" *** No Logon, aborting ...", "loopCallback")
            self.status_text = "Set Hoppie Logon"
            self.comm_ready = False
            return DEFAULT_SCHEDULE

        # --- Callsign handling --------------------------------------------

        if self.send_callsign:
            debug("  ** sending callsign ...", "loopCallback")
            self.callsign = self.send_callsign
            self.send_callsign = ""

        if not self.callsign:
            debug(" *** waiting for callsign ...")
            self.status_text = "waiting for callsign ..."
            self.comm_ready = False
            return DEFAULT_SCHEDULE

        # --- Main processing ----------------------------------------------

        debug(" *** loopCallback() ...", "loopCallback")
        debug(f"   * callsign: {self.callsign}", "loopCallback")
        debug(f'   * inbox: {self.inbox}', "loopCallback")
        debug(f"   * outbox: {self.outbox}", "loopCallback")
        debug(f"   * time to poll: {self.time_to_poll}", "loopCallback")

        # check if we need to clear inbox
        if self.clear_inbox:
            debug("  ** clearing inbox ...", "loopCallback")
            self.inbox = ""
            self.clear_inbox = False

        # check if we have pending messages
        if self.pending_inbox and not self.inbox:
            self.inbox = self.pending_inbox.popleft()

        # check if we have an async task running
        if self.async_task:
            self.check_async_task()
        else:
            # check if we need to poll and / or send messages
            self.check_poll_or_send()

        debug(
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} "
            f"loopCallback() ended after {perf_counter() - start:.3f}s",
            "loopCallback"
        )
        return DEFAULT_SCHEDULE

    def XPluginStart(self) -> tuple[str, str, str]:
        return self.plugin_name, self.plugin_sig, self.plugin_desc

    def XPluginEnable(self) -> int:
        # dref init 
        self.dref_init()
        # loopCallback
        self.loop = self.loopCallback
        self.loop_id = xp.createFlightLoop(self.loop, phase=1)
        log(f" - {datetime.now(timezone.utc).strftime('%H:%M:%S')} Flightloop created, ID {self.loop_id}")
        xp.scheduleFlightLoop(self.loop_id, interval=DEFAULT_SCHEDULE)
        return 1

    def XPluginDisable(self) -> None:
        pass

    def XPluginStop(self) -> None:
        # Called once by X-Plane on quit (or when plugins are exiting as part of reload)
        xp.destroyFlightLoop(self.loop_id)
        # destroy widgets
        if self.monitor:
            self.monitor.destroy()
        # destroy menu
        xp.destroyMenu(self.main_menu)
        log("flightloop closed, widgets and menu destroyed, exiting ...")

    def XPluginReceiveMessage(self, *args, **kwargs) -> None:
        pass
