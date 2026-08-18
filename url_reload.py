#!/usr/bin/env python3
"""Reload a specific URL's tab in Chrome -- by URL, not by keystroke.

Talks to Chrome's DevTools Protocol, so it refreshes the right tab even when
that tab is in the background and you're working in another window. Nothing is
installed: the WebSocket client below is ~40 lines of stdlib socket code.

    python url_reload.py --launch                       # open Chrome + start reloading
    python url_reload.py --launch -i 5                  # every 5 seconds
    python url_reload.py --launch --url https://...     # any page
    python url_reload.py --tabs                         # list debuggable tabs
    python url_reload.py                                # attach to an already-launched Chrome

Chrome must be running with --remote-debugging-port. --launch does that for you,
using a separate profile so it won't disturb your normal Chrome session.
"""

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://github.com/Daniel-B-V"
PORT = 9222
PROFILE = os.path.join(tempfile.gettempdir(), "chrome-reload-profile")

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


# --------------------------------------------------------------------------
# Minimal WebSocket client (RFC 6455) -- just enough to drive CDP.
# --------------------------------------------------------------------------
class WS:
    def __init__(self, url, timeout=10):
        rest = url.split("://", 1)[1]              # ws://host:port/path
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            "GET /" + path + " HTTP/1.1\r\n"
            "Host: " + hostport + "\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: " + key + "\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake failed")
            buf += chunk
        status = buf.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise ConnectionError("upgrade refused: " + status.decode("utf-8", "replace"))

    def send(self, obj):
        payload = json.dumps(obj).encode()
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        self.sock.sendall(header + mask + masked)

    def _read(self, n):
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("socket closed")
            out += chunk
        return out

    def recv(self):
        b1, b2 = self._read(2)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read(8))[0]
        masked = bool(b2 & 0x80)
        if masked:
            mask = self._read(4)
            data = self._read(length)
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        else:
            data = self._read(length)
        return json.loads(data) if data else {}

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
def find_chrome():
    for path in CHROME_PATHS:
        if os.path.exists(path):
            return path
    return None


def launch_chrome(url, port, headless=False):
    exe = find_chrome()
    if not exe:
        sys.exit("couldn't find chrome.exe -- edit CHROME_PATHS in this file")
    cmd = [exe, "--remote-debugging-port=" + str(port), "--user-data-dir=" + PROFILE,
           "--no-first-run", "--no-default-browser-check"]
    if headless:
        cmd.append("--headless=new")
    cmd.append(url)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return exe


def list_targets(port, timeout=1.5):
    try:
        with urllib.request.urlopen("http://127.0.0.1:" + str(port) + "/json", timeout=timeout) as r:
            return [t for t in json.load(r) if t.get("type") == "page"]
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_for_target(port, match, tries=40):
    """Poll until Chrome is up and a tab whose URL contains `match` exists."""
    for _ in range(tries):
        targets = list_targets(port)
        if targets:
            for t in targets:
                if match.lower() in t.get("url", "").lower():
                    return t
        time.sleep(0.5)
    return None


def main():
    p = argparse.ArgumentParser(description="Reload a specific URL's Chrome tab.")
    p.add_argument("--url", default=DEFAULT_URL, help="page to open/reload")
    p.add_argument("--match", help="substring to find the tab by (default: derived from --url)")
    p.add_argument("-i", "--interval", type=float, default=5.0,
                   help="seconds between reloads (default: 5)")
    p.add_argument("-n", "--count", type=int, default=0, help="stop after N reloads (0 = forever)")
    p.add_argument("--launch", action="store_true", help="start Chrome with debugging enabled")
    p.add_argument("--headless", action="store_true", help="with --launch, run with no window")
    p.add_argument("--hard", action="store_true", help="ignore cache on reload")
    p.add_argument("--tabs", action="store_true", help="list debuggable tabs and exit")
    p.add_argument("--port", type=int, default=PORT)
    args = p.parse_args()

    if args.tabs:
        targets = list_targets(args.port)
        if targets is None:
            sys.exit("nothing listening on :" + str(args.port) + " -- start with --launch first.")
        for i, t in enumerate(targets, 1):
            print("%d. %-50s %s" % (i, (t.get("title") or "")[:50], (t.get("url") or "")[:70]))
        return

    match = args.match or args.url.split("://", 1)[-1].rstrip("/")

    if args.launch:
        if list_targets(args.port) is not None:
            print("chrome already listening on :" + str(args.port) + ", attaching instead")
        else:
            print("launching chrome (profile: " + PROFILE + ")")
            launch_chrome(args.url, args.port, args.headless)

    target = wait_for_target(args.port, match)
    if not target:
        sys.exit("no tab matching " + repr(match) + ". Run --tabs to see what is open, "
                 "or add --launch to open it.")

    print("reloading: " + (target.get("title") or target["url"]))
    print("url: " + target["url"])
    print("every %gs%s -- Ctrl+C to stop" % (args.interval, " (hard)" if args.hard else ""))
    print("this targets the tab directly, so you can use other windows freely.\n")

    ws = WS(target["webSocketDebuggerUrl"])
    limit = args.count if args.count > 0 else float("inf")
    done = failed = 0
    try:
        n = 0
        while n < limit:
            n += 1
            t0 = time.time()
            try:
                ws.send({"id": n, "method": "Page.reload",
                         "params": {"ignoreCache": bool(args.hard)}})
                while True:                       # skip CDP events, wait for our reply
                    msg = ws.recv()
                    if msg.get("id") == n:
                        break
                done += 1
                note = "ok"
            except (ConnectionError, OSError, ValueError) as e:
                failed += 1
                note = "failed (" + str(e) + ")"
                try:                              # tab may have been closed -- re-find it
                    ws.close()
                    target = wait_for_target(args.port, match, tries=4)
                    if not target:
                        print("tab is gone. stopping.")
                        break
                    ws = WS(target["webSocketDebuggerUrl"])
                    note += " -- reattached"
                except (ConnectionError, OSError):
                    print("lost chrome. stopping.")
                    break

            print("[%s] reload #%d %s (%.0fms)"
                  % (time.strftime("%H:%M:%S"), n, note, (time.time() - t0) * 1000), flush=True)
            if n < limit:
                time.sleep(max(0.0, args.interval - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        ws.close()

    print("\n%d reloads%s." % (done, ", %d failed" % failed if failed else ""))


if __name__ == "__main__":
    main()
