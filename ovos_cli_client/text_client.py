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
import sys
import time
from mycroft_bus_client import Message, MessageBusClient
from ovos_plugin_manager.templates.tts import TTS
from ovos_utils.log import LOG


class SimpleCli:
    bus = None

    @classmethod
    def bind(cls, bus=None):
        if not bus:
            bus = MessageBusClient()
            bus.run_in_thread()
        cls.bus = bus

    @classmethod
    def handle_speak(cls, message):
        utterance = message.data.get('utterance')
        utterance = TTS.remove_ssml(utterance)
        print(">> " + utterance)

    @classmethod
    def run(cls):
        if not cls.bus:
            cls.bind()

        cls.bus.on('speak', cls.handle_speak)
        try:
            while True:
                # Sleep for a while so all the output that results
                # from the previous command finishes before we print.
                time.sleep(1.5)
                print("Input (Ctrl+C to quit):")
                line = sys.stdin.readline()
                cls.bus.emit(Message("recognizer_loop:utterance",
                                 {'utterances': [line.strip()]},
                                 {'client_name': 'mycroft_simple_cli',
                                  'source': 'debug_cli',
                                  'destination': ["skills"]}))
        except KeyboardInterrupt as e:
            # User hit Ctrl+C to quit
            print("")
            sys.exit()
        except Exception as e:
            LOG.exception(e)
            sys.exit()


def launch_simple_cli(bus):
    SimpleCli.bind(bus)
    SimpleCli.run()
