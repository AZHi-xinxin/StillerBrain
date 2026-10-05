"""Synthetic-only browser fixture. No real model/ST transport is constructed."""
import json
from pathlib import Path
import sys
import tempfile
import threading

from migration_ui.engine import MigrationEngine
from migration_ui.server import make_server
from migration_ui.storage import PrivateLedger
from migration_ui.tests.test_migration import FakeClassifier, FakeST


def main():
    with tempfile.TemporaryDirectory(prefix="migration-ui-synthetic-") as directory:
        ledger=PrivateLedger(Path(directory)/"migration-ui")
        engine=MigrationEngine(ledger,classifier_factory=FakeClassifier,st_factory=FakeST)
        server=make_server(engine)
        print(json.dumps({"url":f"http://127.0.0.1:{server.server_address[1]}/"}),flush=True)
        def stop():
            sys.stdin.readline()
            engine.cancel()
            server.shutdown()
        threading.Thread(target=stop,daemon=True).start()
        try:server.serve_forever()
        finally:
            server.server_close()
            if engine.worker:engine.worker.join()
            ledger.close()


if __name__=="__main__":main()
