# Copyright 2017 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import curses
import io
import json
import locale
import os
import os.path
import signal
import textwrap
from os.path import isfile, exists
from threading import Thread, Lock

import time
from math import ceil
from mycroft_bus_client import Message, MessageBusClient
from ovos_config.config import get_xdg_config_locations, get_xdg_config_save_path, Configuration
from ovos_config.meta import get_xdg_base
from ovos_plugin_manager.templates.tts import TTS
from ovos_utils.signal import get_ipc_directory
from ovos_utils.xdg_utils import xdg_state_home

from ovos_cli_client.gui_server import start_qml_gui


##############################################################################
# Log file monitoring

class LogMonitorThread(Thread):
    log_lock = Lock()
    max_log_lines = 5000
    mergedLog = []
    filteredLog = []
    default_log_filters = ["mouth.viseme", "mouth.display", "mouth.icon"]
    log_filters = list(default_log_filters)
    log_files = []
    find_str = None

    def __init__(self, filename, logid):
        Thread.__init__(self, daemon=True)
        self.filename = filename
        self.st_results = os.stat(filename)
        self.logid = str(logid)
        self.log_files.append(filename)

    def run(self):
        while True:
            try:
                st_results = os.stat(self.filename)

                # Check if file has been modified since last read
                if not st_results.st_mtime == self.st_results.st_mtime:
                    self.read_file_from(self.st_results.st_size)
                    self.st_results = st_results

                    ScreenDrawThread.set_screen_dirty()
            except OSError:
                # ignore any file IO exceptions, just try again
                pass
            time.sleep(0.1)

    def read_file_from(self, bytefrom):
        with io.open(self.filename) as fh:
            fh.seek(bytefrom)
            while True:
                line = fh.readline()
                if line == "":
                    break

                # Allow user to filter log output
                ignore = False
                if self.find_str:
                    if self.find_str not in line:
                        ignore = True
                else:
                    for filtered_text in LogMonitorThread.log_filters:
                        if filtered_text in line:
                            ignore = True
                            break

                with self.log_lock:
                    if ignore:
                        self.mergedLog.append(self.logid + line.rstrip())
                    else:
                        self.filteredLog.append(self.logid + line.rstrip())
                        self.mergedLog.append(self.logid + line.rstrip())
                        if not ScreenDrawThread.auto_scroll:
                            ScreenDrawThread.log_line_offset += 1

        # Limit log to  max_log_lines
        if len(self.mergedLog) >= self.max_log_lines:
            with self.log_lock:
                cToDel = len(self.mergedLog) - self.max_log_lines
                if len(self.filteredLog) == len(self.mergedLog):
                    del self.filteredLog[:cToDel]
                del self.mergedLog[:cToDel]

            # release log_lock before calling to prevent deadlock
            if len(self.filteredLog) != len(self.mergedLog):
                self.rebuild_filtered_log()

    @classmethod
    def add_log_message(cls, message):
        """ Show a message for the user (mixed in the logs) """
        with cls.log_lock:
            message = "@" + message  # the first byte is a code
            cls.filteredLog.append(message)
            cls.mergedLog.append(message)

            if ScreenDrawThread.log_line_offset != 0:
                ScreenDrawThread.log_line_offset = 0  # scroll so the user can see the message
        ScreenDrawThread.set_screen_dirty()

    @classmethod
    def clear_log(cls):
        with cls.log_lock:
            cls.mergedLog = []
            cls.filteredLog = []
            ScreenDrawThread.log_line_offset = 0

    @classmethod
    def rebuild_filtered_log(cls):
        with cls.log_lock:
            cls.filteredLog = []
            for line in cls.mergedLog:
                # Apply filters
                ignore = False

                if cls.find_str and cls.find_str != "":
                    # Searching log
                    if cls.find_str not in line:
                        ignore = True
                else:
                    # Apply filters
                    for filtered_text in cls.log_filters:
                        if filtered_text and filtered_text in line:
                            ignore = True
                            break

                if not ignore:
                    cls.filteredLog.append(line)


def start_log_monitor(filename):
    if os.path.isfile(filename):
        thread = LogMonitorThread(filename, len(LogMonitorThread.log_files))
        thread.start()


class MicMonitorThread(Thread):
    meter_cur = -1
    meter_thresh = -1

    def __init__(self, filename):
        Thread.__init__(self, daemon=True)
        self.filename = filename
        self.st_results = None

    def run(self):
        while True:
            try:
                st_results = os.stat(self.filename)

                if (not self.st_results or
                        not st_results.st_ctime == self.st_results.st_ctime or
                        not st_results.st_mtime == self.st_results.st_mtime):
                    self.read_mic_level()
                    self.st_results = st_results
                    ScreenDrawThread.set_screen_dirty()
            except Exception:
                # Ignore whatever failure happened and just try again later
                pass
            time.sleep(0.2)

    def read_mic_level(self):
        with io.open(self.filename, 'r') as fh:
            line = fh.readline()
            # Just adjust meter settings
            # Ex:Energy:  cur=4 thresh=1.5 muted=0
            cur_text, thresh_text, _ = line.split(' ')[-3:]
            self.meter_thresh = float(thresh_text.split('=')[-1])
            self.meter_cur = float(cur_text.split('=')[-1])


class ScreenDrawThread(Thread):
    scr = None
    SCR_MAIN = 0
    SCR_HELP = 1
    SCR_SKILLS = 2
    screen_mode = SCR_MAIN

    subscreen = 0  # for help pages, etc.
    REDRAW_FREQUENCY = 10  # seconds between full redraws
    last_redraw = time.time() - (REDRAW_FREQUENCY - 1)  # seed for 1s redraw
    screen_lock = Lock()
    is_screen_dirty = True

    line = ""
    size_log_area = 0  # max number of visible log lines, calculated during draw
    log_line_offset = 0  # num lines back in logs to show
    log_line_lr_scroll = 0  # amount to scroll left/right for long lines
    longest_visible_line = 0  # for HOME key
    auto_scroll = True

    # for debugging odd terminals
    last_key = ""
    show_gui = None  # None = not initialized, else True/False
    tui_text = []

    # Curses color codes (reassigned at runtime)
    CLR_HEADING = 0
    CLR_FIND = 0
    CLR_CHAT_RESP = 0
    CLR_CHAT_QUERY = 0
    CLR_CMDLINE = 0
    CLR_INPUT = 0
    CLR_LOG1 = 0
    CLR_LOG2 = 0
    CLR_LOG_DEBUG = 0
    CLR_LOG_ERROR = 0
    CLR_LOG_CMDMESSAGE = 0
    CLR_METER_CUR = 0
    CLR_METER = 0

    def __init__(self):
        Thread.__init__(self, daemon=True)

    ##############################################################################
    # Helper functions
    @staticmethod
    def clamp(n, smallest, largest):
        """ Force n to be between smallest and largest, inclusive """
        return max(smallest, min(n, largest))

    @staticmethod
    def handleNonAscii(text):
        """
            If default locale supports UTF-8 reencode the string otherwise
            remove the offending characters.
        """
        if TUI.preferred_encoding == 'ASCII':
            return ''.join([i if ord(i) < 128 else ' ' for i in text])
        else:
            return text.encode(TUI.preferred_encoding)

    @staticmethod
    def center(str_len):
        # generate number of characters needed to center a string
        # of the given length
        return " " * ((curses.COLS - str_len) // 2)

    ##############################################################################
    # Screen handling
    @classmethod
    def init_screen(cls):
        if curses.has_colors():
            curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)
            bg = curses.COLOR_BLACK
            for i in range(1, curses.COLORS):
                curses.init_pair(i + 1, i, bg)

            # Colors (on black backgound):
            # 1 = white         5 = dk blue
            # 2 = dk red        6 = dk purple
            # 3 = dk green      7 = dk cyan
            # 4 = dk yellow     8 = lt gray
            cls.CLR_HEADING = curses.color_pair(1)
            cls.CLR_CHAT_RESP = curses.color_pair(4)
            cls.CLR_CHAT_QUERY = curses.color_pair(7)
            cls.CLR_FIND = curses.color_pair(4)
            cls.CLR_CMDLINE = curses.color_pair(7)
            cls.CLR_INPUT = curses.color_pair(7)
            cls.CLR_LOG1 = curses.color_pair(3)
            cls.CLR_LOG2 = curses.color_pair(6)
            cls.CLR_LOG_DEBUG = curses.color_pair(4)
            cls.CLR_LOG_ERROR = curses.color_pair(2)
            cls.CLR_LOG_CMDMESSAGE = curses.color_pair(2)
            cls.CLR_METER_CUR = curses.color_pair(2)
            cls.CLR_METER = curses.color_pair(4)

    @classmethod
    def scroll_log(cls, up, num_lines=None):
        # default to a half-page
        if not num_lines:
            num_lines = cls.size_log_area // 2

        with LogMonitorThread.log_lock:
            if up:
                cls.log_line_offset -= num_lines
            else:
                cls.log_line_offset += num_lines
            if cls.log_line_offset > len(LogMonitorThread.filteredLog):
                cls.log_line_offset = len(LogMonitorThread.filteredLog) - 10
            if cls.log_line_offset < 0:
                cls.log_line_offset = 0
        cls.set_screen_dirty()

    @classmethod
    def _do_meter(cls, height):
        if not TUI.show_meter or MicMonitorThread.meter_cur == -1:
            return

        # The meter will look something like this:
        #
        # 8.4   *
        #       *
        #      -*- 2.4
        #       *
        #       *
        #       *
        # Where the left side is the current level and the right side is
        # the threshold level for 'silence'.

        if MicMonitorThread.meter_cur > MicMonitorThread.meter_peak:
            MicMonitorThread.meter_peak = MicMonitorThread.meter_cur + 1

        scale = MicMonitorThread.meter_peak
        if MicMonitorThread.meter_peak > MicMonitorThread.meter_thresh * 3:
            scale = MicMonitorThread.meter_thresh * 3
        h_cur = cls.clamp(int((float(MicMonitorThread.meter_cur) / scale) * height), 0, height - 1)
        h_thresh = cls.clamp(
            int((float(MicMonitorThread.meter_thresh) / scale) * height), 0, height - 1)
        clr = curses.color_pair(4)  # dark yellow

        str_level = "{0:3} ".format(int(MicMonitorThread.meter_cur))  # e.g. '  4'
        str_thresh = "{0:4.2f}".format(MicMonitorThread.meter_thresh)  # e.g. '3.24'
        meter_width = len(str_level) + len(str_thresh) + 4
        for i in range(0, height):
            meter = ""
            if i == h_cur:
                # current energy level
                meter = str_level
            else:
                meter = " " * len(str_level)

            if i == h_thresh:
                # add threshold indicator
                meter += "--- "
            else:
                meter += "    "

            if i == h_thresh:
                # 'silence' threshold energy level
                meter += str_thresh

            # draw the line
            meter += " " * (meter_width - len(meter))
            cls.scr.addstr(curses.LINES - 1 - i, curses.COLS -
                           len(meter) - 1, meter, clr)

            # draw an asterisk if the audio energy is at this level
            if i <= h_cur:
                if MicMonitorThread.meter_cur > MicMonitorThread.meter_thresh:
                    clr_bar = curses.color_pair(3)  # dark green for loud
                else:
                    clr_bar = curses.color_pair(5)  # dark blue for 'silent'
                cls.scr.addstr(curses.LINES - 1 - i, curses.COLS - len(str_thresh) - 4,
                               "*", clr_bar)

    @classmethod
    def _do_gui(cls, tui_width):
        clr = curses.color_pair(2)  # dark red
        x = curses.COLS - tui_width
        y = 3
        cls.draw(
            x,
            y,
            " " +
            cls.make_titlebar(
                "= GUI",
                tui_width -
                1) +
            " ",
            clr=cls.CLR_HEADING)
        cnt = len(cls.tui_text) + 1
        if cnt > curses.LINES - 15:
            cnt = curses.LINES - 15
        for i in range(0, cnt):
            cls.draw(x, y + 1 + i, " !", clr=cls.CLR_HEADING)
            if i < len(cls.tui_text):
                cls.draw(x + 2, y + 1 + i, cls.tui_text[i], pad=tui_width - 3)
            else:
                cls.draw(x + 2, y + 1 + i, "*" * (tui_width - 3))
            cls.draw(x + (tui_width - 1), y + 1 + i, "!", clr=cls.CLR_HEADING)
        cls.draw(x, y + cnt, " " + "-" * (tui_width - 2) + " ", clr=cls.CLR_HEADING)

    @classmethod
    def do_draw_main(cls):
        if time.time() - cls.last_redraw > cls.REDRAW_FREQUENCY:
            # Do a full-screen redraw periodically to clear and
            # noise from non-curses text that get output to the
            # screen (e.g. modules that do a 'print')
            cls.scr.clear()
            cls.last_redraw = time.time()
        else:
            cls.scr.erase()

        # Display log output at the top
        cLogs = len(LogMonitorThread.filteredLog) + 1  # +1 for the '--end--'
        size_log_area = curses.LINES - (TUI.cy_chat_area + 5)
        start = cls.clamp(cLogs - size_log_area, 0, cLogs - 1) - cls.log_line_offset
        end = cLogs - cls.log_line_offset
        if start < 0:
            end -= start
            start = 0
        if end > cLogs:
            end = cLogs

        cls.auto_scroll = (end == cLogs)

        # adjust the line offset (prevents paging up too far)
        cls.log_line_offset = cLogs - end

        # Top header and line counts
        if LogMonitorThread.find_str:
            cls.scr.addstr(0, 0, "Search Results: ", cls.CLR_HEADING)
            cls.scr.addstr(0, 16, LogMonitorThread.find_str, cls.CLR_FIND)
            cls.scr.addstr(0, 16 + len(LogMonitorThread.find_str), " ctrl+X to end" +
                           " " * (curses.COLS - 31 - 12 - len(LogMonitorThread.find_str)) +
                           str(start) + "-" + str(end) + " of " + str(cLogs),
                           cls.CLR_HEADING)
        else:
            cls.scr.addstr(0, 0, "Log Output:" + " " * (curses.COLS - 31) +
                           str(start) + "-" + str(end) + " of " + str(cLogs),
                           cls.CLR_HEADING)
        ver = " ovos-core        ==="
        cls.scr.addstr(1, 0, "=" * (curses.COLS - 1 - len(ver)), cls.CLR_HEADING)
        cls.scr.addstr(1, curses.COLS - 1 - len(ver), ver, cls.CLR_HEADING)

        y = 2
        for i in range(start, end):
            if i >= cLogs - 1:
                log = '   ^--- NEWEST ---^ '
            else:
                log = LogMonitorThread.filteredLog[i]
            logid = log[0]
            if len(log) > 25 and log[5] == '-' and log[8] == '-':
                log = log[11:]  # skip logid & date at the front of log line
            else:
                log = log[1:]  # just skip the logid

            # Categorize log line
            if "| DEBUG    |" in log:
                log = log.replace("Skills ", "")
                clr = cls.CLR_LOG_DEBUG
            elif "| ERROR    |" in log:
                clr = cls.CLR_LOG_ERROR
            else:
                if logid == "1":
                    clr = cls.CLR_LOG1
                elif logid == "@":
                    clr = cls.CLR_LOG_CMDMESSAGE
                else:
                    clr = cls.CLR_LOG2

            # limit output line to screen width
            len_line = len(log)
            if len(log) > curses.COLS:
                start = len_line - (curses.COLS - 4) - cls.log_line_lr_scroll
                if start < 0:
                    start = 0
                end = start + (curses.COLS - 4)
                if start == 0:
                    log = log[start:end] + "~~~~"  # start....
                elif end >= len_line - 1:
                    log = "~~~~" + log[start:end]  # ....end
                else:
                    log = "~~" + log[start:end] + "~~"  # ..middle..
            if len_line > cls.longest_visible_line:
                cls.longest_visible_line = len_line
            cls.scr.addstr(y, 0, cls.handleNonAscii(log), clr)
            y += 1

        # Log legend in the lower-right
        y_log_legend = curses.LINES - (3 + TUI.cy_chat_area)
        cls.scr.addstr(y_log_legend, curses.COLS // 2 + 2,
                       cls.make_titlebar("Log Output Legend", curses.COLS // 2 - 2),
                       cls.CLR_HEADING)
        cls.scr.addstr(y_log_legend + 1, curses.COLS // 2 + 2,
                       "DEBUG output",
                       cls.CLR_LOG_DEBUG)
        if len(LogMonitorThread.log_files) > 0:
            cls.scr.addstr(y_log_legend + 2, curses.COLS // 2 + 2,
                           os.path.basename(LogMonitorThread.log_files[0]) + ", other",
                           cls.CLR_LOG2)
        if len(LogMonitorThread.log_files) > 1:
            cls.scr.addstr(y_log_legend + 3, curses.COLS // 2 + 2,
                           os.path.basename(LogMonitorThread.log_files[1]), cls.CLR_LOG1)

        # Meter
        y_meter = y_log_legend
        if TUI.show_meter:
            cls.scr.addstr(y_meter, curses.COLS - 14, " Mic Level ",
                           cls.CLR_HEADING)

        # History log in the middle
        y_chat_history = curses.LINES - (3 + TUI.cy_chat_area)
        chat_width = curses.COLS // 2 - 2
        chat_out = []
        cls.scr.addstr(y_chat_history, 0, cls.make_titlebar("History", chat_width),
                       cls.CLR_HEADING)

        # Build a nicely wrapped version of the chat log
        idx_chat = len(TUI.chat) - 1
        while len(chat_out) < TUI.cy_chat_area and idx_chat >= 0:
            if TUI.chat[idx_chat][0] == '>':
                wrapper = textwrap.TextWrapper(initial_indent="",
                                               subsequent_indent="   ",
                                               width=chat_width)
            else:
                wrapper = textwrap.TextWrapper(width=chat_width)

            chatlines = wrapper.wrap(TUI.chat[idx_chat])
            for txt in reversed(chatlines):
                if len(chat_out) >= TUI.cy_chat_area:
                    break
                chat_out.insert(0, txt)

            idx_chat -= 1

        # Output the chat
        y = curses.LINES - (2 + TUI.cy_chat_area)
        for txt in chat_out:
            if txt.startswith(">> ") or txt.startswith("   "):
                clr = cls.CLR_CHAT_RESP
            else:
                clr = cls.CLR_CHAT_QUERY
            cls.scr.addstr(y, 1, cls.handleNonAscii(txt), clr)
            y += 1

        if cls.show_gui and curses.COLS > 20 and curses.LINES > 20:
            cls._do_gui(curses.COLS - 20)

        # Command line at the bottom
        ln = cls.line
        if len(cls.line) > 0 and cls.line[0] == ":":
            cls.scr.addstr(curses.LINES - 2, 0, "Command ('help' for options):",
                           cls.CLR_CMDLINE)
            cls.scr.addstr(curses.LINES - 1, 0, ":", cls.CLR_CMDLINE)
            ln = cls.line[1:]
        else:
            prompt = "Input (':' for command, Ctrl+C to quit)"
            if TUI.show_last_key:
                prompt += " === keycode: " + cls.last_key
            cls.scr.addstr(curses.LINES - 2, 0,
                           cls.make_titlebar(prompt,
                                             curses.COLS - 1),
                           cls.CLR_HEADING)
            cls.scr.addstr(curses.LINES - 1, 0, ">", cls.CLR_HEADING)

        cls._do_meter(TUI.cy_chat_area + 2)
        cls.scr.addstr(curses.LINES - 1, 2, ln[-(curses.COLS - 3):], cls.CLR_INPUT)

        # Curses doesn't actually update the display until refresh() is called
        cls.scr.refresh()

    ##############################################################################
    # "Graphic primitives"
    @classmethod
    def draw(cls, x, y, msg, pad=None, pad_chr=None, clr=None):
        """Draw a text to the screen

        Args:
            x (int): X coordinate (col), 0-based from upper-left
            y (int): Y coordinate (row), 0-based from upper-left
            msg (str): string to render to screen
            pad (bool or int, optional): if int, pads/clips to given length, if
                                         True use right edge of the screen.
            pad_chr (char, optional): pad character, default is space
            clr (int, optional): curses color, Defaults to CLR_LOG1.
        """
        if y < 0 or y > curses.LINES or x < 0 or x > curses.COLS:
            return

        if x + len(msg) > curses.COLS:
            s = msg[:curses.COLS - x]
        else:
            s = msg
            if pad:
                ch = pad_chr or " "
                if pad is True:
                    pad = curses.COLS  # pad to edge of screen
                    s += ch * (pad - x - len(msg))
                else:
                    # pad to given length (or screen width)
                    if x + pad > curses.COLS:
                        pad = curses.COLS - x
                    s += ch * (pad - len(msg))

        if not clr:
            clr = cls.CLR_LOG1

        cls.scr.addstr(y, x, s, clr)

    @staticmethod
    def make_titlebar(title, bar_length):
        return title + " " + ("=" * (bar_length - 1 - len(title)))

    @classmethod
    def set_screen_dirty(cls):
        with cls.screen_lock:
            cls.is_screen_dirty = True

    ##############################################################################
    # Help system
    help_struct = [
        ('Log Scrolling shortcuts',
         [("Up / Down / PgUp / PgDn",
           "scroll thru history"),
          ("Ctrl+T / Ctrl+PgUp",
           "scroll to top of logs (jump to oldest)"),
          ("Ctrl+B / Ctrl+PgDn",
           "scroll to bottom of logs" + "(jump to newest)"),
          ("Left / Right",
           "scroll long lines left/right"),
          ("Home / End",
           "scroll to start/end of long lines")]),
        ("Query History shortcuts",
         [("Ctrl+N / Ctrl+Left",
           "previous query"),
          ("Ctrl+P / Ctrl+Right",
           "next query")]),
        ("General Commands (type ':' to enter command mode)",
         [(":quit or :exit",
           "exit the program"),
          (":meter (show|hide)",
           "display the microphone level"),
          (":keycode (show|hide)",
           "display typed key codes (mainly debugging)"),
          (":history (# lines)",
           "set size of visible history buffer"),
          (":clear",
           "flush the logs")]),
        ("Log Manipulation Commands",
         [(":filter 'STR'",
           "adds a log filter (optional quotes)"),
          (":filter remove 'STR'",
           "removes a log filter"),
          (":filter (clear|reset)",
           "reset filters"),
          (":filter (show|list)",
           "display current filters"),
          (":find 'STR'",
           "show logs containing 'str'"),
          (":log level (DEBUG|INFO|ERROR)",
           "set logging level"),
          (":log bus (on|off)",
           "control logging of messagebus messages")]),
        ("Skill Debugging Commands",
         [(":skills",
           "list installed Skills"),
          (":api SKILL",
           "show Skill's public API"),
          (":activate SKILL",
           "activate Skill, e.g. 'activate skill-wiki'"),
          (":deactivate SKILL",
           "deactivate Skill"),
          (":keep SKILL",
           "deactivate all Skills except the indicated Skill")])]
    help_longest = 0
    for s in help_struct:
        for ent in s[1]:
            help_longest = max(help_longest, len(ent[0]))

    HEADER_SIZE = 2
    HEADER_FOOTER_SIZE = 4

    @classmethod
    def show_help(cls):
        if cls.screen_mode != cls.SCR_HELP:
            cls.screen_mode = cls.SCR_HELP
            cls.subscreen = 0
            cls.set_screen_dirty()

    @classmethod
    def num_help_pages(cls):
        lines = 0
        for section in cls.help_struct:
            lines += 3 + len(section[1])
        return ceil(lines / (curses.LINES - cls.HEADER_FOOTER_SIZE))

    @classmethod
    def do_draw_help(cls):

        def render_header():
            cls.scr.addstr(0, 0, cls.center(25) + "Mycroft Command Line Help", cls.CLR_HEADING)
            cls.scr.addstr(1, 0, "=" * (curses.COLS - 1), cls.CLR_HEADING)

        def render_help(txt, y_pos, i, first_line, last_line, clr):
            if i >= first_line and i < last_line:
                cls.scr.addstr(y_pos, 0, txt, clr)
                y_pos += 1
            return y_pos

        def render_footer(page, total):
            text = "Page {} of {} [ Any key to continue ]".format(page, total)
            cls.scr.addstr(curses.LINES - 1, 0, cls.center(len(text)) + text, cls.CLR_HEADING)

        cls.scr.erase()
        render_header()
        y = cls.HEADER_SIZE
        page = cls.subscreen + 1

        # Find first and last taking into account the header and footer
        first = cls.subscreen * (curses.LINES - cls.HEADER_FOOTER_SIZE)
        last = first + (curses.LINES - cls.HEADER_FOOTER_SIZE)
        i = 0
        for section in cls.help_struct:
            y = render_help(section[0], y, i, first, last, cls.CLR_HEADING)
            i += 1
            y = render_help("=" * (curses.COLS - 1), y, i, first, last,
                            cls.CLR_HEADING)
            i += 1

            for line in section[1]:
                words = line[1].split()
                ln = line[0].ljust(cls.help_longest + 1)
                for w in words:
                    if len(ln) + 1 + len(w) < curses.COLS:
                        ln += " " + w
                    else:
                        y = render_help(ln, y, i, first, last, cls.CLR_CMDLINE)
                        ln = " ".ljust(cls.help_longest + 2) + w
                y = render_help(ln, y, i, first, last, cls.CLR_CMDLINE)
                i += 1

            y = render_help(" ", y, i, first, last, cls.CLR_CMDLINE)
            i += 1

            if i > last:
                break

        render_footer(page, cls.num_help_pages())

        # Curses doesn't actually update the display until refresh() is called
        cls.scr.refresh()

    @classmethod
    def show_next_help(cls):
        if cls.screen_mode == cls.SCR_HELP:
            cls.subscreen += 1
            if cls.subscreen >= cls.num_help_pages():
                cls.screen_mode = cls.SCR_MAIN
            cls.set_screen_dirty()

    def run(self):
        while self.scr:
            try:
                if self.is_screen_dirty:
                    # Use a lock to prevent screen corruption when drawing
                    # from multiple threads
                    with self.screen_lock:
                        self.is_screen_dirty = False

                        if self.screen_mode == self.SCR_MAIN:
                            with LogMonitorThread.log_lock:
                                self.do_draw_main()
                        elif self.screen_mode == self.SCR_HELP:
                            self.do_draw_help()

            finally:
                time.sleep(0.01)


def start_mic_monitor(filename):
    if os.path.isfile(filename):
        thread = MicMonitorThread(filename)
        thread.start()


class TUI:
    # Curses uses LC_ALL to determine how to display chars set it to system
    # default
    locale.setlocale(locale.LC_ALL, "")  # Set LC_ALL to user default
    preferred_encoding = locale.getpreferredencoding()

    bus = None  # Mycroft messagebus connection
    config = Configuration()
    config_file = None  # mycroft_cli.conf
    event_thread = None
    history = []
    chat = []  # chat history, oldest at the lowest index

    show_meter = True  # Values used to display the audio meter
    cy_chat_area = 10  # default chat history height (in lines)
    show_last_key = False

    # Allow Ctrl+C catching...
    ctrl_c_was_pressed = False

    @classmethod
    def bind(cls, bus=None):
        if not bus:
            bus = MessageBusClient()
            bus.run_in_thread()
        cls.bus = bus

    @classmethod
    def ctrl_c_handler(cls, signum, frame):
        cls.ctrl_c_was_pressed = True

    @classmethod
    def ctrl_c_pressed(cls):
        if cls.ctrl_c_was_pressed:
            cls.ctrl_c_was_pressed = False
            return True
        else:
            return False

    ##############################################################################
    # Settings

    filename = "mycroft_cli.conf"

    @classmethod
    def load_settings(cls):
        # Old location
        path = os.path.join(os.path.expanduser("~"), f".{cls.filename}")

        if os.path.isfile(path):
            cls.config_file = path

        # Check XDG_CONFIG_DIR
        if cls.config_file is None:
            for conf_dir in get_xdg_config_locations():
                xdg_file = os.path.join(conf_dir, cls.filename)
                if os.path.isfile(xdg_file):
                    cls.config_file = xdg_file
                    break

        # Check /etc/mycroft
        if cls.config_file is None:  # TODO ovos_config xdg
            cls.config_file = os.path.join("/etc/mycroft", cls.filename)

        if not isfile(cls.config_file):
            cls.config_file = os.path.join(get_xdg_config_save_path(), cls.filename)

        try:
            with io.open(cls.config_file, 'r') as f:
                cls.config = json.load(f)
            if "filters" in cls.config:
                # Disregard the filtering of DEBUG messages
                LogMonitorThread.log_filters = [f for f in cls.config["filters"] if f != "DEBUG"]
            if "cy_chat_area" in cls.config:
                cls.cy_chat_area = cls.config["cy_chat_area"]
            if "show_last_key" in cls.config:
                cls.show_last_key = cls.config["show_last_key"]
            if "max_log_lines" in cls.config:
                LogMonitorThread.max_log_lines = cls.config["max_log_lines"]
            if "show_meter" in cls.config:
                cls.show_meter = cls.config["show_meter"]
        except Exception as e:
            LogMonitorThread.add_log_message("Ignoring failed load of settings file")

    @classmethod
    def save_settings(cls):
        config = {}
        config["filters"] = LogMonitorThread.log_filters
        config["cy_chat_area"] = cls.cy_chat_area
        config["show_last_key"] = cls.show_last_key
        config["max_log_lines"] = LogMonitorThread.max_log_lines
        config["show_meter"] = cls.show_meter

        with io.open(cls.config_file, 'w') as f:
            f.write(str(json.dumps(config, ensure_ascii=False)))

    ##############################################################################
    # Capturing output from Mycroft
    @classmethod
    def handle_speak(cls, event):
        utterance = event.data.get('utterance')
        utterance = TTS.remove_ssml(utterance)
        cls.chat.append(">> " + utterance)
        ScreenDrawThread.set_screen_dirty()

    @classmethod
    def handle_utterance(cls, event):
        utterance = event.data.get('utterances')[0]
        cls.history.append(utterance)
        cls.chat.append(utterance)
        ScreenDrawThread.set_screen_dirty()

    ##############################################################################
    # Skill debugging
    @classmethod
    def show_skills(cls, skills):
        """Show list of loaded Skills in as many column as necessary."""
        ScreenDrawThread.screen_mode = ScreenDrawThread.SCR_SKILLS

        row = 2
        column = 0

        def prepare_page():
            nonlocal row
            nonlocal column
            ScreenDrawThread.scr.erase()
            ScreenDrawThread.scr.addstr(0, 0, ScreenDrawThread.center(25) + "Loaded Skills",
                                        ScreenDrawThread.CLR_CMDLINE)
            ScreenDrawThread.scr.addstr(1, 1, "=" * (curses.COLS - 2), ScreenDrawThread.CLR_CMDLINE)
            row = 2
            column = 0

        prepare_page()
        col_width = 0
        skill_names = sorted(skills.keys())
        for skill in skill_names:
            if skills[skill]['active']:
                color = curses.color_pair(4)
            else:
                color = curses.color_pair(2)

            ScreenDrawThread.scr.addstr(row, column, "  {}".format(skill), color)
            row += 1
            col_width = max(col_width, len(skill))
            if row == curses.LINES - 2 and column > 0 and skill != skill_names[-1]:
                column = 0
                ScreenDrawThread.scr.addstr(curses.LINES - 1, 0,
                                            ScreenDrawThread.center(23) + "Press any key to continue",
                                            ScreenDrawThread.CLR_HEADING)
                ScreenDrawThread.scr.refresh()
                cls.wait_for_any_key()
                prepare_page()
            elif row == curses.LINES - 2:
                # Reached bottom of screen, start at top and move output to a
                # New column
                row = 2
                column += col_width + 2
                col_width = 0
                if column > curses.COLS - 20:
                    # End of screen
                    break

        ScreenDrawThread.scr.addstr(curses.LINES - 1, 0, ScreenDrawThread.center(23) + "Press any key to return",
                                    ScreenDrawThread.CLR_HEADING)
        ScreenDrawThread.scr.refresh()

    @classmethod
    def show_skill_api(cls, skill, data):
        """Show available help on Skill's API."""

        ScreenDrawThread.screen_mode = ScreenDrawThread.SCR_SKILLS

        row = 2
        column = 0

        def prepare_page():
            nonlocal row
            nonlocal column
            ScreenDrawThread.scr.erase()
            ScreenDrawThread.scr.addstr(0, 0, ScreenDrawThread.center(25) + "Skill-API for {}".format(skill),
                                        ScreenDrawThread.CLR_CMDLINE)
            ScreenDrawThread.scr.addstr(1, 1, "=" * (curses.COLS - 2), ScreenDrawThread.CLR_CMDLINE)
            row = 2
            column = 4

        prepare_page()
        for key in data:
            color = curses.color_pair(4)

            ScreenDrawThread.scr.addstr(row, column, "{} ({})".format(key, data[key]['type']),
                                        ScreenDrawThread.CLR_HEADING)
            row += 2
            if 'help' in data[key]:
                help_text = data[key]['help'].split('\n')
                for line in help_text:
                    ScreenDrawThread.scr.addstr(row, column + 2, line, color)
                    row += 1
                row += 2
            else:
                row += 1

            if row == curses.LINES - 5:
                ScreenDrawThread.scr.addstr(curses.LINES - 1, 0,
                                            ScreenDrawThread.center(23) + "Press any key to continue",
                                            ScreenDrawThread.CLR_HEADING)
                ScreenDrawThread.scr.refresh()
                cls.wait_for_any_key()
                prepare_page()
            elif row == curses.LINES - 5:
                # Reached bottom of screen, start at top and move output to a
                # New column
                row = 2

        ScreenDrawThread.scr.addstr(curses.LINES - 1, 0, ScreenDrawThread.center(23) + "Press any key to return",
                                    ScreenDrawThread.CLR_HEADING)
        ScreenDrawThread.scr.refresh()

    ##############################################################################
    # Main UI lopo
    @staticmethod
    def _get_cmd_param(cmd, keyword):
        # Returns parameter to a command.  Will de-quote.
        # Ex: find 'abc def'   returns: abc def
        #    find abc def     returns: abc def
        if isinstance(keyword, list):
            for w in keyword:
                cmd = cmd.replace(w, "").strip()
        else:
            cmd = cmd.replace(keyword, "").strip()
        if not cmd:
            return None

        last_char = cmd[-1]
        if last_char == '"' or last_char == "'":
            parts = cmd.split(last_char)
            return parts[-2]
        else:
            parts = cmd.split(" ")
            return parts[-1]

    @classmethod
    def wait_for_any_key(cls):
        """Block until key is pressed.

        This works around curses.error that can occur on old versions of ncurses.
        """
        while True:
            try:
                ScreenDrawThread.scr.get_wch()  # blocks
            except curses.error:
                # Loop if get_wch throws error
                time.sleep(0.05)
            else:
                break

    @classmethod
    def handle_cmd(cls, cmd):
        if "show" in cmd and "log" in cmd:
            pass
        elif "help" in cmd:
            ScreenDrawThread.show_help()
        elif "exit" in cmd or "quit" in cmd:
            return 1
        elif "keycode" in cmd:
            # debugging keyboard
            if "hide" in cmd or "off" in cmd:
                cls.show_last_key = False
            elif "show" in cmd or "on" in cmd:
                cls.show_last_key = True
        elif "meter" in cmd:
            # microphone level meter
            if "hide" in cmd or "off" in cmd:
                cls.show_meter = False
            elif "show" in cmd or "on" in cmd:
                cls.show_meter = True
        elif "find" in cmd:
            LogMonitorThread.find_str = cls._get_cmd_param(cmd, "find")
            LogMonitorThread.rebuild_filtered_log()
        elif "filter" in cmd:
            if "show" in cmd or "list" in cmd:
                # display active filters
                LogMonitorThread.add_log_message("Filters: " + str(LogMonitorThread.log_filters))
                return

            if "reset" in cmd or "clear" in cmd:
                LogMonitorThread.log_filters = list(LogMonitorThread.default_log_filters)
            else:
                # extract last word(s)
                param = cls._get_cmd_param(cmd, "filter")
                if param:
                    if "remove" in cmd and param in LogMonitorThread.log_filters:
                        LogMonitorThread.log_filters.remove(param)
                    else:
                        LogMonitorThread.log_filters.append(param)

            LogMonitorThread.rebuild_filtered_log()
            LogMonitorThread.add_log_message("Filters: " + str(LogMonitorThread.log_filters))
        elif "clear" in cmd:
            LogMonitorThread.clear_log()
        elif "log" in cmd:
            # Control logging behavior in all Mycroft processes
            if "level" in cmd:
                level = cls._get_cmd_param(cmd, ["log", "level"])
                cls.bus.emit(Message("mycroft.debug.log", data={'level': level}))
            elif "bus" in cmd:
                state = cls._get_cmd_param(cmd, ["log", "bus"]).lower()
                if state in ["on", "true", "yes"]:
                    cls.bus.emit(Message("mycroft.debug.log", data={'bus': True}))
                elif state in ["off", "false", "no"]:
                    cls.bus.emit(Message("mycroft.debug.log", data={'bus': False}))
        elif "history" in cmd:
            # extract last word(s)
            lines = int(cls._get_cmd_param(cmd, "history"))
            if not lines or lines < 1:
                lines = 1
            max_chat_area = curses.LINES - 7
            if lines > max_chat_area:
                lines = max_chat_area
            cy_chat_area = lines
        elif "skills" in cmd:
            # List loaded skill
            message = cls.bus.wait_for_response(
                Message('skillmanager.list'), reply_type='mycroft.skills.list')

            if message:
                cls.show_skills(message.data)
                cls.wait_for_any_key()

                ScreenDrawThread.screen_mode = ScreenDrawThread.SCR_MAIN
                ScreenDrawThread.set_screen_dirty()
        elif "deactivate" in cmd:
            skills = cmd.split()[1:]
            if len(skills) > 0:
                for s in skills:
                    cls.bus.emit(Message("skillmanager.deactivate", data={'skill': s}))
            else:
                LogMonitorThread.add_log_message('Usage :deactivate SKILL [SKILL2] [...]')
        elif "keep" in cmd:
            s = cmd.split()
            if len(s) > 1:
                cls.bus.emit(Message("skillmanager.keep", data={'skill': s[1]}))
            else:
                LogMonitorThread.add_log_message('Usage :keep SKILL')

        elif "activate" in cmd:
            skills = cmd.split()[1:]
            if len(skills) > 0:
                for s in skills:
                    cls.bus.emit(Message("skillmanager.activate", data={'skill': s}))
            else:
                LogMonitorThread.add_log_message('Usage :activate SKILL [SKILL2] [...]')
        elif "api" in cmd:
            parts = cmd.split()
            if len(parts) < 2:
                return
            skill = parts[1]
            message = cls.bus.wait_for_response(Message('{}.public_api'.format(skill)))
            if message:
                cls.show_skill_api(skill, message.data)
                ScreenDrawThread.scr.get_wch()  # blocks
                ScreenDrawThread.screen_mode = ScreenDrawThread.SCR_MAIN
                ScreenDrawThread.set_screen_dirty()

        # TODO: More commands
        return 0  # do nothing upon return

    @staticmethod
    def handle_is_connected(msg):
        LogMonitorThread.add_log_message("Connected to Messagebus!")

    @staticmethod
    def handle_reconnecting():
        LogMonitorThread.add_log_message("Looking for Messagebus websocket...")

    @classmethod
    def run(cls, stdscr):
        screen = ScreenDrawThread()
        ScreenDrawThread.scr = stdscr
        ScreenDrawThread.scr.keypad(1)
        ScreenDrawThread.scr.notimeout(True)
        screen.init_screen()
        screen.start()

        cls.bus.on('speak', cls.handle_speak)
        cls.bus.on('recognizer_loop:utterance', cls.handle_utterance)
        cls.bus.on('connected', cls.handle_is_connected)
        cls.bus.on('reconnecting', cls.handle_reconnecting)

        LogMonitorThread.add_log_message("Establishing Mycroft Messagebus connection...")

        hist_idx = -1  # index, from the bottom
        c = 0
        try:
            while True:
                ScreenDrawThread.set_screen_dirty()
                c = 0
                code = 0

                try:
                    if cls.ctrl_c_pressed():
                        # User hit Ctrl+C. treat same as Ctrl+X
                        c = 24
                    else:
                        # Don't block, this allows us to refresh the screen while
                        # waiting on initial messagebus connection, etc
                        ScreenDrawThread.scr.timeout(1)
                        c = ScreenDrawThread.scr.get_wch()  # unicode char or int for special keys
                        if c == -1:
                            continue
                except curses.error:
                    # This happens in odd cases, such as when you Ctrl+Z
                    # the CLI and then resume.  Curses fails on get_wch().
                    continue

                if isinstance(c, int):
                    code = c
                else:
                    code = ord(c)

                # Convert VT100 ESC codes generated by some terminals
                if code == 27:
                    # NOTE:  Not sure exactly why, but the screen can get corrupted
                    # if we draw to the screen while doing a scr.getch().  So
                    # lock screen updates until the VT100 sequence has been
                    # completely read.
                    with ScreenDrawThread.screen_lock:
                        ScreenDrawThread.scr.timeout(0)
                        c1 = -1
                        start = time.time()
                        while c1 == -1:
                            c1 = ScreenDrawThread.scr.getch()
                            if time.time() - start > 1:
                                break  # 1 second timeout waiting for ESC code

                        c2 = -1
                        while c2 == -1:
                            c2 = ScreenDrawThread.scr.getch()
                            if time.time() - start > 1:  # 1 second timeout
                                break  # 1 second timeout waiting for ESC code

                    if c1 == 79 and c2 == 120:
                        c = curses.KEY_UP
                    elif c1 == 79 and c2 == 116:
                        c = curses.KEY_LEFT
                    elif c1 == 79 and c2 == 114:
                        c = curses.KEY_DOWN
                    elif c1 == 79 and c2 == 118:
                        c = curses.KEY_RIGHT
                    elif c1 == 79 and c2 == 121:
                        c = curses.KEY_PPAGE  # aka PgUp
                    elif c1 == 79 and c2 == 115:
                        c = curses.KEY_NPAGE  # aka PgDn
                    elif c1 == 79 and c2 == 119:
                        c = curses.KEY_HOME
                    elif c1 == 79 and c2 == 113:
                        c = curses.KEY_END
                    else:
                        c = c1

                    if c1 != -1:
                        cls.last_key = str(c) + ",ESC+" + str(c1) + "+" + str(c2)
                        code = c
                    else:
                        cls.last_key = "ESC"
                else:
                    if code < 33:
                        cls.last_key = str(code)
                    else:
                        cls.last_key = str(code)

                ScreenDrawThread.scr.timeout(-1)  # resume blocking
                if code == 27:  # Hitting ESC twice clears the entry line
                    hist_idx = -1
                    ScreenDrawThread.line = ""
                elif c == curses.KEY_RESIZE:
                    # Generated by Curses when window/screen has been resized
                    y, x = ScreenDrawThread.scr.getmaxyx()
                    curses.resizeterm(y, x)

                    # resizeterm() causes another curses.KEY_RESIZE, so
                    # we need to capture that to prevent a loop of resizes
                    c = ScreenDrawThread.scr.get_wch()
                elif ScreenDrawThread.screen_mode == ScreenDrawThread.SCR_HELP:
                    # in Help mode, any key goes to next page
                    ScreenDrawThread.show_next_help()
                    continue
                elif c == '\n' or code == 10 or code == 13 or code == 343:
                    # ENTER sends the typed line to be processed by Mycroft
                    if ScreenDrawThread.line == "":
                        continue

                    if ScreenDrawThread.line[:1] == ":":
                        # Lines typed like ":help" are 'commands'
                        if cls.handle_cmd(ScreenDrawThread.line[1:]) == 1:
                            break
                    else:
                        # Treat this as an utterance
                        cls.bus.emit(Message("recognizer_loop:utterance",
                                             {'utterances': [ScreenDrawThread.line.strip()],
                                              'lang': cls.config.get('lang', 'en-us')},
                                             {'client_name': 'mycroft_cli',
                                              'source': 'debug_cli',
                                              'destination': ["skills"]}
                                             ))
                    hist_idx = -1
                    ScreenDrawThread.line = ""
                elif code == 16 or code == 545:  # Ctrl+P or Ctrl+Left (Previous)
                    # Move up the history stack
                    hist_idx = ScreenDrawThread.clamp(hist_idx + 1, -1, len(cls.history) - 1)
                    if hist_idx >= 0:
                        ScreenDrawThread.line = cls.history[len(cls.history) - hist_idx - 1]
                    else:
                        ScreenDrawThread.line = ""
                elif code == 14 or code == 560:  # Ctrl+N or Ctrl+Right (Next)
                    # Move down the history stack
                    hist_idx = ScreenDrawThread.clamp(hist_idx - 1, -1, len(cls.history) - 1)
                    if hist_idx >= 0:
                        ScreenDrawThread.line = cls.history[len(cls.history) - hist_idx - 1]
                    else:
                        ScreenDrawThread.line = ""
                elif c == curses.KEY_LEFT:
                    # scroll long log lines left
                    ScreenDrawThread.log_line_lr_scroll += curses.COLS // 4
                elif c == curses.KEY_RIGHT:
                    # scroll long log lines right
                    ScreenDrawThread.log_line_lr_scroll -= curses.COLS // 4
                    if ScreenDrawThread.log_line_lr_scroll < 0:
                        ScreenDrawThread.log_line_lr_scroll = 0
                elif c == curses.KEY_HOME:
                    # HOME scrolls log lines all the way to the start
                    ScreenDrawThread.log_line_lr_scroll = ScreenDrawThread.longest_visible_line
                elif c == curses.KEY_END:
                    # END scrolls log lines all the way to the end
                    ScreenDrawThread.log_line_lr_scroll = 0
                elif c == curses.KEY_UP:
                    ScreenDrawThread.scroll_log(False, 1)
                elif c == curses.KEY_DOWN:
                    ScreenDrawThread.scroll_log(True, 1)
                elif c == curses.KEY_NPAGE:  # aka PgDn
                    # PgDn to go down a page in the logs
                    ScreenDrawThread.scroll_log(True)
                elif c == curses.KEY_PPAGE:  # aka PgUp
                    # PgUp to go up a page in the logs
                    ScreenDrawThread.scroll_log(False)
                elif code == 2 or code == 550:  # Ctrl+B or Ctrl+PgDn
                    ScreenDrawThread.scroll_log(True, LogMonitorThread.max_log_lines)
                elif code == 20 or code == 555:  # Ctrl+T or Ctrl+PgUp
                    ScreenDrawThread.scroll_log(False, LogMonitorThread.max_log_lines)
                elif code == curses.KEY_BACKSPACE or code == 127:
                    # Backspace to erase a character in the utterance
                    ScreenDrawThread.line = ScreenDrawThread.line[:-1]
                elif code == 6:  # Ctrl+F (Find)
                    ScreenDrawThread.line = ":find "
                elif code == 7:  # Ctrl+G (start GUI)
                    if cls.show_gui is None:
                        start_qml_gui(cls.bus, ScreenDrawThread.tui_text)
                    cls.show_gui = not cls.show_gui
                elif code == 18:  # Ctrl+R (Redraw)
                    ScreenDrawThread.scr.erase()
                elif code == 24:  # Ctrl+X (Exit)
                    if LogMonitorThread.find_str:
                        # End the find session
                        LogMonitorThread.find_str = None
                        LogMonitorThread.rebuild_filtered_log()
                    elif ScreenDrawThread.line.startswith(":"):
                        # cancel command mode
                        ScreenDrawThread.line = ""
                    else:
                        # exit CLI
                        break
                elif code > 31 and isinstance(c, str):
                    # Accept typed character in the utterance
                    ScreenDrawThread.line += c

        finally:
            ScreenDrawThread.scr.erase()
            ScreenDrawThread.scr.refresh()
            ScreenDrawThread.scr = None


def launch_curses_tui(bus):
    TUI.bind(bus)

    # Monitor system logs
    config = Configuration()

    legacy_path = "/var/log/mycroft"

    if 'log_dir' not in config:
        config["log_dir"] = f"{xdg_state_home()}/{get_xdg_base()}"

    log_dir = os.path.expanduser(config['log_dir'])
    for f in os.listdir(log_dir):
        if not f.endswith(".log"):
            continue
        start_log_monitor(os.path.join(log_dir, f))

    # also monitor legacy path for compat
    if log_dir != legacy_path and exists(legacy_path):
        LogMonitorThread.add_log_message(
            f"this installation seems to also contain logs in the legacy directory {legacy_path}, "
            f"please start using {log_dir}")
        for f in os.listdir(legacy_path):
            if not f.endswith(".log"):
                continue
            start_log_monitor(os.path.join(legacy_path, f))

    # Monitor IPC file containing microphone level info
    start_mic_monitor(os.path.join(get_ipc_directory(), "mic_level"))

    # Special signal handler allows a clean shutdown of the GUI
    signal.signal(signal.SIGINT, TUI.ctrl_c_handler)
    TUI.load_settings()
    curses.wrapper(TUI.run)
    curses.endwin()
    TUI.save_settings()
