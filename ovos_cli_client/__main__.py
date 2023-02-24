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

import io
import sys
from mycroft_bus_client import MessageBusClient

from ovos_cli_client.curses_client import launch_curses_tui
from ovos_cli_client.text_client import launch_simple_cli


def main():
    simple = '--simple' in sys.argv
    bus = MessageBusClient()
    bus.run_in_thread()

    if simple:
        launch_simple_cli(bus)
    else:

        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()

        def custom_except_hook(exctype, value, traceback):
            print(sys.stdout.getvalue(), file=sys.__stdout__)
            print(sys.stderr.getvalue(), file=sys.__stderr__)
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            sys.__excepthook__(exctype, value, traceback)

        sys.excepthook = custom_except_hook  # noqa

        launch_curses_tui(bus)


if __name__ == "__main__":
    main()
