"""
topology.py  -  builds the Mininet network and starts everything.

                      POX controller (dns_controller.py)
                                  |
                              s1 (core)
                             /         \\
                  s2 (clients)          s3 (servers)
                  /    |    \\         /    |    |    \\
                c1    c2    c3      ns1  ns2  ns3   ns4
             10.0.0.1  .2   .3      .11  .12  .13   .14
                                   (cn.lab, behind  (research.cn.lab)
                                    VIP 10.0.0.53)

Run:   sudo python3 topology.py                    (dynamic SDN, round robin)
       sudo python3 topology.py --policy static    (baseline)
"""

import argparse 
import os 
import subprocess
import sys
import time
from functools import partial

from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

HERE = os.path.dirname(os.path.abspath(__file__))
POX = os.path.expanduser("~/pox/pox.py")
if os.environ.get("SUDO_USER"):                       # with sudo, ~ is /root: use the real user's home
    POX = os.path.expanduser("~%s/pox/pox.py" % os.environ["SUDO_USER"])
POX = os.environ.get("POX", POX)


def start_controller(policy): #POX is Plainold XML
    """Start POX with our controller in the background. Its log goes to controller.log."""
    log = open(os.path.join(HERE, "controller.log"), "w")
    env = dict(os.environ, PYTHONPATH=HERE)
    proc = subprocess.Popen([sys.executable, POX, "log.level", "--INFO",
                             "dns_controller", "--policy=" + policy],
                            stdout=log, stderr=subprocess.STDOUT, env=env)
    time.sleep(2)                                     # give POX time to start
    return proc


def build_network(userspace=False):
    """Create the switches, hosts and links."""
    switch = partial(OVSSwitch, protocols="OpenFlow10",
                     datapath="user" if userspace else "kernel")
    net = Mininet(switch=switch, controller=None, build=False)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6633)

    s1 = net.addSwitch("s1")      # core switch
    s2 = net.addSwitch("s2")      # switch for the clients
    s3 = net.addSwitch("s3")      # switch for the servers
    net.addLink(s1, s2)
    net.addLink(s1, s3)

    for i in (1, 2, 3):           # clients c1, c2, c3
        host = net.addHost("c%d" % i, ip="10.0.0.%d/24" % i, mac="00:00:00:00:00:0%d" % i)
        net.addLink(host, s2)
    for i in (1, 2, 3, 4):        # servers ns1..ns4 = 10.0.0.11..14
        host = net.addHost("ns%d" % i, ip="10.0.0.1%d/24" % i, mac="00:00:00:00:00:1%d" % i)
        net.addLink(host, s3)
    return net


def start_servers(net, extra_args=""):
    """Start the DNS server program inside each server host."""
    replicas = ["10.0.0.11", "10.0.0.12", "10.0.0.13"]
    for i in (1, 2, 3):
        me = "10.0.0.1%d" % i
        peers = ",".join(ip for ip in replicas if ip != me)
        net["ns%d" % i].cmd("cd %s && python3 dns_server.py ns%d records.json --heartbeat "
                            "--peers %s %s > ns%d.log 2>&1 &" % (HERE, i, peers, extra_args, i))
    net["ns4"].cmd("cd %s && python3 dns_server.py ns4 research_records.json > ns4.log 2>&1 &" % HERE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", default="round_robin", choices=["round_robin", "static"])
    parser.add_argument("--userspace", action="store_true",
                        help="only for machines without the Open vSwitch kernel module (e.g. WSL)")
    args = parser.parse_args()

    os.chdir(HERE)                                    # so the hosts can find our .py files
    setLogLevel("info")
    pox = start_controller(args.policy)
    net = build_network(args.userspace)
    try:
        net.start()
        if args.userspace:                            # needed by the userspace datapath
            for host in net.hosts:
                host.cmd("ethtool -K %s-eth0 tx off" % host.name)
        start_servers(net)
        time.sleep(3)                                 # wait for the first heartbeats
        print("""
Try these at the mininet> prompt:
  c1 python3 dns_client.py www.cn.lab mail.cn.lab portal.cn.lab nope.cn.lab
  c1 python3 dns_client.py db.cn.lab --repeat 3              (caching)
  c2 python3 dns_client.py lab1.research.cn.lab              (redirect to ns4)
  c3 python3 dns_client.py --bench 5 --no-cache              (load balancing + latency)
  sh ovs-ofctl dump-flows s2                                 (the controller's rules)
  ns1 pkill -f "dns_server.py ns1"                           (crash ns1)
  link s3 ns2 down                                           (cut ns2's cable)
  sh tail controller.log                                     (what the controller saw)
""")
        CLI(net)
    finally:
        os.system("pkill -f dns_server.py")
        net.stop()
        pox.terminate()


if __name__ == "__main__":
    main()
