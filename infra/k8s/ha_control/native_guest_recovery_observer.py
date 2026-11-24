"""Read-only guest A recovery observations over a dedicated Hyper-V socket."""

import argparse
from collections import deque
from functools import partial
import os
import signal
import socket
import time

from . import native_guest_worker as worker, native_stdio_bridge as bridge

PURPOSES = {
    "held_admission": bridge.GUEST_OBSERVE_INITIAL_RECOVERY_HOLD,
    "provider_isolation": bridge.GUEST_OBSERVE_CAR_ISOLATION,
}
REQUEST_SECONDS = 1.8
ERROR = "guest_recovery_observation_unconfirmed"


class Observer:
    def __init__(self, hold_sha256, boot_sha256, *, postboot_attempt_id=None, postboot_stopped_receipt_sha256=None, postboot_carryover_receipt_sha256=None):
        bridge._need(bridge._hash(hold_sha256) and bridge._hash(boot_sha256), ERROR)
        bridge._need((postboot_attempt_id is None and postboot_stopped_receipt_sha256 is None) or
            (bridge._hash(postboot_attempt_id) and bridge._hash(postboot_stopped_receipt_sha256)), ERROR)
        bridge._need(postboot_carryover_receipt_sha256 is None or
            (bridge._hash(postboot_carryover_receipt_sha256) and postboot_attempt_id is not None), ERROR)
        self.postboot_carryover_receipt_sha256 = postboot_carryover_receipt_sha256
        self.postboot_attempt_id = postboot_attempt_id
        self.postboot_stopped_receipt_sha256 = postboot_stopped_receipt_sha256
        self.hold_sha256 = hold_sha256
        self.boot_sha256 = boot_sha256
        self.recent = deque(maxlen=1024)

    def observe(self, frame, *, deadline=None):
        purpose, nonce = frame.get("purpose"), frame.get("nonce")
        bridge._need(type(purpose) is str and purpose in PURPOSES and bridge._hash(nonce)
                     and nonce not in self.recent and bridge._matches(frame, {
                         "version": 1, "purpose": purpose, "nonce": nonce,
                         "hold_sha256": self.hold_sha256, "guest_boot_sha256": self.boot_sha256,
                     }), ERROR)
        self.recent.append(nonce)
        bridge._need(bridge._digest(worker.restore.BOOT_ID.read_bytes()) == self.boot_sha256, ERROR)
        raw, hold = worker._read_json(worker.ROOT / worker.INITIAL_RECOVERY_HOLD)
        bridge._need(bridge._digest(raw) == self.hold_sha256 and hold.get("node") == "A", ERROR)
        binding = bridge.Binding(**hold["binding"])
        bridge._need(binding.boot("A") == self.boot_sha256, ERROR)
        args = {key: hold[key] for key in bridge.INITIAL_RECOVERY_ARGS if key != "recovery_attempt_id"}
        args["recovery_attempt_id"] = hold["attempt_id"]
        verb = PURPOSES[purpose]
        if purpose == "provider_isolation":
            args["hold_sha256"] = self.hold_sha256
        if purpose == 'held_admission' and self.postboot_attempt_id is not None:
            verb=bridge.GUEST_OBSERVE_INITIAL_POSTBOOT
            args.update(hold_sha256=self.hold_sha256,postboot_attempt_id=self.postboot_attempt_id,
                        postboot_stopped_receipt_sha256=self.postboot_stopped_receipt_sha256)
            if self.postboot_carryover_receipt_sha256 is not None:
                args['postboot_carryover_receipt_sha256'] = self.postboot_carryover_receipt_sha256
        request = bridge.Request(binding, "A", verb, 1, nonce, args)
        if purpose == "provider_isolation" or (purpose == "held_admission" and self.postboot_carryover_receipt_sha256 is not None):
            if deadline is None:
                deadline = time.monotonic() + REQUEST_SECONDS
            inventory = partial(worker.restore._observer_inventory, deadline)
            result = worker.handle(request, provider_inventory=inventory)
        else:
            result = worker.handle(request)
        bridge._need(type(result) is bridge.Receipt and result.status == "PASS"
                     and bridge._hash(result.receipt_sha256)
                     and (purpose != "held_admission" or result.receipt_sha256 == (self.postboot_carryover_receipt_sha256 or self.postboot_stopped_receipt_sha256 or self.hold_sha256))
                     and bridge._digest(worker.restore.BOOT_ID.read_bytes()) == self.boot_sha256, ERROR)
        return frame | {"observation_sha256": result.receipt_sha256}


def _serve(listener, observer):
    deadline = None

    def expire(_signum, _frame):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(ERROR)

    signal.signal(signal.SIGALRM, expire)
    while True:
        connection, peer = listener.accept()
        try:
            with connection:
                if peer[0] != socket.VMADDR_CID_HOST:
                    continue
                deadline = time.monotonic() + REQUEST_SECONDS
                try:
                    signal.setitimer(signal.ITIMER_REAL, REQUEST_SECONDS)
                    channel = bridge.Stdio(connection.fileno(), connection.fileno())
                    frame = channel.read(deadline)
                    bridge._need(not channel.buffer, ERROR)
                    channel.write(observer.observe(frame, deadline=deadline), deadline)
                finally:
                    deadline = None
                    signal.setitimer(signal.ITIMER_REAL, 0)
        except (bridge.BridgeRejected, OSError, KeyError, TypeError, ValueError):
            # Disconnect on ambiguity. The Windows signer must require a complete positive response.
            continue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--hold-sha256", required=True)
    parser.add_argument("--guest-boot-sha256", required=True)
    parser.add_argument('--postboot-attempt-id')
    parser.add_argument('--postboot-stopped-receipt-sha256')
    parser.add_argument('--postboot-carryover-receipt-sha256')
    args = parser.parse_args()
    bridge._need(os.geteuid() == 0 and 1 <= args.port < 1024, ERROR)
    observer = Observer(args.hold_sha256, args.guest_boot_sha256,
        postboot_attempt_id=args.postboot_attempt_id,postboot_stopped_receipt_sha256=args.postboot_stopped_receipt_sha256,
        postboot_carryover_receipt_sha256=args.postboot_carryover_receipt_sha256)
    with socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM) as listener:
        listener.bind((socket.VMADDR_CID_ANY, args.port))
        listener.listen(4)
        _serve(listener, observer)


if __name__ == "__main__":
    main()
