import subprocess
import sys

import pytest

from protocollab.isolation import WorkerProcess


def test_late_worker_response_cannot_be_mistaken_for_next_request():
    # Transport-only test. Real namespace boundaries have their own integration test.
    script = """
import json,sys,time
for line in sys.stdin:
    packet=json.loads(line)
    if packet['value']=='slow': time.sleep(0.2)
    print(json.dumps({'type':'result','result':packet['value'],'_request_id':packet['_request_id']}),flush=True)
"""
    worker = WorkerProcess.__new__(WorkerProcess)
    worker.role = "transport-test"
    worker._read_buffer = b""
    worker._request_counter = 0
    worker.process = subprocess.Popen([sys.executable, "-u", "-c", script], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with pytest.raises(TimeoutError):
            worker.request({"value": "slow"}, timeout=.05)
        assert worker.request({"value": "next"}, timeout=2)["result"] == "next"
        assert worker.request({"value": "last"}, timeout=2)["result"] == "last"
    finally:
        worker.close()
