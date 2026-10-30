"""
experiment.py  -  performance evaluation in Mininet.   Run:  sudo python3 experiment.py

Experiment 1  Server failure : static (baseline) vs round_robin (dynamic SDN).
              c1 sends ~20 queries/s for 20 s; ns1 is killed after 5 s.
Experiment 2  Caching        : the same load with the cache OFF vs ON.
Experiment 3  Load balancing : static vs round_robin with slow servers (100 ms per request,
              one request at a time), 3 clients sending as fast as they can for 10 s.

Results are printed, saved to results.txt, and drawn in results.png (if matplotlib is installed).
"""

import json
import os
import subprocess
import sys
import time

import topology

HERE = os.path.dirname(os.path.abspath(__file__))
USERSPACE = "--userspace" in sys.argv


def start(policy, server_args=""):
    """Start controller + network + servers, wait for the first heartbeats."""
    pox = topology.start_controller(policy)
    net = topology.build_network(USERSPACE)
    net.start()
    if USERSPACE:
        for host in net.hosts:
            host.cmd("ethtool -K %s-eth0 tx off" % host.name)
    topology.start_servers(net, server_args)
    time.sleep(3)
    return net, pox


def stop(net, pox):
    os.system("pkill -f dns_server.py")
    net.stop()
    pox.terminate()
    time.sleep(1)


def run_bench(host, seconds, options):
    """Start the client's load test on a Mininet host (in the background)."""
    cmd = ["python3", "dns_client.py", "--bench", str(seconds)] + options.split()
    return host.popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def get_summary(proc):
    """Wait for the load test to finish and read its SUMMARY line."""
    output = proc.communicate()[0].decode()
    line = [l for l in output.splitlines() if l.startswith("SUMMARY ")][-1]
    return json.loads(line[len("SUMMARY "):])


def experiment_failure():
    results = {}
    for policy in ("static", "round_robin"):
        net, pox = start(policy)
        proc = run_bench(net["c1"], 20, "--rate 20 --no-cache")
        time.sleep(5)
        net["ns1"].cmd("pkill -f 'dns_server.py ns1'")        # crash ns1
        results[policy] = get_summary(proc)
        stop(net, pox)
    return results


def experiment_cache():
    net, pox = start("round_robin")
    off = get_summary(run_bench(net["c1"], 10, "--rate 50 --no-cache"))
    on = get_summary(run_bench(net["c1"], 10, "--rate 50"))
    stop(net, pox)
    return {"cache off": off, "cache on": on}


def experiment_load():
    results = {}
    for policy in ("static", "round_robin"):
        net, pox = start(policy, "--delay 0.1 --workers 1")
        procs = [run_bench(net[c], 10, "--no-cache") for c in ("c1", "c2", "c3")]
        summaries = [get_summary(p) for p in procs]
        served = {}
        for s in summaries:
            for server, n in s["served_by"].items():
                served[server] = served.get(server, 0) + n
        results[policy] = {
            "throughput": round(sum(s["ok"] for s in summaries) / 10.0, 1),
            "avg_ms": round(sum(s["avg_ms"] for s in summaries) / 3, 2),
            "served_by": served,
        }
        stop(net, pox)
    return results


def main():
    failure = experiment_failure()
    cache = experiment_cache()
    load = experiment_load()

    lines = ["EXPERIMENT 1: server failure (ns1 killed after 5 s, 20 s run, ~20 queries/s)",
             "%-12s %10s %8s %12s" % ("policy", "answered", "failed", "longest (ms)")]
    for policy, s in failure.items():
        lines.append("%-12s %10d %8d %12s" % (policy, s["ok"], s["failed"], s["max_ms"]))
    lines += ["", "EXPERIMENT 2: caching (10 s, ~50 queries/s)",
              "%-12s %12s %12s" % ("variant", "avg (ms)", "p95 (ms)")]
    for variant, s in cache.items():
        lines.append("%-12s %12s %12s" % (variant, s["avg_ms"], s["p95_ms"]))
    lines += ["", "EXPERIMENT 3: load balancing (servers: 100 ms per request, 1 at a time; 3 clients, 10 s)",
              "%-12s %16s %10s   %s" % ("policy", "queries/second", "avg (ms)", "answered by")]
    for policy, s in load.items():
        lines.append("%-12s %16s %10s   %s" % (policy, s["throughput"], s["avg_ms"], s["served_by"]))
    text = "\n".join(lines)
    print("\n" + text)
    with open(os.path.join(HERE, "results.txt"), "w") as f:
        f.write(text + "\n")

    try:                                               # optional chart
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(1, 3, figsize=(12, 3.6))
        ax[0].bar(["static", "round_robin"], [failure[p]["ok"] for p in failure],
                  color=["#eb6834", "#2a78d6"])
        ax[0].set_title("Answers in 20 s (ns1 crashes)")
        ax[1].bar(["cache off", "cache on"], [cache[v]["avg_ms"] for v in cache],
                  color=["#eb6834", "#2a78d6"])
        ax[1].set_title("Average lookup time (ms)")
        ax[2].bar(["static", "round_robin"], [load[p]["throughput"] for p in load],
                  color=["#eb6834", "#2a78d6"])
        ax[2].set_title("Queries per second (3 clients)")
        fig.tight_layout()
        fig.savefig(os.path.join(HERE, "results.png"), dpi=120)
        print("chart saved to results.png")
    except ImportError:
        print("(install python3-matplotlib to get results.png)")


if __name__ == "__main__":
    main()
