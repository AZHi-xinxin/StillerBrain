"""Synthetic regression runner. Never loads deployment configuration."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import time
import unittest

parser = argparse.ArgumentParser()
parser.add_argument("--suite", choices=("gateway", "runtime", "mcp"), required=True)
parser.add_argument("--label", required=True)
args = parser.parse_args()
if not args.label.replace("-", "").isalnum():
    raise SystemExit("invalid_label")
root = Path(__file__).resolve().parents[1]
output = root.parent / (args.label + ".log")
for key in tuple(os.environ):
    if key.startswith(("STBRAIN_", "OMBRE_")):
        del os.environ[key]
sys.dont_write_bytecode = True
sys.path.insert(0, str(root))
os.chdir(root)
os.environ["STBRAIN_LONG_STREAM_TEST"] = "1"
original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex
def check(address):
    if not isinstance(address, tuple):
        raise RuntimeError("non_loopback_socket_blocked")
    if address[0] != "localhost":
        try:
            if not ipaddress.ip_address(address[0]).is_loopback:
                raise ValueError()
        except ValueError:
            raise RuntimeError("external_socket_blocked") from None
def connect(sock, address):
    check(address)
    return original_connect(sock, address)
def connect_ex(sock, address):
    check(address)
    return original_connect_ex(sock, address)
socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
directories = {"gateway": "rikkahub_gateway/tests", "runtime": "tests", "mcp": "mcp_server/tests"}
suite = unittest.TestLoader().discover(str(root / directories[args.suite]), top_level_dir=str(root))
started = time.monotonic()
with output.open("x", encoding="utf-8") as stream:
    original_stderr, original_stdout = sys.stderr, sys.stdout
    try:
        sys.stderr = sys.stdout = stream
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    finally:
        sys.stderr, sys.stdout = original_stderr, original_stdout
receipt = {"suite": args.suite, "tests": result.testsRun, "ok": result.wasSuccessful(),
           "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
           "seconds": round(time.monotonic() - started, 3), "socket_policy": "loopback-only",
           "production_configuration_loaded": False}
with output.with_suffix(".json").open("x", encoding="utf-8") as stream:
    json.dump(receipt, stream, indent=2)
print(json.dumps(receipt))
raise SystemExit(0 if result.wasSuccessful() else 1)
