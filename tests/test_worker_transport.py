import subprocess
import sys

import pytest

from protocollab.isolation import WorkerProcess


@pytest.mark.parametrize("late_error", [False, True])
def test_late_worker_response_cannot_be_mistaken_for_next_request(late_error):
    # Transport-only test. Real namespace boundaries have their own integration test.
    script = """
import json,sys,time
for line in sys.stdin:
    packet=json.loads(line)
    if packet['value']=='slow': time.sleep(0.2)
    if packet['value']=='slow' and LATE_ERROR:
        response={'type':'error','error':'QueryInterrupted','message':'expired request failed'}
    elif packet['value']=='error':
        response={'type':'error','error':'RuntimeError','message':'current request failed'}
    else:
        response={'type':'result','result':packet['value']}
    print(json.dumps({**response,'_request_id':packet['_request_id']}),flush=True)
"""
    script = script.replace("LATE_ERROR", repr(late_error))
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
        with pytest.raises(RuntimeError, match="current request failed"):
            worker.request({"value": "error"}, timeout=2)
    finally:
        worker.close()
