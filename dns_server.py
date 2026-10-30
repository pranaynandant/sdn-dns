"""
dns_server.py  -  a simple DNS-like name server that works over UDP.

What it does
  * keeps name -> IP mappings (loaded from a JSON file)
  * answers "query" messages from clients over UDP
  * redirects clients ("REFER") for names that belong to another server
  * accepts "add" messages to add a new mapping, and copies them to its peers
  * sends a heartbeat every 0.5 s so the SDN controller knows it is alive

Run it like this:
    python3 dns_server.py ns1 records.json
    python3 dns_server.py ns1 records.json --heartbeat --peers 10.0.0.12,10.0.0.13

Messages are JSON text inside UDP packets, for example
    client -> server : {"type": "query", "id": 1234, "name": "www.cn.lab"}
    server -> client : {"id": 1234, "status": "OK", "name": "www.cn.lab",
                        "ip": "192.168.10.10", "ttl": 60, "server": "ns1"}
"""

import argparse
import json
import random
import socket
import threading
import time

HEARTBEAT_IP = "10.0.0.254"   # fake address; the SDN controller catches these packets
HEARTBEAT_PORT = 9999


def load_records(path):
    """Read the name -> IP mappings from a JSON file into a Python dictionary."""
    with open(path) as f:
        return json.load(f)


def lookup(records, name):
    """Find the answer for one name. Returns a dictionary (the reply)."""
    # 1) Is the name inside a zone that another server is responsible for?
    for zone, server_ip in records.get("_delegations", {}).items():
        if name == zone or name.endswith("." + zone):
            return {"status": "REFER", "name": name, "ask": server_ip}

    # 2) Follow aliases (like a CNAME): portal.cn.lab -> www.cn.lab
    asked = name
    for _ in range(5):                       # at most 5 steps, to avoid loops
        entry = records.get(name)
        if entry is None:
            return {"status": "NOT_FOUND", "name": asked}
        if "alias" in entry:
            name = entry["alias"]
            continue
        return {"status": "OK", "name": asked, "ip": entry["ip"], "ttl": entry["ttl"]}
    return {"status": "NOT_FOUND", "name": asked}


def handle_request(sock, data, client_addr, args, records, workers):
    """Runs in its own thread: decode one request, build the reply, send it back."""
    with workers:                            # wait for a free "worker" (limited capacity)
        process(sock, data, client_addr, args, records)


def process(sock, data, client_addr, args, records):
    try:
        request = json.loads(data.decode())
    except ValueError:
        sock.sendto(b'{"status": "ERROR", "reason": "bad message"}', client_addr)
        return

    if args.delay:                           # pretend the server is slow (for experiments)
        time.sleep(args.delay)

    if request.get("type") == "query":
        reply = lookup(records, request.get("name", ""))

    elif request.get("type") == "add":
        # Add (or change) a mapping, then copy it to the other servers.
        records[request["name"]] = {"ip": request["ip"], "ttl": request.get("ttl", 60)}
        print(f"[{args.name}] added {request['name']} -> {request['ip']}")
        if not request.get("from_peer"):
            copy = dict(request, from_peer=True)
            for peer in args.peers:
                sock.sendto(json.dumps(copy).encode(), (peer, args.port))
        reply = {"status": "OK", "name": request["name"], "ip": request["ip"]}

    else:
        reply = {"status": "ERROR", "reason": "unknown type"}

    reply["id"] = request.get("id")          # so the client can match reply to request
    reply["server"] = args.name              # tells the client which server answered
    sock.sendto(json.dumps(reply).encode(), client_addr)


def send_heartbeats(name):
    """Every 0.5 s, tell the network 'I am alive'. The SDN controller listens for these."""
    hb_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        message = json.dumps({"type": "heartbeat", "server": name}).encode()
        try:
            hb_sock.sendto(message, (HEARTBEAT_IP, HEARTBEAT_PORT))
        except OSError:
            pass                              # e.g. no network yet - just try again
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description="Simple DNS-like UDP server")
    parser.add_argument("name", help="server name, e.g. ns1")
    parser.add_argument("records", help="JSON file with the name -> IP mappings")
    parser.add_argument("--ip", default="0.0.0.0", help="address to listen on")
    parser.add_argument("--port", type=int, default=53, help="UDP port (DNS uses 53)")
    parser.add_argument("--peers", default="", help="other servers to copy 'add' messages to")
    parser.add_argument("--heartbeat", action="store_true", help="send heartbeats to the controller")
    parser.add_argument("--delay", type=float, default=0, help="extra seconds per request")
    parser.add_argument("--workers", type=int, default=8, help="requests handled at the same time")
    parser.add_argument("--drop", type=float, default=0, help="fraction of requests to ignore (packet loss)")
    args = parser.parse_args()
    args.peers = [p for p in args.peers.split(",") if p]

    records = load_records(args.records)
    workers = threading.Semaphore(args.workers)       # at most N requests at the same time

    # Create the UDP socket and bind it to the port. (No listen/accept: UDP has no connections.)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.ip, args.port))
    print(f"[{args.name}] listening on UDP {args.ip}:{args.port} with {len(records)} records")

    if args.heartbeat:
        threading.Thread(target=send_heartbeats, args=(args.name,), daemon=True).start()

    while True:
        data, client_addr = sock.recvfrom(2048)       # wait for the next request
        if random.random() < args.drop:               # simulate a lost packet
            continue
        # Answer in a new thread so one slow request does not block the others.
        threading.Thread(target=handle_request,
                         args=(sock, data, client_addr, args, records, workers),
                         daemon=True).start()


if __name__ == "__main__":
    main()
