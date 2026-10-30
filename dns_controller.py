"""
dns_controller.py  -  a simple SDN controller (POX, OpenFlow 1.0) for the DNS project.

Idea
  * Clients send every DNS query to ONE virtual IP: 10.0.0.53 (no real host has it).
  * When the first packet of a query reaches a switch, the switch has no rule for it,
    so it sends the packet to this controller ("packet-in").
  * The controller picks a real server (ns1 / ns2 / ns3) and installs two rules:
        forward rule : client -> 10.0.0.53   becomes   client -> chosen server
        reverse rule : server -> client      becomes   10.0.0.53 -> client
  * Servers send a heartbeat every 0.5 s. No heartbeat for 1.5 s  =>  server is DEAD.
    A cable going down (port-status message) also marks the server DEAD at once.
  * When a server dies, its rules are deleted, so the next queries go to live servers.

Policies (choose with --policy=...)
  round_robin : live servers in turn (our dynamic SDN approach)
  static      : always ns1, never reacts to failures (the baseline we compare with)

Run (from the pox folder):
  PYTHONPATH=~/sdn-dns-simple python3 pox.py log.level --INFO dns_controller --policy=round_robin
"""

import logging
import time

from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.addresses import EthAddr, IPAddr
from pox.lib.packet.arp import arp
from pox.lib.packet.ethernet import ethernet
from pox.lib.recoco import Timer

log = core.getLogger("dns")

VIP = IPAddr("10.0.0.53")                      # the virtual DNS server address
VIP_MAC = EthAddr("00:00:00:00:00:53")
HEARTBEAT_IP = IPAddr("10.0.0.254")            # servers send heartbeats to this fake address
HEARTBEAT_MAC = EthAddr("00:00:00:00:00:fe")
DEAD_AFTER = 1.5                               # seconds without a heartbeat => dead

# The three replicated DNS servers: IP address -> MAC address (set in topology.py)
SERVERS = {
    IPAddr("10.0.0.11"): EthAddr("00:00:00:00:00:11"),
    IPAddr("10.0.0.12"): EthAddr("00:00:00:00:00:12"),
    IPAddr("10.0.0.13"): EthAddr("00:00:00:00:00:13"),
}


class DNSController(object):

    def __init__(self, policy):
        self.policy = policy
        self.mac_to_port = {}                               # switch -> {MAC: port}
        self.last_heartbeat = {ip: 0 for ip in SERVERS}     # server -> time of last heartbeat
        self.alive = {ip: False for ip in SERVERS}          # server -> True / False
        self.next_index = 0                                 # for round robin
        core.openflow.addListeners(self)                    # receive OpenFlow events
        Timer(0.5, self.check_servers, recurring=True)      # health check every 0.5 s
        log.info("DNS controller started, policy = %s", policy)

    # ------------------------------------------------------------ OpenFlow events
    def _handle_ConnectionUp(self, event):
        log.info("switch s%s connected", event.dpid)

    def _handle_PacketIn(self, event):
        """Called for every packet that did not match any rule in a switch."""
        try:
            packet = event.parsed
        except Exception:            # POX cannot parse some packets on new Python: ignore them
            return
        if not packet.parsed:
            return

        # Learn which port each MAC address is behind (normal "learning switch")
        if packet.src not in (VIP_MAC, HEARTBEAT_MAC):
            self.mac_to_port.setdefault(event.dpid, {})[packet.src] = event.port

        arp_pkt = packet.find("arp")
        ip_pkt = packet.find("ipv4")
        udp_pkt = packet.find("udp")

        # 1) Someone asks "who has 10.0.0.53 / 10.0.0.254?" -> we answer (proxy ARP)
        if arp_pkt and arp_pkt.opcode == arp.REQUEST and arp_pkt.protodst in (VIP, HEARTBEAT_IP):
            self.answer_arp(event, arp_pkt)
        # 2) A server's heartbeat -> remember the time
        elif ip_pkt and ip_pkt.dstip == HEARTBEAT_IP:
            self.got_heartbeat(event, ip_pkt)
        # 3) A DNS query to the virtual IP -> choose a server and install rules
        elif ip_pkt and udp_pkt and ip_pkt.dstip == VIP and udp_pkt.dstport == 53:
            self.send_to_server(event, ip_pkt, udp_pkt)
        # 4) Anything else -> behave like a normal switch
        else:
            self.forward_normally(event, packet)

    def _handle_PortStatus(self, event):
        """A switch tells us a port went up or down (e.g. a cable was cut)."""
        port = event.ofp.desc.port_no
        is_down = bool(event.ofp.desc.state & of.OFPPS_LINK_DOWN) or event.deleted
        if not is_down:
            return
        log.warning("port %s on switch s%s went DOWN", port, event.dpid)
        table = self.mac_to_port.get(event.dpid, {})
        for ip, mac in SERVERS.items():
            if table.get(mac) == port:                   # this server was behind that port
                self.last_heartbeat[ip] = 0
                self.mark_dead(ip, "link down")
        for mac in [m for m, p in table.items() if p == port]:
            del table[mac]

    # ------------------------------------------------------------ DNS load balancing
    def choose_server(self):
        """Pick the server for a new query."""
        if self.policy == "static":
            return IPAddr("10.0.0.11")                   # baseline: always ns1
        live = [ip for ip in SERVERS if self.alive[ip]]
        if not live:
            return None
        server = live[self.next_index % len(live)]       # round robin
        self.next_index += 1
        return server

    def send_to_server(self, event, ip_pkt, udp_pkt):
        server_ip = self.choose_server()
        if server_ip is None:
            log.warning("no live DNS server - query dropped")
            return
        server_mac = SERVERS[server_ip]
        client_ip, client_port = ip_pkt.srcip, udp_pkt.srcport
        out_port = self.mac_to_port.get(event.dpid, {}).get(server_mac, of.OFPP_FLOOD)

        # Reverse rule first: replies from the server look like they come from 10.0.0.53
        reverse = of.ofp_flow_mod(priority=200, idle_timeout=5)
        reverse.match = of.ofp_match(dl_type=0x0800, nw_proto=17,
                                     nw_src=server_ip, tp_src=53,
                                     nw_dst=client_ip, tp_dst=client_port)
        reverse.actions = [of.ofp_action_nw_addr.set_src(VIP),
                           of.ofp_action_dl_addr.set_src(VIP_MAC),
                           of.ofp_action_output(port=event.port)]
        event.connection.send(reverse)

        # Forward rule: packets to 10.0.0.53 are rewritten to go to the chosen server
        forward = of.ofp_flow_mod(priority=200, idle_timeout=5)
        forward.match = of.ofp_match(dl_type=0x0800, nw_proto=17,
                                     nw_src=client_ip, tp_src=client_port,
                                     nw_dst=VIP, tp_dst=53)
        forward.actions = [of.ofp_action_dl_addr.set_dst(server_mac),
                           of.ofp_action_nw_addr.set_dst(server_ip),
                           of.ofp_action_output(port=out_port)]
        forward.data = event.ofp            # also send this first packet on its way
        event.connection.send(forward)
        log.debug("query %s:%s -> %s", client_ip, client_port, server_ip)

    # ------------------------------------------------------------ failure detection
    def got_heartbeat(self, event, ip_pkt):
        if ip_pkt.srcip in SERVERS:
            self.last_heartbeat[ip_pkt.srcip] = time.time()
        # Flood the heartbeat on, so every switch learns where the servers are.
        self.flood(event)

    def check_servers(self):
        """Runs every 0.5 s: compare each server's last heartbeat with the clock."""
        now = time.time()
        for ip in SERVERS:
            heard_recently = (now - self.last_heartbeat[ip]) < DEAD_AFTER
            if self.alive[ip] and not heard_recently:
                self.mark_dead(ip, "no heartbeat for %.1f s" % DEAD_AFTER)
            elif not self.alive[ip] and heard_recently:
                self.alive[ip] = True
                log.info("server %s is UP", ip)

    def mark_dead(self, ip, reason):
        if not self.alive[ip]:
            return
        self.alive[ip] = False
        log.warning("server %s is DOWN (%s)", ip, reason)
        if self.policy == "static":
            return                                   # the baseline does nothing about it
        # Delete the DNS rules on every switch. The next packet of every query then comes
        # back to the controller and is sent to a live server.
        for connection in core.openflow.connections:
            to_vip = of.ofp_flow_mod(command=of.OFPFC_DELETE)
            to_vip.match = of.ofp_match(dl_type=0x0800, nw_proto=17, nw_dst=VIP)
            connection.send(to_vip)
            from_dead = of.ofp_flow_mod(command=of.OFPFC_DELETE)
            from_dead.match = of.ofp_match(dl_type=0x0800, nw_proto=17, nw_src=ip)
            connection.send(from_dead)
        log.warning("deleted the rules for %s - queries will be redirected", ip)

    # ------------------------------------------------------------ helpers
    def answer_arp(self, event, request):
        mac = VIP_MAC if request.protodst == VIP else HEARTBEAT_MAC
        reply = arp(opcode=arp.REPLY, hwsrc=mac, hwdst=request.hwsrc,
                    protosrc=request.protodst, protodst=request.protosrc)
        frame = ethernet(type=ethernet.ARP_TYPE, src=mac, dst=request.hwsrc)
        frame.payload = reply
        msg = of.ofp_packet_out(data=frame.pack(), in_port=event.port)
        msg.actions.append(of.ofp_action_output(port=of.OFPP_IN_PORT))
        event.connection.send(msg)

    def forward_normally(self, event, packet):
        """Learning switch: send to the known port (and install a rule), or flood."""
        port = self.mac_to_port.get(event.dpid, {}).get(packet.dst)
        if packet.dst.is_multicast or port is None:
            self.flood(event)
            return
        rule = of.ofp_flow_mod(priority=10, idle_timeout=60)
        rule.match = of.ofp_match(dl_dst=packet.dst)
        rule.actions = [of.ofp_action_output(port=port)]
        rule.data = event.ofp
        event.connection.send(rule)

    def flood(self, event):
        msg = of.ofp_packet_out(data=event.ofp, in_port=event.port)
        msg.actions.append(of.ofp_action_output(port=of.OFPP_FLOOD))
        event.connection.send(msg)


def launch(policy="round_robin"):
    """POX calls this when the component is loaded."""
    logging.getLogger("packet").setLevel(logging.CRITICAL)   # hide POX's own parser warnings
    core.registerNew(DNSController, policy)
