import signal
import time
from pathlib import Path


root = Path("/workspace/agora-run")
counter = root / "fixture-training-launch-count"
identity = root / "private_gpu0.key"
if not identity.exists():
    identity.write_text("process-composition-fixture-identity\n", encoding="utf-8")
    identity.chmod(0o600)
attempt = int(counter.read_text(encoding="utf-8") or "0") + 1 if counter.exists() else 1
counter.write_text(str(attempt), encoding="utf-8")
stopped = False


def stop(*_args):
    global stopped
    stopped = True


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
while not stopped:
    time.sleep(0.2)
