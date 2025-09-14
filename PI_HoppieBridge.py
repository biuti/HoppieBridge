"""
HoppieBridge
X-Plane plugin

This X-Plane plugin exposes three string datarefs used to integrate with Hoppie’s ACARS:
hoppiebridge/send_queue — write a JSON string to send a message.
hoppiebridge/poll_queue — read the latest received message as a JSON string.
hoppiebridge/callsign — read the current callsign.

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

As Dref do not permit Array of data, inbox and outbox dref will be json like string that will be encoded and decoded
before sending to the communication bridge.

Strings sent to outbox dref will be like:
{"to": "SERVER", "type": "inforeq", "packet": "METAR LIPE"}
Received messages, alike, will be json like string:
{'response': 'ok {acars info {LIPE 031350Z 05009KT 010V090 9999 BKN055 28/13 Q1014}}'}

Copyright (c) 2025, Antonio Golfari
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

from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime
from time import perf_counter

try:
    import xp
    from XPPython3.utils.datarefs import find_dataref, create_dataref, DataRef
except ImportError:
    print('xp module not found')
    pass

# Version
__VERSION__ = 'v0.3-beta.2'

# Plugin parameters required from XPPython3
plugin_name = 'HoppieBridge'
plugin_sig = 'xppython3.hoppiebridge'
plugin_desc = 'Simple Python script to add drefs for Hoppie\'s ACARS'

# Other parameters
DEFAULT_SCHEDULE = 5  # positive numbers are seconds, 0 disabled, negative numbers are cycles
POLL_SCHEDULE = 65  # seconds
URL = 'https://www.hoppie.nl/acars/system/connect.html'

# widget parameters
MONITOR_WIDTH = 240


try:
    FONT = xp.Font_Proportional
    FONT_WIDTH, FONT_HEIGHT, _ = xp.getFontDimensions(FONT)
    PREF_PATH = Path(xp.getPrefsPath()).parent
    xp.log(f"font width: {FONT_WIDTH} | height: {FONT_HEIGHT}")
except NameError:
    FONT_WIDTH, FONT_HEIGHT = 10, 10
    PREF_PATH = Path(os.path.dirname(__file__)).parent


# safe attrgetter with default value
def safe_attrgetter(path, default=None):
    def getter(obj):
        try:
            return operator.attrgetter(path)(obj)
        except Exception as e:
            xp.log(f"**** {path} Error: {e}")
            return default
    return getter


def random_connection_time(min: int = 45, max: int = 75) -> int:
    """Calculate a random connection time between 45 and 75 seconds."""
    return random.randint(min, max)


def parse_message(raw: str) -> dict:
    """Convert raw string into dict using JSON first, ast.literal_eval as fallback."""
    if not raw or not raw.strip():
        return {}  # empty string → empty dict

    try:
        # JSON requires double quotes, so replace single quotes optimistically
        return json.loads(raw.replace("'", '"'))
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            raise ValueError(f"Cannot parse message: {raw!r}")


def format_message(msg: dict | str) -> str:
    """Convert Python dict or string into a string suitable for ACARS/X-Plane."""
    if isinstance(msg, dict):
        try:
            return json.dumps(msg)   # valid JSON
        except (TypeError, ValueError):
            return str(msg)          # last resort
    return str(msg)


class Dref:
    """Adapter around XPPython3 DataRefs used by HoppieBridge."""

    def __init__(self) -> None:
        # created datarefs
        self._send_queue = create_dataref('hoppiebridge/send_queue', 'string')
        self._poll_queue = create_dataref('hoppiebridge/poll_queue', 'string')
        self._callsign = create_dataref('hoppiebridge/callsign', 'string')
        # standard datarefs
        self._avionics = find_dataref('sim/cockpit/electrical/avionics_on')

    @property
    def avionics_powered(self) -> bool:
        """True if avionics are powered on."""
        return self._avionics.value == 1

    @property
    def callsign(self) -> str:
        """Get the callsign"""
        xp.log(f'  ** _callsign: {self._callsign.value} | type: {type(self._callsign.value)} | len: {len(self._callsign.value)}')
        return self._callsign.value

    @property
    def inbox(self) -> dict:
        """Return decoded inbox messages"""
        xp.log(f'  ** _poll_queue: {self._poll_queue.value} | type: {type(self._poll_queue.value)} | dim: {self._poll_queue._dim} | len: {len(self._poll_queue.value)}')
        return parse_message(self._poll_queue.value)

    @inbox.setter
    def inbox(self, message: dict | str) -> None:
        """Set inbox with a message (encoded before storing)"""
        xp.log(f'  ** add_to_inbox: {message} | type: {type(message)}')
        self._poll_queue.value = format_message(message)

    @property
    def outbox(self) -> dict:
        """Return decoded outbox messages"""
        xp.log(f'   * _send_queue: {self._send_queue.value} | type: {type(self._send_queue.value)} | dim: {self._send_queue._dim} | len: {len(self._send_queue.value)}')
        return parse_message(self._send_queue.value)

    @outbox.setter
    def outbox(self, message: dict | str) -> None:
        """Set outbox with a message (encoded before storing)"""
        xp.log(f'  ** add_to_outbox: {message} | type: {type(message)}')
        self._send_queue.value = format_message(message)


class Async(threading.Thread):
    """Async thread to handle connection to Hoppie's ACARS"""

    def __init__(self, task, *args, **kwargs) -> None:
        self.pid = os.getpid()
        self.task = task
        self.cancel = threading.Event()
        self.args = args
        self.kwargs = kwargs
        self.elapsed = 0
        self.result = None
        threading.Thread.__init__(self)

        self.daemon = True  # Daemon thread will exit when the main program exits

    @property
    def pending(self):
        return self.is_alive()

    def run(self):
        start = perf_counter()
        try:
            self.result = self.task(*self.args, **self.kwargs)
        except Exception as e:
            self.result = e
        finally:
            self.elapsed = perf_counter() - start
            print(f"Async task {self.task.__name__} completed in {self.elapsed:.3f} seconds")

    def stop(self):
        """Stop the async task"""
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
    def run(message: dict | None = None, poll_payload: dict | None = None) -> dict:
        bridge = Bridge(message or {}, poll_payload or {})
        response = {}
        try:
            if bridge.message:
                response = bridge.query(bridge.message)
            elif bridge.poll_payload:
                response = bridge.poll()
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
            raise
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

    def get_height(self, lines: int | None = None) -> int:
        if not lines:
            return self.top - self.bottom
        else:
            return self.LINE*lines + 2*self.MARGIN

    def get_subwindow_margins(self, lines: int | None = None) -> tuple[int, int, int, int]:
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

    def add_subwindow(self, lines: int = None):
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
        xp.setWidgetProperty(self.logon_input, xp.Property_MaxCharacters, 16)
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

    def add_content_widget(self, title: str = "", lines: int | None = None):
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

    def show_content_widget(self):
        if not xp.isWidgetVisible(self.content_widget['subwindow']):
            xp.showWidget(self.content_widget['subwindow'])
            if self.content_widget['title']:
                xp.showWidget(self.content_widget['title'])
            for el in self.content_widget['lines']:
                xp.showWidget(el)

    def hide_content_widget(self):
        if xp.isWidgetVisible(self.content_widget['subwindow']):
            xp.hideWidget(self.content_widget['subwindow'])
            if self.content_widget['title']:
                xp.hideWidget(self.content_widget['title'])
            for el in self.content_widget['lines']:
                xp.hideWidget(el)

    def check_content_widget(self, lines: list[str]):
        content = self.content_widget['lines']
        for i, el in enumerate(lines):
            if i < len(content):
                text = str(el) if not isinstance(el, tuple) else  f"{el[0].upper()}: {el[1]}"
                if not text in xp.getWidgetDescriptor(content[i]):
                    xp.setWidgetDescriptor(content[i], text)

    def populate_content_widget(self, lines: list[tuple[str, str] or str]):
        content = self.content_widget['lines']
        for i, el in enumerate(lines):
            text = str(el) if not isinstance(el, tuple) else  f"{el[0].upper()}: {el[1]}"
            xp.setWidgetDescriptor(content[i], text)

    def clear_content_widget(self):
        content = self.content_widget['lines']
        for el in content:
            xp.setWidgetDescriptor(el, "--")

    def switch_window_position(self):
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

    def setup_widget(self, logon: str = None) -> None:
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
        self.pending_inbox = []

        # load settings
        self.load_settings()

        # widget and windows
        self.status_text = ''  # text displayed in widget info_line
        self.message_content = []  # content of the messages widget
        self.create_monitor_window(100, 400)

        # create main menu and widget
        self.main_menu = self.create_main_menu()

        # status
        self.first_message_sent = False
        self.waiting_response = False
        self.next_poll_time = 0

    inbox = property(
        safe_attrgetter("dref.inbox", default={}),
        lambda self, value: setattr(self.dref, "inbox", value)
    )

    outbox = property(
        safe_attrgetter("dref.outbox", default={}),
        lambda self, value: setattr(self.dref, "outbox", value)
    )

    @property
    def avionics_powered(self) -> bool:
        """Check if avionics are on"""
        try:
            return self.dref.avionics_powered
        except Exception as e:
            xp.log(f'**** avionics_powered Error: {e}')
        return False

    @property
    def callsign(self) -> str:
        """Get the callsign from the dref"""
        try:
            return self.dref.callsign
        except Exception as e:
            xp.log(f'**** callsign Error: {e}')
        return ''

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
            xp.log(f'**** poll_payload Error: {e}')
            return {}

    @property
    def time_to_poll(self) -> bool:
        """Check if it's time to poll messages"""
        if not self.first_message_sent:
            return False
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
        except Exception as e:
            xp.log(f'**** dref_init Error: {e}')
        # check datarefs creation and availability
        try:
            # read datarefs test
            assert isinstance(self.dref._callsign, DataRef)
            assert isinstance(self.dref._avionics, DataRef)
        except AssertionError as e:
            xp.log(f'**** dref Error: {e}')
            self.dref = None

    def create_main_menu(self):
        # create Menu
        menu = xp.createMenu('HoppieBridge', handler=self.main_menu_callback)
        # add Menu Items
        xp.appendMenuItem(menu, 'Monitor', 1)
        return menu

    def main_menu_callback(self, menuRef, menuItem):
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

    def monitor_widget_handler(self, inMessage, inWidget, inParam1, inParam2):
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

    def format_message(self, data: dict) -> list:
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
            xp.log(f"Settings loaded: {settings}")
            # check if we have a logon
            self.logon = settings.get('settings').get('logon', '')
            if self.logon:
                xp.log(f"Logon found: {self.logon}")
            return True
        else:
            # open settings window
            return False

    def save_settings(self) -> None:
        logon = xp.getWidgetDescriptor(self.monitor.logon_input).strip()
        xp.log(f"logon: {logon}")
        if logon:
            # save settings
            settings = {'settings': {'logon': logon}}
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f)
            # check file
            self.load_settings()
            self.status_text = 'settings saved'
            self.monitor.setup_widget(self.logon)

    def loopCallback(self, lastCall, elapsedTime, counter, refCon):
        """Loop Callback"""
        t = datetime.now()
        start = perf_counter()

        if not self.dref:
            xp.log("**** Dref not set, aborting ...")
            self.status_text = "System Error"

        elif not self.avionics_powered:
            xp.log("**** Avionics off, aborting ...")
            self.status_text = "System off"

        elif not self.logon:
            xp.log(" *** No Logon, aborting ...")
            self.status_text = "Set Hoppie Logon"

        elif not self.callsign:
            xp.log(" *** waiting for callsign ...")
            self.status_text = "waiting for callsign"

        else:
            xp.log(" *** loopCallback() ...")
            xp.log(f"   * callsign: {self.callsign}")
            xp.log(f'   * inbox: {self.inbox}')
            xp.log(f"   * outbox: {self.outbox}")
            xp.log(f"   * time to poll: {self.time_to_poll}")

            self.status_text = "ACARS ready"

            # check if we have pending messages
            if len(self.pending_inbox) and not self.inbox:
                self.inbox = self.pending_inbox.pop()

            # check if we need to send or poll messages
            if self.async_task:
                if not self.async_task.pending:
                    # async task completed
                    self.async_task.join()
                    xp.log(f"  ** Async task completed in {self.async_task.elapsed:.3f} sec")
                    result = self.async_task.result
                    if isinstance(result, Exception):
                        # log the error
                        self.status_text = "Connection task failed"
                        xp.log(f"**** Async task failed: {result}")
                    else:
                        # check if we have received messages
                        if result.get('error'):
                            self.status_text = f"Error: {result['error']}"
                            xp.log(f"**** Error: {result['error']}")
                        else:
                            # process received message
                            xp.log(f"Received message: {result}")
                            self.waiting_response = False
                            if not self.inbox:
                                self.inbox = result
                                xp.log("Message added to inbox")
                                self.status_text = "New Message received ..."
                                self.message_content = self.format_message(result)
                            else:
                                # inbox is not empy, we need to wait for client to clean it
                                self.pending_inbox.append(result)
                                self.status_text = "Message received but inbox not empty"
                    self.async_task = False
                else:
                    xp.log(f"  ** Async job {self.async_task.pid} still pending, waiting ...")
                    self.status_text = "No new messages"
            else:
                # check if we need to poll and / or send messages
                xp.log("  ** No async task running, checking outbox and poll data ...")
                message = {}
                poll_payload = {}
                if self.outbox:
                    # we have a message to send
                    try:
                        message = self.outbox
                        if isinstance(message, dict):
                            # self.outbox: '{"to": "value", "type": "value", "packet": "value"}'
                            message['logon'] = self.logon
                            message['from'] = self.callsign
                            self.outbox = ''
                            if not self.first_message_sent:
                                self.first_message_sent = True
                            self.waiting_response = True
                    except Exception as e:
                        xp.log(f" *** Invalid message format, Error: {e}")
                elif self.time_to_poll:
                    # it's time to poll messages
                    poll_payload = self.poll_payload
                    self.last_poll_time = perf_counter()

                if message or poll_payload:
                    # we have messages to send or it's time to poll
                    xp.log("  ** starting a new job ...")
                    xp.log(f"   * message: {message}")
                    xp.log(f"   * poll_payload: {poll_payload}")
                    self.async_task = Async(
                        Bridge.run,
                        message=message,
                        poll_payload=poll_payload,
                    )
                    self.async_task.start()
                    self.calculate_next_poll_time()

        xp.log(f" {t.strftime('%H:%M:%S')} - loopCallback() ended after {round(perf_counter() - start, 3)} sec")
        return DEFAULT_SCHEDULE

    def XPluginStart(self):
        return self.plugin_name, self.plugin_sig, self.plugin_desc

    def XPluginEnable(self):
        # dref init 
        self.dref_init()
        # loopCallback
        self.loop = self.loopCallback
        self.loop_id = xp.createFlightLoop(self.loop, phase=1)
        xp.log(f" - {datetime.now().strftime('%H:%M:%S')} Flightloop created, ID {self.loop_id}")
        xp.scheduleFlightLoop(self.loop_id, interval=DEFAULT_SCHEDULE)
        return 1

    def XPluginDisable(self):
        pass

    def XPluginStop(self):
        # Called once by X-Plane on quit (or when plugins are exiting as part of reload)
        xp.destroyFlightLoop(self.loop_id)
        xp.log("flightloop closed, exiting ...")

    def XPluginReceiveMessage(self, *args, **kwargs):
        pass
