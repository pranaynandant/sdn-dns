"""
dns_client.py  -  a simple DNS-like client (resolver) over UDP.

What it does
  * sends a query to the server over UDP and waits for the reply
  * TIMEOUTS: if no reply comes, it tries again with double the waiting time
  * CACHING: remembers answers until their TTL runs out
  * REDIRECTS: if the server says "REFER", it asks the server it was pointed to
  * LATENCY: measures how long every lookup takes (in milliseconds)

Examples
    python3 dns_client.py www.cn.lab mail.cn.lab              # look up names
    python3 dns_client.py db.cn.lab --repeat 3                # see the cache work
    python3 dns_client.py lab1.research.cn.lab                # see a redirect
    python3 dns_client.py --add new.cn.lab 192.168.50.5 --server 10.0.0.11
    python3 dns_client.py --bench 10 --no-cache               # load test for 10 s
"""

import argparse
import json
import random
import socket
import time

VIP = "10.0.0.53"     # the "virtual" DNS server address handled by the SDN controller
cache = {}            # name -> (ip, time when the entry expires)


def ask_server(server_ip, port, request, timeout):
    """Send ONE request over UDP and wait for the matching reply.
    Returns the reply (a dictionary), or None if nothing arrived in time."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(json.dumps(request).encode(), (server_ip, port))
        while True:
            data, addr = sock.recvfrom(2048)
            reply = json.loads(data.decode())
            # Only accept the reply if it comes from the server we asked AND has our id.
            # (Protects against old/duplicate replies and fake replies.)
            if addr[0] == server_ip and reply.get("id") == request["id"]:
                return reply
    except socket.timeout:
        return None
    except (OSError, ValueError):
        return None
    finally:
        sock.close()


def resolve(name, server=VIP, port=53, use_cache=True, timeout=1.0, retries=2, verbose=False):
    """Find the IP address of `name`. Returns a dictionary with the result and latency."""
    start = time.time()

    # 1) CACHE: answer immediately if we still have a fresh copy.
    if use_cache and name in cache:
        ip, expires_at = cache[name]
        if time.time() < expires_at:
            return {"status": "OK", "name": name, "ip": ip, "server": "cache",
                    "latency_ms": (time.time() - start) * 1000, "attempts": 0}
        del cache[name]                                   # expired: forget it

    attempts = 0
    for _hop in range(3):                                 # follow at most 3 redirects
        reply = None
        wait = timeout
        # 2) TIMEOUT + RETRY: try 1 + `retries` times, doubling the wait each time.
        for _try in range(retries + 1):
            attempts += 1
            request = {"type": "query", "id": random.randint(1, 65535), "name": name}
            reply = ask_server(server, port, request, wait)
            if reply is not None:
                break
            if verbose:
                print(f"   timeout after {wait:.1f}s from {server}, retrying...")
            wait = wait * 2                               # exponential back-off

        if reply is None:
            return {"status": "TIMEOUT", "name": name, "server": server, "attempts": attempts,
                    "latency_ms": (time.time() - start) * 1000}

        # 3) REDIRECT: the server told us to ask another server.
        if reply["status"] == "REFER":
            if verbose:
                print(f"   {reply['server']} says: ask {reply['ask']} for {name}")
            server = reply["ask"]
            continue
        break

    # 4) Save good answers in the cache for `ttl` seconds.
    if reply["status"] == "OK" and use_cache:
        cache[name] = (reply["ip"], time.time() + reply["ttl"])

    reply["latency_ms"] = (time.time() - start) * 1000
    reply["attempts"] = attempts
    return reply


def show(result):
    """Print one result on one line."""
    if result["status"] == "OK":
        print(f"{result['name']:<22} -> {result['ip']:<15} {result['latency_ms']:8.2f} ms"
              f"   (from {result['server']}, attempts={result['attempts']})")
    else:
        print(f"{result['name']:<22} -> {result['status']:<15} {result['latency_ms']:8.2f} ms"
              f"   (attempts={result['attempts']})")


def add_record(name, ip, server, port):
    """Ask a server to add a new name -> IP mapping."""
    request = {"type": "add", "id": random.randint(1, 65535), "name": name, "ip": ip, "ttl": 60}
    reply = ask_server(server, port, request, 2.0)
    print("add:", reply["status"] if reply else "TIMEOUT", "by", reply["server"] if reply else "-")


def bench(seconds, rate, server, port, use_cache):
    """Send queries for `seconds` seconds (about `rate` per second) and print statistics."""
    names = ["www.cn.lab", "mail.cn.lab", "ftp.cn.lab", "db.cn.lab"]
    latencies, served_by = [], {}
    ok = failed = 0
    end = time.time() + seconds
    while time.time() < end:
        result = resolve(random.choice(names), server, port, use_cache)
        if result["status"] != "TIMEOUT":          # we got an answer (OK or NOT_FOUND)
            ok += 1
            latencies.append(result["latency_ms"])
            served_by[result["server"]] = served_by.get(result["server"], 0) + 1
        else:
            failed += 1
        if rate:
            time.sleep(1.0 / rate)

    latencies.sort()
    summary = {
        "ok": ok, "failed": failed,
        "success_percent": round(100.0 * ok / max(1, ok + failed), 1),
        "avg_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "p95_ms": round(latencies[int(0.95 * (len(latencies) - 1))], 2) if latencies else None,
        "max_ms": round(latencies[-1], 2) if latencies else None,
        "served_by": served_by,
    }
    print(f"queries ok={ok} failed={failed} success={summary['success_percent']}%")
    print(f"latency avg={summary['avg_ms']} ms  p95={summary['p95_ms']} ms  max={summary['max_ms']} ms")
    print(f"answered by: {served_by}")
    print("SUMMARY " + json.dumps(summary))       # one machine-readable line for experiment.py


def main():
    parser = argparse.ArgumentParser(description="Simple DNS-like client")
    parser.add_argument("names", nargs="*", help="names to look up")
    parser.add_argument("--server", default=VIP, help="server address (default: the SDN virtual IP)")
    parser.add_argument("--port", type=int, default=53)
    parser.add_argument("--repeat", type=int, default=1, help="look each name up N times")
    parser.add_argument("--no-cache", action="store_true", help="switch the cache off")
    parser.add_argument("--add", nargs=2, metavar=("NAME", "IP"), help="add a mapping on --server")
    parser.add_argument("--bench", type=float, default=0, help="run a load test for N seconds")
    parser.add_argument("--rate", type=float, default=0, help="queries per second in --bench (0 = max)")
    args = parser.parse_args()

    if args.add:
        add_record(args.add[0], args.add[1], args.server, args.port)
    elif args.bench:
        bench(args.bench, args.rate, args.server, args.port, not args.no_cache)
    else:
        for _ in range(args.repeat):
            for name in args.names:
                show(resolve(name, args.server, args.port, not args.no_cache, verbose=True))


if __name__ == "__main__":
    main()
