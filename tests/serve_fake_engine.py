"""Serve the fake engine on a fixed port, for the workflow's own check."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_engine import FakeEngine  # noqa: E402

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18765
    engine = FakeEngine(polls_before_done=2).start(port=port)
    print(f"fake engine on {engine.url}", flush=True)
    while True:
        time.sleep(3600)
