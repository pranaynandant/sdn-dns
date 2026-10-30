"""
test_local.py  -  automatic tests that run on any laptop (no Mininet, no sudo needed).

They start real servers on the loopback addresses 127.0.0.11 / .12 / .13 / .14 and use
the client's resolve() function against them.

Run:  python3 test_local.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import dns_client

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 5300                                   # a high port, so no sudo is needed


def start_server(name, ip, records_file, *extra):
    cmd = [sys.executable, os.path.join(HERE, "dns_server.py"), name, records_file,
           "--ip", ip, "--port", str(PORT)] + list(extra)
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class TestDNS(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Same records as records.json, but the delegated zone points to 127.0.0.14
        with open(os.path.join(HERE, "records.json")) as f:
            records = json.load(f)
        records["_delegations"] = {"research.cn.lab": "127.0.0.14"}
        cls.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(records, cls.tmp)
        cls.tmp.close()
        cls.servers = [
            start_server("ns1", "127.0.0.11", cls.tmp.name, "--peers", "127.0.0.12"),
            start_server("ns2", "127.0.0.12", cls.tmp.name, "--peers", "127.0.0.11"),
            start_server("lossy", "127.0.0.13", cls.tmp.name, "--drop", "0.5"),
            start_server("ns4", "127.0.0.14", os.path.join(HERE, "research_records.json")),
        ]
        time.sleep(1)

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.terminate()
        os.unlink(cls.tmp.name)

    def setUp(self):
        dns_client.cache.clear()

    def resolve(self, name, server="127.0.0.11", **kw):
        return dns_client.resolve(name, server, PORT, **kw)

    def test_1_answer(self):
        r = self.resolve("www.cn.lab")
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["ip"], "192.168.10.10")
        self.assertEqual(r["server"], "ns1")

    def test_2_alias(self):
        self.assertEqual(self.resolve("portal.cn.lab")["ip"], "192.168.10.10")

    def test_3_not_found(self):
        self.assertEqual(self.resolve("nope.cn.lab")["status"], "NOT_FOUND")

    def test_4_cache(self):
        first = self.resolve("mail.cn.lab")
        second = self.resolve("mail.cn.lab")
        self.assertEqual(first["server"], "ns1")
        self.assertEqual(second["server"], "cache")
        self.assertLess(second["latency_ms"], first["latency_ms"])

    def test_5_cache_expires_after_ttl(self):
        self.resolve("short.cn.lab")                      # TTL is 5 seconds
        dns_client.cache["short.cn.lab"] = ("192.168.10.50", time.time() - 1)   # pretend 5 s passed
        self.assertEqual(self.resolve("short.cn.lab")["server"], "ns1")

    def test_6_redirect(self):
        r = self.resolve("gpu.research.cn.lab")
        self.assertEqual(r["ip"], "192.168.20.100")
        self.assertEqual(r["server"], "ns4")              # answered by the delegated server

    def test_7_timeout(self):
        start = time.time()
        r = self.resolve("www.cn.lab", server="127.0.0.99", timeout=0.2, retries=2)
        self.assertEqual(r["status"], "TIMEOUT")
        self.assertEqual(r["attempts"], 3)
        self.assertGreaterEqual(time.time() - start, 0.2 + 0.4 + 0.8 - 0.05)   # back-off

    def test_8_retries_survive_packet_loss(self):
        for _ in range(10):                               # server drops 50 % of requests
            r = self.resolve("ftp.cn.lab", server="127.0.0.13", use_cache=False,
                             timeout=0.1, retries=8)
            self.assertEqual(r["status"], "OK")

    def test_9_add_is_copied_to_peer(self):
        dns_client.add_record("added.cn.lab", "10.9.9.9", "127.0.0.11", PORT)
        time.sleep(0.3)
        self.assertEqual(self.resolve("added.cn.lab", server="127.0.0.12")["ip"], "10.9.9.9")


if __name__ == "__main__":
    unittest.main(verbosity=2)
