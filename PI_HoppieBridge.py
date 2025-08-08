"""
HoppieBridge
X-Plane plugin

this script creates drefs needed to comunicate with Hoppie's ACARS system,
so to make it simpler for developers to create their own ACARS interface.
Drefs:
- hoppiebridge/send_queue: data, to send messages to Hoppie's ACARS
- hoppiebridge/poll_queue: data, to poll messages from Hoppie's ACARS
- hoppiebridge/callsign: data, to set your callsign

received messages will be in poll_queue, and sent messages should be added to send_queue.

Message format:
The messages for Hoppie's ACARS should be in JSON format, with the following structure:
{
    "logon": string, # your logon string
    "from": string, # your callsign
    "to": string, # destination callsign or "all"
    "type": string, # type of message, one of "progress", "cpdlc", "telex", "ping", "inforeq", "posreq", "position", "datareq", "poll", or "peek".
    "packet": string,  # the actual message to send
}

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
import threading
import requests

from pathlib import Path
from xml.etree import ElementTree as ET
from datetime import datetime
from time import perf_counter

try:
    import xp
    from XPPython3.utils.easy_python import EasyPython
    from XPPython3.utils.datarefs import find_dataref, create_dataref, DataRef
    # needed until XPPython3 will be fixed to use the new DataRef class
    from XPLMDefs import *
except ImportError:
    print('xp module not found')
    pass

# Version
__VERSION__ = 'v0.1-beta.1'

# Plugin parameters required from XPPython3
plugin_name = 'HoppieBridge'
plugin_sig = 'xppython3.hoppiebridge'
plugin_desc = 'Simple Python script to add drefs for Hoppie\'s ACARS'

# Other parameters
DEFAULT_SCHEDULE = 5  # positive numbers are seconds, 0 disabled, negative numbers are cycles
POLL_SCHEDULE = 65  # seconds
URL = 'https://www.hoppie.nl/acars/system/connect.html'

# widget parameters
try:
    FONT = xp.Font_Proportional
    FONT_WIDTH, FONT_HEIGHT, _ = xp.getFontDimensions(FONT)
    PREF_PATH = Path(xp.getPrefsPath()).parent
    xp.log(f"font width: {FONT_WIDTH} | height: {FONT_HEIGHT}")
except NameError:
    FONT_WIDTH, FONT_HEIGHT = 10, 10
    PREF_PATH = Path(os.path.dirname(__file__)).parent


MONITOR_WIDTH = 240


import sys
from XPLMDataAccess import *
from XPLMUtilities import *
from XPLMPlugin import *
from XPLMDefs import *


class EasyDref:
    '''
    Easy Dataref access

    Copyright (C) 2011  Joan Perez i Cauhe
    '''

    datarefs = []
    plugin = False

    def __init__(self, dataref, type="float", register=False, writable=False):
        # Clear dataref
        dataref = dataref.strip()
        self.isarray, dref = False, False
        self.register = register

        if ('"' in dataref):
            dref = dataref.split('"')[1]
            dataref = dataref[dataref.rfind('"') + 1:]

        if ('(' in dataref):
            # Detect embedded type, and strip it from dataref
            type = dataref[dataref.find('(') + 1:dataref.find(')')]
            dataref = dataref[:dataref.find('(')] + dataref[dataref.find(')') + 1:]

        if ('[' in dataref):
            # We have an array
            self.isarray = True
            range = dataref[dataref.find('[') + 1:dataref.find(']')].split(':')
            dataref = dataref[:dataref.find('[')]
            if (len(range) < 2):
                range.append(range[0])

            self.initArrayDref(range[0], range[1], type)

        elif (type == "int"):
            self.dr_get = XPLMGetDatai
            self.dr_set = XPLMSetDatai
            self.dr_type = xplmType_Int
            self.cast = int
        elif (type == "float"):
            self.dr_get = XPLMGetDataf
            self.dr_set = XPLMSetDataf
            self.dr_type = xplmType_Float
            self.cast = float
        elif (type == "double"):
            self.dr_get = XPLMGetDatad
            self.dr_set = XPLMSetDatad
            self.dr_type = xplmType_Double
            self.cast = float
        else:
            print("ERROR: invalid DataRef type: {}".format(type))

        if dref: dataref = dref

        self.dataref = dataref

        if register:
            self.setCB, self.getCB = False, False
            self.rsetCB, self.rgetCB = False, False

            if self.isarray:
                if writable: self.rsetCB = self.set_cb
                self.rgetCB = self.rget_cb
            else:
                if writable: self.setCB = self.set_cb
                self.getCB = self.get_cb

            self.DataRef = XPLMRegisterDataAccessor(dataref, self.dr_type,
                                                    writable,
                                                    self.getCB, self.setCB,
                                                    self.getCB, self.setCB,
                                                    self.getCB, self.setCB,
                                                    self.rgetCB, self.rsetCB,
                                                    self.rgetCB, self.rsetCB,
                                                    self.rgetCB, self.rsetCB,
                                                    0, 0)

            self.__class__.datarefs.append(self)

            # Local shortcut
            self.set = self.set_f
            self.get = self.get_f

            # Init default value
            if self.isarray:
                self.value_f = [self.cast(0)] * self.index
                self.set = self.rset_f
            else:
                self.value_f = self.cast(0)

        else:
            self.DataRef = XPLMFindDataRef(dataref)
            if self.DataRef == False:
                print("Can't find " + dataref + " DataRef")

    def initArrayDref(self, first, last, type):
        if self.register:
            self.index = 0
            self.count = int(first)
        else:
            self.index = int(first)
            self.count = int(last) - int(first) + 1
            self.last = int(last)

        if (type == "int"):
            self.rget = XPLMGetDatavi
            self.rset = XPLMSetDatavi
            self.dr_type = xplmType_IntArray
            self.cast = int
        elif (type == "float"):
            self.rget = XPLMGetDatavf
            self.rset = XPLMSetDatavf
            self.dr_type = xplmType_FloatArray
            self.cast = float
        elif (type in ("bit", "data")):
            self.rget = XPLMGetDatab
            self.rset = XPLMSetDatab
            self.dr_type = xplmType_Data
            self.cast = int
        else:
            print("ERROR: invalid DataRef type: {}".format(type))
        pass

    def set(self, value):
        if self.isarray:
            self.rset(self.DataRef, value, self.index, len(value))
        else:
            self.dr_set(self.DataRef, self.cast(value))

    def get(self):
        if (self.isarray):
            list = []
            self.rget(self.DataRef, list, self.index, self.count)
            return list
        else:
            return self.dr_get(self.DataRef)

    # Local shortcuts
    def set_f(self, value):
        self.value_f = value

    def get_f(self):
        if self.isarray:
            vals = []
            for item in self.value_f:
                vals.append(self.cast(item))
            return vals
        else:
            return self.value_f

    def rset_f(self, value):

        vlen = len(value)
        if vlen < self.count:
            self.value_f = [self.cast(0)] * self.count
            self.value_f = value + self.value[vlen:]
        else:
            self.value_f = value

    # Data access SDK Callbacks
    def set_cb(self, inRefcon, value):
        self.value_f = value

    def get_cb(self, inRefcon):
        return self.cast(self.value_f)

    def rget_cb(self, inRefcon, values, index, limit):
        if values == None:
            return self.count
        else:
            i = 0
            for item in self.value_f:
                if i < limit:
                    values.append(self.cast(item))
                    i += 1
                else:
                    break
            return i

    def rset_cb(self, inRefcon, values, index, count):
        if self.count >= index + count:
            self.value_f = self.value_f[:index] + values + self.value_f[index + count:]
        else:
            return False

    def __getattr__(self, name):
        if name == 'value':
            return self.get()
        else:
            raise AttributeError

    def __setattr__(self, name, value):
        if name == 'value':
            self.set(value)
        else:
            self.__dict__[name] = value

    @classmethod
    def cleanup(cls):
        for dataref in cls.datarefs:
            XPLMUnregisterDataAccessor(dataref.DataRef)

    @classmethod
    def DataRefEditorRegister(cls):
        MSG_ADD_DATAREF = 0x01000000
        PluginID = XPLMFindPluginBySignature("xplanesdk.examples.DataRefEditor")

        drefs = 0
        if PluginID != XPLM_NO_PLUGIN_ID:
            for dataref in cls.datarefs:
                XPLMSendMessageToPlugin(PluginID, MSG_ADD_DATAREF, dataref.dataref)
                drefs += 1

        return drefs


class EasyCommand:
    '''
    Creates a command with an assigned callback with arguments
    '''

    def __init__(self, plugin, command, function, args=False, description=''):
        command = 'xjpc/XPNoaaWeather/' + command
        self.command = XPLMCreateCommand(command, description)
        self.commandCH = self.commandCHandler
        XPLMRegisterCommandHandler(self.command, self.commandCH, 1, 0)

        self.function = function
        self.args = args
        self.plugin = plugin
        # Command handlers

    def commandCHandler(self, inCommand, inPhase, inRefcon):
        if inPhase == 0:
            if self.args:
                if type(self.args).__name__ == 'tuple':
                    self.function(*self.args)
                else:
                    self.function(self.args)
            else:
                self.function()
        return 0

    def destroy(self):
        XPLMUnregisterCommandHandler(self.command, self.commandCH, 1, 0)


def text2data(text) -> list:
    xp.log(f' ** text2data: {text} | type: {type(text)}')
    if not isinstance(text, (dict, str)):
        xp.log(f' ** text2data: exiting empty ...')
        return []
    elif isinstance(text, dict):
        text = json.dumps(text)
    val = bytearray()
    try:
        val.extend(map(ord, text))
        xp.log(f' ** text2data: {val}')
    except (TypeError, ValueError) as e:
        xp.log(f"text2data ERROR: {e}")
    return list(val)


def data2text(data) -> str:
    xp.log(f' ** data2text: {data} | type: {type(data)}')
    try:
        text = bytearray([e for e in data if e]).decode('utf-8')
        xp.log(f' ** data2text: {text}')
        return text
    except (TypeError, ValueError) as e:
        xp.log(f"data2text ERROR: {e}")
    return ''


def data2dict(data: list) -> dict:
    xp.log(f' ** data2dict: {data} | type: {type(data)}')
    if len(data) and isinstance(data, list):
        try:
            text = bytearray([e for e in data if e]).decode('utf-8')
            parsed = json.loads(text)
            xp.log(f' ** data2dict: {parsed}')
            return parsed
        except (TypeError, ValueError) as e:
            xp.log(f"data2dict ERROR: {e}")
    return {}


class Dref:

    def __init__(self) -> None:
        self._on_ground = find_dataref('sim/flightmodel2/gear/on_ground')
        self._avionics = find_dataref('sim/cockpit/electrical/avionics_on')
        # there's an issue with create_dataref and string type in version 4.5.0
        self._send_queue = EasyDref('hoppiebridge/send_queue[255]', 'data', register=True, writable=True)
        self._poll_queue = EasyDref('hoppiebridge/poll_queue[255]', 'data', register=True, writable=True)
        self._callsign = EasyDref('hoppiebridge/callsign[15]', 'data', register=True, writable=True)

    @property
    def callsign(self) -> str:
        """Get the callsign"""
        xp.log(f'  * callsign: {self._callsign.value}')
        xp.log(f"  * callsign to str: {bytearray([int(x) for x in self._callsign.value if x]).decode('utf-8')}")
        val = bytearray([int(x) for x in self._callsign.value if x]).decode('utf-8')
        if not val:
            try:
                val = bytearray()
                val.extend(map(ord, 'TEST'))
                self._callsign.value = list(val)
            except (ValueError, TypeError) as e:
                xp.log(f'Dref.callsign ERROR: {e}')
        return val

    @property
    def inbox(self) -> dict:
        """Handle incoming messages from Hoppie's ACARS"""
        xp.log(f'  * inbox: {self._poll_queue.value}')
        return data2dict(self._poll_queue.value)

    @property
    def outbox(self) -> dict:
        """Get the outbox messages"""
        xp.log(f'  * outbox: {self._send_queue.value}')
        return data2dict(self._send_queue.value)

    @property
    def avionics_on(self) -> bool:
        """Check if avionics are on"""
        return self._avionics.value == 1

    def _get(self, dref) -> str:
        """Get the value of a dataref"""
        try:
            # return dref.value
            val = XPLMGetDatab(dref)
            return bytearray(val).decode('utf-8').strip('\x00')
        except (TypeError, ValueError) as e:
            xp.log(f"ERROR: {e}")
            return ''
        except SystemError as e:
            xp.log(f"SYSTEM ERROR: {e}")
            return ''

    def _set(self, dref, value: str) -> bool:
        try:
            XPLMSetDatab(dref, value.encode('utf-8'), 0, len(value))
        except (TypeError, ValueError) as e:
            xp.log(f"ERROR: {e}")
            return ''
        except SystemError as e:
            xp.log(f"SYSTEM ERROR: {e}")
            return ''

    def add_to_outbox(self, message: dict) -> bool:
        """Add a message to the send queue"""
        if message:
            xp.log(f' ** message to outbox: {message} | type: {type(message)}')
            data = text2data(message)
            self._send_queue.value = data
            return True
        return False

    def add_to_inbox(self, message: dict) -> bool:
        """Add a message to the receive queue"""
        xp.log(f' ** message to inbox: {message} | type: {type(message)}')
        if message:
            val = text2data(message)
            self._poll_queue.value = val
            return True
        return False

    def clear_received(self) -> bool:
        """Clear received messages from Hoppie's ACARS"""
        try:
            self._poll_queue.value = list(bytearray())
            return True
        except (TypeError, ValueError) as e:
            xp.log(f"clear_received ERROR: {e}")
        return False

    def clear_send(self) -> bool:
        """Clear sent messages to Hoppie's ACARS"""
        try:
            self._send_queue.value = list(bytearray())
            return True
        except (TypeError, ValueError) as e:
            xp.log(f"clear_send ERROR: {e}")
        return True


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

    def __init__(self, message: dict, poll_data: dict) -> None:
        # self.session = requests.Session()
        # self.session.headers.update({'User-Agent': 'HoppieBridge/1.0'})
        self.message = message
        self.poll_data = poll_data

    @staticmethod
    def run(message: dict = {}, poll_data: dict = {}) -> dict:
        """Run the connection to Hoppie's ACARS"""

        bridge = Bridge(message, poll_data)
        response = {}

        try:
            if message:
                response = bridge.query(message)
            elif poll_data:
                response = bridge.poll()
        except requests.RequestException as e:
            response = {'error': f"Connection error: {str(e)}"}
        return response

    def query(self, message: dict) -> dict:
        """query data to Hoppie Bridge"""
        if not isinstance(message, dict):
            # Ensure message is a dictionary
            return {'error': 'Message must be a dictionary'}

        try:
            # response = self.session.post(self.url, data=message, timeout=(15, 15))
            response = requests.post(self.url, data=message, timeout=(15, 15))
            if not response.status_code == 200:
                return {'error': f"Failed to send message: {response.status_code} {response.reason}"}
            elif not 'ok' in response.text.lower():
                return {'error': 'Message error: ' + response.text}
        except requests.Timeout:
            return {'error': "Timeout occurred while sending message"}
        except requests.RequestException as e:
            return {'error': f"Request error: {str(e)}"}
        return {'response': response.text}

    def poll(self) -> dict:
        """Poll data from Hoppie's ACARS"""
        try:
            response = requests.post(self.url, data=self.poll_data, timeout=(15, 15))
            if response.status_code != 200:
                return {'error': f"Failed to poll data: {response.status_code} {response.reason}"}
        except requests.Timeout:
            return {'error': "Timeout occurred while polling data"}
        except requests.RequestException as e:
            return {'error': f"Request error: {str(e)}"}
        return {'poll': response.text}


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
        # xp.log(f"check_info_line: {message}")
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
        self.dref = Dref()  # Dref instance

        # app init
        self.logon = ''  # logon string
        self.started = False  # started pre flight, to inhibit cold and dark
        self.last_poll_time = 0  # last poll time
        self.async_task = False
        self.send = False
        self.poll = False

        # load settings
        self.load_settings()

        # widget and windows
        self.details_message = "testing ..."  # text displayed in widget info_line
        self.message_content = []  # content of the messages widget
        self.create_monitor_window(100, 400)

        # create main menu and widget
        self.main_menu = self.create_main_menu()

        # testing
        self.outbox = ''  # outbox messages, for testing purposes

    @property
    def callsign(self) -> str:
        """Get the callsign from the dref"""
        if self.dref:
            return self.dref.callsign
        return ''

    @property
    def poll_data(self) -> dict:
        """Get the logon from the dref"""
        data = {
            'logon': self.logon,
            'from': self.callsign,
            'to': self.callsign,
            'type': 'poll'
        }
        return data

    @property
    def time_to_poll(self) -> bool:
        """Check if it's time to poll messages"""
        now = perf_counter()
        return now - self.last_poll_time >= POLL_SCHEDULE

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
        # self.monitor.top -= self.monitor.cr()

        # info message line
        self.monitor.add_info_line()
        # self.monitor.top -= self.monitor.cr()

        # # Test buttons sub window
        self.monitor.add_test_buttons_widget()
        # self.monitor.top -= self.monitor.cr()

        # Messages sub window
        self.monitor.add_content_widget(title='Messages:')

        self.monitor.setup_widget(self.logon)

        # Register our widget handler
        self.monitorWidgetHandlerCB = self.monitorWidgetHandler
        xp.addWidgetCallback(self.monitor.widget, self.monitorWidgetHandlerCB)

    def monitorWidgetHandler(self, inMessage, inWidget, inParam1, inParam2):
        if not self.monitor:
            return 1

        self.monitor.check_info_line(self.details_message)

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
            if inParam1 == self.monitor.reqinfo_button:
                # TEST - handle ReqInfo button
                # send a request for LIPE METAR
                xp.log('**** Requesting LIPE METAR ...')
                self.dref.add_to_outbox({
                    "to": "SERVER",
                    "type": "inforeq",
                    "packet": "METAR LIPE"
                })
                self.details_message = "Requesting LIPE METAR ..."
                return 1
            if inParam1 == self.monitor.telex_button:
                # TEST - handle Telex button
                # send a telex for LIPE ATIS
                xp.log('**** Sending LIPE ATIS Telex ...')
                self.dref.add_to_outbox({
                    "to": "SERVER",
                    "type": "telex",
                    "packet": "HC002 LIPE"
                })
                self.details_message = "Sending LIPE ATIS Telex ..."
                return 1
        return 0

    def format_message(self, data: dict) -> list:
        # create lines from D-ATIS string
        width = self.monitor.content_width
        # print(f"width: {width} | char: {FONT_WIDTH}")
        result = []
        for k, v in data.items():
            string = f"{k}: {v}"
            words = string.split(' ')
            result.append('-')
            for word in words:
                if xp.measureString(FONT, result[-1] + ' ' + word) < width:
                    result[-1] += word if not result[-1] else ' ' + word
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
            self.details_message = 'settings saved'
            self.monitor.setup_widget(self.logon)

    def loopCallback(self, lastCall, elapsedTime, counter, refCon):
        """Loop Callback"""
        t = datetime.now()
        start = perf_counter()
        # xp.log(f"avionics on: {self.dref.avionics_on} | loopCallback() started at {t.strftime('%H:%M:%S')}")
        loop_schedule = DEFAULT_SCHEDULE
        if self.logon and self.dref and self.dref.avionics_on:
            # check if we need to send or poll messages
            if self.async_task:
                if not self.async_task.pending:
                    # async task completed
                    self.async_task.join()
                    xp.log(f"Async task completed in {self.async_task.elapsed:.3f} sec")
                    result = self.async_task.result
                    if isinstance(result, Exception):
                        # log the error
                        self.details_message = "Connection task failed"
                        xp.log(f"Async task failed: {result}")
                    else:
                        # check if we have received messages
                        if result.get('error'):
                            self.details_message = f"Error: {result['error']}"
                            xp.log(f"Error: {result['error']}")
                        else:
                            # process received message
                            xp.log(f"Received message: {result}")
                            if self.dref.add_to_inbox(result):
                                xp.log(f"Message added to inbox")
                                self.details_message = "Message received"
                            else:
                                xp.log(f"Failed to add message to inbox: not empty")
                                xp.log(f"inbox: {self.dref.inbox}")
                                self.details_message = "Message received but inbox not empty"
                    self.async_task = False
                else:
                    self.details_message = "No new messages"
            else:
                # check if we need to poll and / or send messages
                message = ''
                if self.dref.outbox:
                    try:
                        # it's time to poll messages or we have messages to send
                        parsed = self.dref.outbox
                        if isinstance(parsed, dict):
                            # dref is a json-like string, convert to json
                            # dref: '{"to": "value", "type": "value", "packet": "value"}'
                            parsed['logon'] = self.logon
                            parsed['from'] = self.callsign
                            message = parsed
                            self.dref.clear_send()
                    except Exception as e:
                        xp.log(f"Invalid message format: {parsed} | Error: {e}")

                if message or self.time_to_poll:
                    # we have messages to send or it's time to poll
                    self.async_task = Async(
                        Bridge.run,
                        message=message,
                        poll_data=False if not self.time_to_poll else self.poll_data,
                    )
                    self.async_task.start()
                    xp.log(f"Async task started, sending {len(message)} messages and polling data: {self.time_to_poll}")

        else:
            # TEST MODE
            # check if we need to send or poll messages
            xp.log(f" *** TEST MODE loopCallback() ...")
            xp.log(f"   * callsign: {self.callsign}")
            xp.log(f"   * outbox: {self.outbox}")
            xp.log(f"   * time to poll: {self.time_to_poll}")
            if self.async_task:
                xp.log(f"  ** Async task {self.async_task.pid} existing")
                xp.log(f"   * Async task pending: {self.async_task.pending} | is_alive: {self.async_task.is_alive()}")
                if not self.async_task.pending:
                    # async task completed
                    self.async_task.join()
                    xp.log(f"  ** Async task completed in {self.async_task.elapsed:.3f} sec")
                    result = self.async_task.result
                    if isinstance(result, Exception):
                        # log the error
                        self.details_message = "Connection task failed"
                        xp.log(f" *** Async task failed: {result}")
                    else:
                        # check if we have received messages
                        if result.get('error'):
                            self.details_message = f"Error: {result['error']}"
                            xp.log(f" *** Error: {result['error']}")
                        else:
                            # process received message
                            xp.log(f" *** Received message: {result}")
                            self.details_message = "New message"
                            self.message_content = self.format_message(result)
                    self.async_task = False
                else:
                    xp.log(f"  ** Async job {self.async_task.pid} still pending, waiting ...")
                    self.details_message = "No new messages"
            else:
                # check if we need to poll and / or send messages
                xp.log(f"  ** No async task running, checking outbox and poll data ...")
                message = ''
                poll_data = ''
                if self.outbox:
                    try:
                        # it's time to poll messages or we have messages to send
                        xp.log(f"   * We have a message to send ...")
                        parsed = json.loads(self.outbox)
                        if isinstance(parsed, dict):
                            # dref is a json-like string, convert to json
                            # dref: '{"to": "value", "type": "value", "packet": "value"}'
                            parsed['logon'] = self.logon
                            parsed['from'] = self.callsign
                            message = parsed
                            self.outbox = ''  # clear outbox after sending
                    except Exception as e:
                        xp.log(f" *** Invalid message format, Error: {e}")
                elif self.time_to_poll:
                    # it's time to poll messages
                    xp.log(f"   * It's time to poll messages ...")
                    poll_data = self.poll_data
                    self.last_poll_time = perf_counter()

                if message or poll_data:
                    # we have messages to send or it's time to poll
                    xp.log(f"  ** starting a new job ...")
                    xp.log(f"   * message: {message}")
                    xp.log(f"   * poll_data: {poll_data}")
                    self.async_task = Async(
                        Bridge.run,
                        message=message,
                        poll_data=poll_data,
                    )
                    self.async_task.start()
                    self.details_message = "Async task started ..."
                    xp.log(f"Async task {self.async_task.pid} started, output: {message != ''} | polling data: {poll_data != ''}")

        xp.log(f" {t.strftime('%H:%M:%S')} - loopCallback() ended after {round(perf_counter() - start, 3)} sec | schedule = {loop_schedule} sec")
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
        xp.log("flightloop closed, exiting ...")

    def XPluginReceiveMessage(self, *args, **kwargs):
        pass
