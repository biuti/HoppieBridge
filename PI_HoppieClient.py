"""
HoppieClient
X-Plane plugin

Copyright (c) 2026, Antonio Golfari
All rights reserved.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree. 
"""

from __future__ import annotations

import os
import ast
import json
import operator

from pathlib import Path
from enum import Enum
from datetime import datetime
from time import perf_counter

try:
    import xp
    from XPPython3.utils.datarefs import find_dataref
except ImportError:
    print('xp module not found')
    pass

# Version
__VERSION__ = 'v0.5-beta.6'

# Plugin parameters required from XPPython3
plugin_name = 'HoppieClient'
plugin_sig = 'xppython3.hoppieclient'
plugin_desc = 'Simple Python script to test Hoppie\'s ACARS'

# Other parameters
DEFAULT_SCHEDULE = 5  # positive numbers are seconds, 0 disabled, negative numbers are cycles
URL = 'https://www.hoppie.nl/acars/system/connect.html'

# widget parameters
MONITOR_WIDTH = 480


try:
    FONT = xp.Font_Proportional
    FONT_WIDTH, FONT_HEIGHT, _ = xp.getFontDimensions(FONT)
    PREF_PATH = Path(xp.getPrefsPath()).parent
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


def looks_like_json(raw: str) -> bool:
    """
    Cheap heuristic:
    - starts with '{'
    - contains at least one double-quoted key
    """
    if not raw:
        return False

    s = raw.lstrip()
    return s.startswith('{') and '"' in s


def parse_message(raw: str) -> dict:
    """
    Best-effort decoder:
    - JSON if it looks like JSON
    - ast.literal_eval as fallback
    - never raises
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
        xp.log(f"**** Cannot parse message: {raw!r}")
        return {}


def format_message(msg: dict | str) -> str:
    """Convert Python dict or string into a string suitable for ACARS/X-Plane."""
    if isinstance(msg, dict):
        try:
            return json.dumps(msg)   # valid JSON
        except (TypeError, ValueError):
            return str(msg)          # last resort
    return str(msg)


class MsgType(str, Enum):
    PROGRESS = "progress"
    CPDLC = "cpdlc"
    TELEX = "telex"
    PING = "ping"
    INFOREQ = "inforeq"
    POSREQ = "posreq"
    POSITION = "position"
    DATAREQ = "datareq"
    POLL = "poll"
    PEEK = "peek"


class Dref:

    def __init__(self) -> None:
        # standard datarefs
        self._send_queue = find_dataref('hoppiebridge/send_queue')  # legacy raw queue
        self._send_message_to = find_dataref('hoppiebridge/send_message_to')
        self._send_message_type = find_dataref('hoppiebridge/send_message_type')
        self._send_message_packet = find_dataref('hoppiebridge/send_message_packet')
        self._poll_queue = find_dataref('hoppiebridge/poll_queue')  # legacy raw queue
        self._poll_message_origin = find_dataref('hoppiebridge/poll_message_origin')
        self._poll_message_from = find_dataref('hoppiebridge/poll_message_from')
        self._poll_message_type = find_dataref('hoppiebridge/poll_message_type')
        self._poll_message_packet = find_dataref('hoppiebridge/poll_message_packet')
        self._callsign = find_dataref('hoppiebridge/callsign')
        self._send_callsign = find_dataref('hoppiebridge/send_callsign')
        self._poll_queue_clear = find_dataref('hoppiebridge/poll_queue_clear')
        self._comm_ready = find_dataref('hoppiebridge/comm_ready')
        self._avionics = find_dataref('sim/cockpit/electrical/avionics_on')

    @property
    def comm_ready(self) -> bool:
        """Check if communication is ready"""
        return bool(self._comm_ready.value)

    @property
    def avionics_powered(self) -> bool:
        """Check if avionics are on"""
        return bool(self._avionics.value)

    @property
    def callsign(self) -> str:
        """Get the callsign"""
        return str(self._callsign.value or "").strip()

    @property
    def send_callsign(self) -> str:
        """Get the callsign"""
        return str(self._send_callsign.value or "").strip()

    @send_callsign.setter
    def send_callsign(self, value: str):
        """Set the callsign"""
        if isinstance(value, str):
            self._send_callsign.value = value

    @property
    def inbox(self) -> dict:
        """Return decoded inbox messages"""
        if self._poll_message_origin.value.strip() and self._poll_message_packet.value.strip():
            # structured message
            return {
                "origin": self._poll_message_origin.value.strip(),
                "from": self._poll_message_from.value.strip() or "",
                "type": self._poll_message_type.value.strip() or "",
                "packet": self._poll_message_packet.value.strip(),
            }
        return parse_message(self._poll_queue.value)

    @inbox.setter
    def inbox(self, message: dict | str | None) -> None:
        """Clear inbox after message is received (legacy and structured) - no value needed"""

        # Clear structured fields
        self._poll_message_origin.value = ""
        self._poll_message_from.value = ""
        self._poll_message_type.value = ""
        self._poll_message_packet.value = ""

        # Clear legacy queue
        self._poll_queue.value = ""

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
            xp.log("ACARS outbox: incomplete structured message, ignoring")

        # 2. Legacy raw queue
        raw = self._send_queue.value.strip()
        if raw:
            return parse_message(raw)

        # 3. Nothing to send
        return {}

    @outbox.setter
    def outbox(self, value: dict) -> None:
        """fill outbox message components drefs from a dict (legacy and structured)"""

        # structured fields
        to_, type_, packet = value.get("to", ""), value.get("type", ""), value.get("packet", "") 
        self._send_message_to.value = to_
        self._send_message_type.value = type_
        self._send_message_packet.value = packet

        # legacy queue
        self._send_queue.value = format_message(value)

    @property
    def clear_inbox(self) -> bool:
        """Return clear inbox request status"""
        return bool(self._poll_queue_clear.value)

    @clear_inbox.setter
    def clear_inbox(self, value: bool | int) -> None:
        """Set clear inbox request status"""
        self._poll_queue_clear.value = int(value)


class FloatingWidget:

    LINE = FONT_HEIGHT + 4
    WIDTH = 480
    HEIGHT = 640
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
                1, "", 0, self.widget, xp.WidgetClass_Caption
            )
            xp.setWidgetProperty(self.info_line, xp.Property_CaptionLit, 1)
            self.top -= self.cr()

    def check_info_line(self, message: str) -> None:
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
            l, t, l + 90, b,
            1, 'Flight ID:', 0, self.widget, xp.WidgetClass_Caption
        )
        l += FONT_WIDTH * (len('Flight ID:') + 1)
        self.fight_ID_input = xp.createWidget(
            l, t, l + FONT_WIDTH*10, b,
            1, "", 0, self.widget, xp.WidgetClass_TextField
        )
        xp.setWidgetProperty(self.fight_ID_input, xp.Property_MaxCharacters, 10)

        l += FONT_WIDTH * 11
        self.set_flight_button = xp.createWidget(
            l, t, l + FONT_WIDTH*4, b,
            1, "SET", 0, self.widget, xp.WidgetClass_Button
        )
        self.top = b - self.cr()

    def add_test_buttons_widget(self) -> None:
        """Add test buttons to the widget"""
        # add test buttons
        self.test_buttons_subwindow = self.add_subwindow(lines=2)
        l, t, r, b = self.get_subwindow_margins(lines=2)
        # add buttons

        # self.test_reqinfo = self.add_button("ReqInfo", subwindow=True, align='left')
        # self.test_telex = self.add_button("Telex", subwindow=True, align='right')
        self.reqinfo_button = xp.createWidget(
            l, t, l + 90, b,
            1, "ReqInfo", 0, self.widget, xp.WidgetClass_Button
        )
        self.telex_button = xp.createWidget(
            r - 90, t, r, b,
            1, "Telex", 0, self.widget, xp.WidgetClass_Button
        )
        self.top = b - self.cr()
        # xp.setWidgetProperty(self.test_reqinfo, xp.Property_ButtonType, xp.LittleUpArrow)
        # xp.setWidgetProperty(self.test_telex, xp.Property_ButtonType, xp.LittleDownArrow)

    def add_message_type_buttons(self) -> None:
        # add test buttons
        self.message_type_subwindow = self.add_subwindow(lines=2)
        l, t, r, b = self.get_subwindow_margins(lines=2)

        # add buttons
        # METAR
        n = FONT_WIDTH * (len('METAR') + 1)
        # xp.createWidget(l, t, l + n, b, 1, 'METAR', 0, self.widget, xp.WidgetClass_Caption)
        inforeq_check = xp.createWidget(
            l, t, l + n, b, 1, 'METAR', 0, self.widget, xp.WidgetClass_Button
        )
        l += n + 20

        # ATIS
        n = FONT_WIDTH * (len('ATIS') + 1)
        # xp.createWidget(l, t, l + n, b, 1, 'ATIS', 0, self.widget, xp.WidgetClass_Caption)
        telex_check = xp.createWidget(
            l, t, l + n, b, 1, 'ATIS', 0, self.widget, xp.WidgetClass_Button
        )
        l += n + 20

        # CPDLC
        n = FONT_WIDTH * (len('CPDLC') + 1)
        # xp.createWidget(l, t, l + n, b, 1, 'CPDLC', 0, self.widget, xp.WidgetClass_Caption)
        cpdlc_check = xp.createWidget(
            l, t, l + n, b, 1, 'CPDLC', 0, self.widget, xp.WidgetClass_Button
        )

        self.message_type = {
            inforeq_check: 'inforeq',
            telex_check: 'telex',
            cpdlc_check: 'cpdlc'
        }

        for k, v in self.message_type.items():
            xp.setWidgetProperty(k, xp.Property_ButtonState, xp.RadioButton)
            xp.setWidgetProperty(k, xp.Property_ButtonBehavior, xp.ButtonBehaviorRadioButton)
            xp.setWidgetProperty(k, xp.Property_ButtonState, int('cpdlc' == v))

        self.top = b - self.cr()

    def add_station_widget(self) -> None:
        # user info subwindow
        self.station_subwindow = self.add_subwindow(lines=2)
        l, t, r, b = self.get_subwindow_margins(lines=2)
        # user info widgets
        caption = xp.createWidget(
            l, t, l + 90, b,
            1, 'Station:', 0, self.widget, xp.WidgetClass_Caption
        )
        n = FONT_WIDTH * 6
        self.station_input = xp.createWidget(
            l + n, t, l + n + 100, b,
            1, "", 0, self.widget, xp.WidgetClass_TextField
        )
        xp.setWidgetProperty(self.fight_ID_input, xp.Property_MaxCharacters, 20)

        self.top = b - self.cr()

    def add_message_widget(self) -> None:
        # user info subwindow
        self.message_subwindow = self.add_subwindow(lines=2)
        l, t, r, b = self.get_subwindow_margins(lines=2)
        # user info widgets
        caption = xp.createWidget(
            l, t, l + 90, b,
            1, 'text:', 0, self.widget, xp.WidgetClass_Caption
        )
        n = FONT_WIDTH * 6
        self.text_input = xp.createWidget(
            l + n, t, r - 80, b,
            1, "", 0, self.widget, xp.WidgetClass_TextField
        )
        xp.setWidgetProperty(self.fight_ID_input, xp.Property_MaxCharacters, 100)

        self.send_button = xp.createWidget(
            r - 75, t, r, b,
            1, "SEND", 0, self.widget, xp.WidgetClass_Button
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

    def set_window_visible(self) -> None:
        if not xp.getWindowIsVisible(self.window):
            xp.setWidgetProperty(self.widget, xp.Property_MainWindowHasCloseBoxes, 1)
            xp.setWindowIsVisible(self.window, 1)

    def toggle_window(self) -> None:
        if not xp.getWindowIsVisible(self.window):
            self.set_window_visible()
        else:
            xp.setWindowIsVisible(self.window, 0)

    def setup_widget(self, outbox: str = None) -> None:
            xp.showWidget(self.fight_ID_input)
            xp.showWidget(self.set_flight_button)
            xp.setKeyboardFocus(self.fight_ID_input)

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

        # app init
        # self.flight_ID = ''  # callsign string
        self.pending_outbox = []

        # widget and windows
        self.monitor = None
        self.status_text = ""  # text displayed in widget info_line
        self.message_content = []  # content of the messages widget
        # self.create_monitor_window(400, 800)

        # create main menu and widget
        self.main_menu = self.create_main_menu()

    send_callsign = property(
        safe_attrgetter("dref.send_callsign", default=''),
        lambda self, value: setattr(self.dref, "send_callsign", value)
    )

    outbox = property(
        safe_attrgetter("dref.outbox", default={}),
        lambda self, value: setattr(self.dref, "outbox", value)
    )

    inbox = property(
        safe_attrgetter("dref.inbox", default={}),
        lambda self, value: setattr(self.dref, "inbox", value)
    )

    clear_inbox = property(
        safe_attrgetter("dref.clear_inbox", default=False),
        lambda self, value: setattr(self.dref, "clear_inbox", value)
    )

    @property
    def callsign(self) -> str:
        """Get the callsign"""
        try:
            return self.dref.callsign
        except Exception as e:
            xp.log(f'callsign Error: {e}')
        return ""

    @property
    def avionics_powered(self) -> bool:
        """Check if avionics are on"""
        try:
            return self.dref.avionics_powered
        except Exception as e:
            xp.log(f'avionics_powered Error: {e}')
        return False

    @property
    def comm_ready(self) -> bool:
        """Check if avionics are on"""
        try:
            return self.dref.comm_ready
        except Exception as e:
            xp.log(f'comm_ready Error: {e}')
        return False

    @property
    def dref(self) -> Dref:
        if not hasattr(self, "_dref"):
            self._dref = Dref()
        return self._dref

    @property
    def message_type(self) -> str:
        return next((v for k, v in self.monitor.message_type.items() if xp.getWidgetProperty(k, xp.Property_ButtonState, None)), None)

    def create_main_menu(self):
        # create Menu
        menu = xp.createMenu('HoppieClient', handler=self.main_menu_callback)
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
        self.monitor = FloatingWidget.create_window(f"HoppieClient {__VERSION__}", x, y, width=MONITOR_WIDTH)

        # LOGON sub window
        self.monitor.add_user_info_widget()
        # self.monitor.top -= self.monitor.cr()

        # info message line
        self.monitor.add_info_line()
        # self.monitor.top -= self.monitor.cr()

        # # Test buttons sub window
        self.monitor.add_message_type_buttons()
        self.monitor.add_station_widget()
        self.monitor.add_message_widget()

        # Messages sub window
        self.monitor.add_content_widget(title='Messages:')

        self.monitor.setup_widget()

        # Register our widget handler
        self.monitorWidgetHandlerCB = self.monitorWidgetHandler
        xp.addWidgetCallback(self.monitor.widget, self.monitorWidgetHandlerCB)

    def monitorWidgetHandler(self, inMessage, inWidget, inParam1, inParam2):
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

        if inMessage == xp.Msg_ButtonStateChanged and inParam1 in self.monitor.message_type:
            if inParam2:
                for i in self.monitor.message_type:
                    if i != inParam1:
                        xp.setWidgetProperty(i, xp.Property_ButtonState, 0)
            else:
                xp.setWidgetProperty(inParam1, xp.Property_ButtonState, 1)
            return 1

        if inMessage == xp.Msg_PushButtonPressed:
            if inParam1 == self.monitor.set_flight_button:
                self.send_flight_ID()
                return 1
            if inParam1 == self.monitor.send_button:
                self.send_message()
                return 1
        return 0

    def send_flight_ID(self):
        try:
            ID = xp.getWidgetDescriptor(self.monitor.fight_ID_input).strip().upper()
            self.send_callsign = ID
            xp.log(f'  ** send_flight_ID: {self.dref._send_callsign.value}')
        except Exception as e:
            xp.log(f'  ** send_flight_ID Error: {e}')

    def send_message(self) -> None:
        if not self.monitor:
            # sanity check, should not happen
            return

        station = xp.getWidgetDescriptor(self.monitor.station_input).strip().upper()
        text = xp.getWidgetDescriptor(self.monitor.text_input).strip().upper()
        message = {
            "to": f"{station}",
            "type": f"{self.message_type}",
            "packet": f"{text}"
        }
        if not self.outbox and not len(self.pending_outbox):
            self.outbox = message
            self.status_text = f"Requesting {self.message_type} ..."
        else:
            self.pending_outbox.append(message)

    def format_message(self, data: dict) -> list:
        """Format message content for display in widget"""
        if not self.monitor:
            # sanity check, should not happen
            return []

        # create lines from D-ATIS string
        width = self.monitor.content_width
        print(f"****** format_message | width: {width} | char: {FONT_WIDTH}")
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

    def loopCallback(self, lastCall, elapsedTime, counter, refCon):
        """Loop Callback"""
        t = datetime.now()
        start = perf_counter()
        loop_schedule = DEFAULT_SCHEDULE

        if not self.dref:
            xp.log(f"**** Dref not set, aborting ...")
            self.status_text = "System Error"

        elif not self.avionics_powered:
            xp.log(f"**** Avionics off, aborting ...")
            self.status_text = "System off"

        else:
            if self.callsign != self.send_callsign:
                xp.log(f" *** setting callsign to {self.send_callsign} ...")
                self.status_text = f"setting callsign to {self.send_callsign} ..."
                self.dref.send_callsign = self.send_callsign

            if not self.callsign:
                xp.log(f" *** waiting for callsign ...")
                self.status_text = "waiting for callsign"

            elif not self.comm_ready:
                xp.log(f" **** Comm not ready ...")
                self.status_text = "connecting ..."

            else:
                self.status_text = "ACARS active ..."

                # check if there are pending messages to send
                if len(self.pending_outbox) and not self.outbox:
                    message = self.pending_outbox.pop()
                    self.outbox = message
                    self.status_text = "sending queued message ..."
                # check if there are incoming messages
                if self.inbox:
                    message = self.inbox
                    self.status_text = f"{t.strftime('%H:%M:%S')} New Message received ..."
                    self.message_content = self.format_message(message)
                    # message received, clear dref
                    self.clear_inbox = True

        return loop_schedule

    def XPluginStart(self):
        return self.plugin_name, self.plugin_sig, self.plugin_desc

    def XPluginEnable(self):
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
        # destroy widgets
        if self.monitor:
            self.monitor.destroy()
        # destroy menu
        xp.destroyMenu(self.main_menu)
        xp.log("flightloop closed, widgets and menu destroyed, exiting ...")

    def XPluginReceiveMessage(self, *args, **kwargs):
        pass
