#==================================================================================

#        Copyright (c) 2018-2019 AT&T Intellectual Property.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#==================================================================================

#
# Author : Ashwin Sridharan
#   Date    : Feb 2019
#


# This initialization script reads in a json from the specified config map path
# to set up the initializations (route config map, variables etc) for the main
# xapp process

import ast
import json
import os
import signal
import sys
import time

SCRIPT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SCRIPT_ROOT not in sys.path:
    sys.path.insert(0, SCRIPT_ROOT)

from xapp_bs_connector.init.register_xapp import register_xapp
from xapp_bs_connector.init.xapp_utils import set_target_gnb


def signal_handler(signum, frame):
    print("Received signal {0}\n".format(signum))
    if(xapp_subprocess == None or xapp_pid == None):
        print("No xapp running. Quiting without sending signal to xapp\n")
    else:
        print("Sending signal {0} to xapp ...".format(signum))
        xapp_subprocess.send_signal(signum)


def getMessagingInfo(config):
     if 'messaging' in config.keys() and 'ports' in config['messaging'].keys():
        port_list = config['messaging']['ports']
        for portdesc in port_list :
            if 'port' in portdesc.keys() and 'name' in portdesc.keys() and portdesc['name'] == 'rmr-data':
                lport = portdesc['port']
                # Set the environment variable
                os.environ["HW_PORT"] = str(lport)
                return True
     if lport == 0:
         print("Error! No valid listening port")
         return False


def getXappName(config):
    myKey = "xapp_name"
    if myKey not in config.keys():
        print(("Error ! No information found for {0} in config\n".format(myKey)))
        return False

    xapp_name = config[myKey]
    print("Xapp Name is: " + xapp_name) 
    os.environ["XAPP_NAME"] = xapp_name


if __name__ == "__main__":

    import subprocess
    cmd = ["/usr/local/bin/hw_xapp_main"]

    debug_enabled = False
    args = sys.argv[1:]
    if "--debug" in args:
        debug_enabled = True
        args.remove("--debug")

    if len(args) > 0:
        config_file = args[0]
    else:
        print("Error! No configuration file specified\n")
        sys.exit(1)

    if len(args) > 1:
        cmd[0] = args[1]

    with open(config_file, 'r') as f:
         try:
             config = json.load(f)
         except Exception as e:
             print(("Error loading json file from {0}. Reason = {1}\n".format(config_file, e)))
             sys.exit(1)

    # register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # this needs to be done before the xApp has messages to receive,
    # otherwise they will stop at the e2term 
    print("Registering xApp with app manager")
    register_xapp(config)

    print('Checking target gNB')
    set_target_gnb(config)

    # Start the xApp
    if debug_enabled:
        os.environ["XAPP_VERBOSE_DEBUG"] = "1"
        os.environ["XAPP_LOG_LEVEL"] = "DEBUG"
        os.environ["RMR_VLEVEL"] = "2"
    else:
        os.environ["XAPP_VERBOSE_DEBUG"] = "0"
        os.environ["XAPP_LOG_LEVEL"] = "ERROR"
        os.environ["RMR_VLEVEL"] = "0"

    print("Executing the xApp...");
    xapp_subprocess = subprocess.Popen(cmd, shell=False, stdin=None, stdout=None, stderr = None)
    xapp_pid = xapp_subprocess.pid

    # Periodically poll the process every 5 seconds to check if still alive
    while(1):
        xapp_status = xapp_subprocess.poll()
        if xapp_status == None:
            time.sleep(5)
        else:
            print("xApp terminated via signal {0}\n".format(-1 * xapp_status))
            break
