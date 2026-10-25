"""A TCP proxy in front of Redis that can misbehave on demand, for failure tests.

    proxy = FaultyProxy()            # forwards 127.0.0.1:<proxy.port> to localhost:6379
    board = Feed.from_url(proxy.url(db=15))
    proxy.cut()                      # drop every open connection, like a Redis restart or a network blip
    proxy.down()                     # drop them and refuse new ones, like Redis being stopped
    proxy.silent()                   # keep connections open but pass nothing, like a hung server
    proxy.up()                       # back to normal
    proxy.close()
"""
import select
import socket
import threading
import time


class FaultyProxy:
    def __init__(self, target=("localhost", 6379)):
        self.target = target
        self.mode = "up"
        self.cuts = 0
        self._pairs = set()
        self._lock = threading.Lock()
        self._running = True
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self._listener.settimeout(0.1)
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def url(self, db=15):
        return f"redis://127.0.0.1:{self.port}/{db}"

    def up(self):
        self.mode = "up"

    def silent(self):
        self.mode = "silent"

    def down(self):
        self.mode = "down"
        self.cut()

    def cut(self):
        """Close every open connection (both sides)."""
        with self._lock:
            pairs, self._pairs = list(self._pairs), set()
            self.cuts += 1
        for pair in pairs:
            for s in pair:
                _close(s)

    def close(self):
        self._running = False
        _close(self._listener)
        self.cut()

    def _accept_loop(self):
        while self._running:
            try:
                client, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self.mode == "down":
                _close(client)
                continue
            try:
                upstream = socket.create_connection(self.target, timeout=2)
            except OSError:
                _close(client)
                continue
            pair = (client, upstream)
            with self._lock:
                self._pairs.add(pair)
            threading.Thread(target=self._pump, args=(pair,), daemon=True).start()

    def _pump(self, pair):
        client, upstream = pair
        other = {client: upstream, upstream: client}
        try:
            while self._running:
                with self._lock:
                    if pair not in self._pairs:
                        return
                if self.mode == "silent":
                    time.sleep(0.02)              # hold everything: data waits in the kernel buffers
                    continue
                readable, _, _ = select.select([client, upstream], [], [], 0.05)
                for s in readable:
                    data = s.recv(65536)
                    if not data:
                        return
                    other[s].sendall(data)
        except (OSError, ValueError):
            return
        finally:
            with self._lock:
                self._pairs.discard(pair)
            _close(client)
            _close(upstream)


def _close(s):
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        s.close()
    except OSError:
        pass
