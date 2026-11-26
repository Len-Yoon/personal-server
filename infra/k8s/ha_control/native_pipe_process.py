"""Deadline-bound subprocess pipes for Windows, where select supports only sockets."""

from queue import Empty, Queue
import subprocess
import threading

from . import native_stdio_bridge as bridge


class PipeProcess:
    def __init__(self, process):
        self.process = process

    def _io(self, operation, deadline):
        remaining = bridge._remaining(deadline)
        result = Queue(maxsize=1)

        def run():
            try:
                result.put((True, operation()))
            except (OSError, ValueError):
                result.put((False, None))

        threading.Thread(target=run, daemon=True).start()
        try:
            ok, value = result.get(timeout=remaining)
        except Empty:
            raise bridge.BridgeRejected('guest_ssh_deadline_exceeded') from None
        bridge._remaining(deadline)
        bridge._need(ok, 'guest_ssh_pipe_failed')
        return value

    def read(self, size, deadline):
        return self._io(lambda: self.process.stdout.read(size), deadline)

    def write(self, data, deadline):
        remaining = memoryview(data)
        while remaining:
            size = self._io(lambda: self.process.stdin.write(remaining), deadline)
            bridge._need(type(size) is int and 0 < size <= len(remaining), 'guest_ssh_pipe_failed')
            remaining = remaining[size:]

    def finish(self, deadline):
        try:
            code = self.process.wait(timeout=bridge._remaining(deadline))
        except subprocess.TimeoutExpired:
            raise bridge.BridgeRejected('guest_ssh_deadline_exceeded') from None
        bridge._need(code == 0, 'guest_ssh_exit_unconfirmed')

    def close(self):
        # Direct subprocess only. Windows callers must disable proxy commands.
        if self.process.poll() is None:
            try:
                self.process.kill()
                self.process.wait(timeout=0.2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.process.stdin.close()
        self.process.stdout.close()


class PipeStdio(bridge.Stdio):
    def __init__(self, pipe):
        self.pipe = pipe
        self.buffer = bytearray()

    def _read(self, size, deadline):
        return self.pipe.read(size, deadline)

    def _write(self, raw, deadline):
        self.pipe.write(raw, deadline)


def start_direct(command, *, cwd=None, env=None):
    # Callers use absolute executable names and explicitly disable SSH proxies.
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, bufsize=0, cwd=cwd, env=env, shell=False)
    return PipeProcess(process)


def exchange_direct(command, payload, deadline, *, maximum, start=start_direct):
    bridge._remaining(deadline)
    pipe = start(command)
    try:
        pipe.write(payload, deadline)
        pipe.process.stdin.close()
        output = bytearray()
        while True:
            chunk = pipe.read(maximum + 1 - len(output), deadline)
            if not chunk:
                break
            output.extend(chunk)
            bridge._need(len(output) <= maximum, 'guest_ssh_frame_limit')
        pipe.finish(deadline)
        return bytes(output)
    finally:
        pipe.close()
