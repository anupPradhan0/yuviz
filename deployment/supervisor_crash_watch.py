"""Restart policy for supervisord: heartbeat, exponential backoff, half-open circuit breaker. stdout is the protocol; log to stderr."""
from __future__ import annotations

import argparse
import errno
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xmlrpc.client
from dataclasses import asdict, dataclass, field
from pathlib import Path

RUNNING_STATES = {"STARTING", "RUNNING", "BACKOFF"}
FATAL = "FATAL"
PROBE_INTERVAL_S = 10
MISSES_TO_RESTART = 3
STARTUP_DEADLINE_S = 300
# A wedged Gateway never closes its calls rows, and its calls are already dead, so it restarts regardless.
LIVE_CALL_EXEMPT = frozenset({"gateway"})
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass
class ServiceState:
    crashes: list = field(default_factory=list)
    opens: int = 0
    half_open_since: float | None = None
    pending_at: float | None = None
    pending_trial: bool = False
    stop_token: object = None  # supervisord's 'stop' time of our stop, or FATAL


class Policy:
    def __init__(self, max_crashes: int, window_s: float, backoff_cap_s: float, cooldown_s: float, cooldown_cap_s: float):
        self.max_crashes = max_crashes
        self.window_s = window_s
        self.backoff_cap_s = backoff_cap_s
        self.cooldown_s = cooldown_s
        self.cooldown_cap_s = cooldown_cap_s
        self.services: dict[str, ServiceState] = {}

    def state(self, name: str) -> ServiceState:
        return self.services.setdefault(name, ServiceState())

    def on_exit(self, name: str, now: float) -> tuple[str, float]:
        s = self.state(name)
        if s.half_open_since is not None and now - s.half_open_since < self.window_s:
            return self._open(s)
        s.half_open_since = None
        s.crashes = [t for t in s.crashes if now - t <= self.window_s] + [now]
        n = len(s.crashes)
        if n >= self.max_crashes:
            return self._open(s)
        if n == 1:
            return ("restart", 0.0)
        return ("delay", min(2.0 ** (n - 1), self.backoff_cap_s))

    def on_fatal(self, name: str) -> tuple[str, float]:
        return self._open(self.state(name))

    def _open(self, s: ServiceState) -> tuple[str, float]:
        cooldown = min(self.cooldown_s * 2 ** s.opens, self.cooldown_cap_s)
        s.opens += 1
        s.crashes = []
        s.half_open_since = None
        return ("open", cooldown)

    def close_if_survived(self, name: str, now: float) -> bool:
        s = self.state(name)
        if s.half_open_since is not None and now - s.half_open_since >= self.window_s:
            s.opens, s.half_open_since = 0, None
            return True
        return False


def parse_tokens(line: str) -> dict[str, str]:
    return dict(token.split(":", 1) for token in line.split())


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost")
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


class _UnixTransport(xmlrpc.client.Transport):
    def __init__(self, path: str):
        super().__init__()
        self._path = path

    def make_connection(self, host):
        return _UnixHTTPConnection(self._path)


def rpc_proxy(server_url: str):
    if server_url.startswith("unix://"):
        return xmlrpc.client.ServerProxy("http://localhost", transport=_UnixTransport(server_url[len("unix://"):]))
    return xmlrpc.client.ServerProxy(server_url.rstrip("/") + "/RPC2")


def log(msg: str) -> None:
    sys.stderr.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    sys.stderr.flush()


LOG_FILE = Path.home() / ".yuviz" / "logs" / "crash-watch.log"


def notify(msg: str) -> None:
    if sys.platform != "darwin":
        return
    notifier = shutil.which("terminal-notifier") or next(
        (p for p in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier") if os.path.exists(p)), None)
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "timeout": 5, "check": False}
    if notifier:  # clicking opens the log
        done = subprocess.run([notifier, "-title", "yuviz crash-watch", "-message", msg, "-open", LOG_FILE.as_uri(),
                               "-group", "yuviz-crash-watch"], **quiet)
        if done is not None and done.returncode == 0:
            return
    # Credited to Script Editor, but always allowed.
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "yuviz crash-watch"'], **quiet)


def stop_service(proxy, name: str, settle_s: float = 60.0) -> float | None:
    """Idempotent. Returns supervisord's stop time once STOPPED, or None if it never got there."""
    deadline = time.monotonic() + settle_s
    while time.monotonic() < deadline:
        info = proxy.supervisor.getProcessInfo(name)
        if info["statename"] == "STOPPED":
            return float(info["stop"])
        if info["statename"] in RUNNING_STATES:
            try:
                proxy.supervisor.stopProcess(name, False)
            except xmlrpc.client.Fault:
                pass  # raced a state change; re-read
        time.sleep(0.2)
    return None


def start_service(proxy, name: str) -> None:
    """Idempotent: already running is success."""
    try:
        proxy.supervisor.startProcess(name, False)
    except xmlrpc.client.Fault as exc:
        if "ALREADY_STARTED" not in exc.faultString:
            raise


def probe_http(url: str, timeout: float = 3.0) -> bool:
    """Any HTTP response proves the event loop answered. No proxy."""
    try:
        with _OPENER.open(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (OSError, ValueError):
        return False


def fetch_clusters(admin: str = "http://127.0.0.1:9901", timeout: float = 3.0) -> str | None:
    try:
        with _OPENER.open(f"{admin}/clusters", timeout=timeout) as r:
            return r.read().decode(errors="replace")
    except OSError:
        return None


class _ClusterCache:
    """Both conversation probes run in the same round; one fetch serves both."""

    def __init__(self, ttl_s: float = 2.0, fetch=fetch_clusters):
        self.ttl_s, self.fetch = ttl_s, fetch
        self._at, self._text = float("-inf"), None

    def get(self) -> str | None:
        now = time.monotonic()
        if now - self._at > self.ttl_s:
            self._text, self._at = self.fetch(), now
        return self._text


def envoy_health(text: str | None, addr: str) -> bool | None:
    if text is None:
        return None
    for line in text.splitlines():
        if f"::{addr}::health_flags::" in line:
            flags = line.rsplit("::", 1)[1]
            if flags == "healthy":
                return True
            return False if "failed_active_hc" in flags else None
    return None


def probe_envoy_host(addr: str, admin: str = "http://127.0.0.1:9901", timeout: float = 3.0) -> bool | None:
    """Envoy's own gRPC health check for one upstream; None when Envoy can't say. No proxy."""
    return envoy_health(fetch_clusters(admin, timeout), addr)


def probe_esl(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            data = b""
            while b"auth/request" not in data:
                chunk = sock.recv(256)
                if not chunk:
                    return False
                data += chunk
        return True
    except OSError:
        return False


def probe_sip(host: str, port: int, timeout: float = 3.0) -> bool | None:
    """SIP OPTIONS; any SIP reply means Kamailio is processing requests. None if network is down (bind fails)."""
    tag, call_id = uuid.uuid4().hex[:8], uuid.uuid4().hex
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            try:
                sock.bind((host, 0))
            except OSError as e:
                if e.errno == errno.EADDRNOTAVAIL:
                    return None
                raise
            lport = sock.getsockname()[1]
            msg = (f"OPTIONS sip:{host}:{port} SIP/2.0\r\nVia: SIP/2.0/UDP {host}:{lport};branch=z9hG4bK{tag}\r\n"
                   f"Max-Forwards: 70\r\nFrom: <sip:crash-watch@{host}>;tag={tag}\r\nTo: <sip:{host}:{port}>\r\n"
                   f"Call-ID: {call_id}\r\nCSeq: 1 OPTIONS\r\nContent-Length: 0\r\n\r\n")
            try:
                sock.sendto(msg.encode(), (host, port))
                return sock.recv(4096).startswith(b"SIP/2.0 ")
            except socket.timeout:
                return False
            except OSError:
                return None
    except OSError:
        return None


def kamailio_listen(cfg: Path) -> tuple[str, int] | None:
    try:
        for line in cfg.read_text().splitlines():
            line = line.strip()
            if line.startswith("listen=udp:"):
                host, _, port = line[len("listen=udp:"):].rpartition(":")
                return host, int(port)
    except (OSError, ValueError):
        pass
    return None


class _FileMemo:
    """Re-parses a file only when its mtime changes."""

    def __init__(self, path: Path, parse):
        self.path, self.parse = path, parse
        self._mtime, self._value = None, None

    def get(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return None
        if mtime != self._mtime:
            self._mtime, self._value = mtime, self.parse(self.path)
        return self._value


LIVE_CALLS_SQL = "SELECT count(*) FROM calls WHERE ended_at IS NULL AND started_at > now() - interval '2 hours'"


def count_live_calls(dsn: str) -> int | None:
    """None when the count can't be read; the caller then aborts the restart."""
    try:
        out = subprocess.run(["psql", dsn, "-tAX", "-c", LIVE_CALLS_SQL],
                             capture_output=True, text=True, timeout=10, check=True)
        return int(out.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def url_port(key: str, default: int) -> int:
    """Port of the .env URL a service's callers use, so the probe follows the service."""
    try:
        return urllib.parse.urlparse(dotenv(key, "")).port or default
    except ValueError:
        return default


def dotenv(key: str, default: str, path: Path = Path(".env")) -> str:
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in path.read_text().splitlines():
            if line.startswith(f"{key}="):
                value = line.split("=", 1)[1].split(" #", 1)[0].strip().strip("'\"")
                if value:
                    return value
    except OSError:
        pass
    return default


def default_probes() -> dict:
    host = dotenv("LISTEN_HOST", "127.0.0.1")
    host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    esl = (dotenv("FREESWITCH_ESL_HOST", "127.0.0.1"), int(dotenv("FREESWITCH_ESL_PORT", "8022")))
    gw_host = dotenv("GATEWAY_LISTEN_HOST", "127.0.0.1")
    gw_host = "127.0.0.1" if gw_host in ("0.0.0.0", "::", "") else gw_host
    gw_port = dotenv("GATEWAY_LISTEN_PORT", "8080")
    clusters = _ClusterCache()
    listen = _FileMemo(Path.home() / ".yuviz" / "kamailio" / "kamailio.cfg", kamailio_listen)
    probes = {
        "config": lambda: probe_http(f"http://{host}:{url_port('CONFIG_SERVICE_URL', 8000)}/health"),
        "knowledge": lambda: probe_http(f"http://{host}:{url_port('KNOWLEDGE_SERVICE_URL', 8100)}/health"),
        "campaigns": lambda: probe_http(f"http://{host}:{url_port('CAMPAIGNS_SERVICE_URL', 8400)}/health"),
        "toolexec": lambda: probe_http(f"http://{host}:{url_port('TOOLEXEC_SERVICE_URL', 8600)}/health"),
        "telephony": lambda: probe_http(f"http://{host}:{url_port('TELEPHONY_SERVICE_URL', 8750)}/health"),
        "webcall": lambda: probe_http(f"http://{host}:{url_port('NEXT_PUBLIC_WEBCALL_URL', 8300)}/health"),
        "ollama": lambda: probe_http("http://127.0.0.1:11434/api/version"),
        "envoy": lambda: probe_http("http://127.0.0.1:9901/ready"),
        "admin-ui": lambda: probe_http(f"http://{host}:3000/login", timeout=15),
        "conversation-1": lambda: envoy_health(clusters.get(), "127.0.0.1:50051"),
        "conversation-2": lambda: envoy_health(clusters.get(), "127.0.0.1:50052"),
        "freeswitch": lambda: probe_esl(*esl),
        "gateway": lambda: probe_http(f"http://{gw_host}:{gw_port}/health"),
        "kamailio": lambda: (lambda a: probe_sip(*a) if a else None)(listen.get()),
    }
    return probes


class Watcher:
    def __init__(self, policy: Policy, proxy_factory, state_path: Path, schedule=None, clock=time.time, dispatch=None,
                 live_calls=lambda: None):
        self.policy = policy
        self.live_calls = live_calls
        self.proxy = proxy_factory  # one client per call: xmlrpc clients are not thread-safe
        self.state_path = state_path
        self.clock = clock
        self.schedule = schedule or self._timer
        self.dispatch = dispatch or self._thread
        self.lock = threading.RLock()
        self.misses: dict[str, int] = {}
        self.armed: dict[str, float] = {}  # name -> start time it first passed a probe
        self.alerted: dict[str, float] = {}
        self.deferred: dict[str, float] = {}  # name -> start time we already announced a deferred restart for

    def probe_once(self, probes: dict) -> None:
        proxy = self.proxy()
        for name, probe in probes.items():
            try:
                info = proxy.supervisor.getProcessInfo(name)
            except xmlrpc.client.Fault:
                continue
            start = float(info["start"])
            if info["statename"] != "RUNNING":
                self.misses.pop(name, None)
                continue
            ok = probe()
            if ok is None:
                self.misses.pop(name, None)  # "in a row": an unknown round breaks the streak
                continue  # can't tell, e.g. Envoy itself is down
            if ok:
                self.misses[name] = 0
                self.armed[name] = start
                continue
            if self.armed.get(name) != start:
                # Never answered since start: a slow starter or a wrong probe, so alert once, never restart.
                if self.clock() - start >= STARTUP_DEADLINE_S and self.alerted.get(name) != start:
                    self.alerted[name] = start
                    msg = f"{name} has not passed its heartbeat in {STARTUP_DEADLINE_S}s since start; check it"
                    log(msg)
                    notify(msg)
                continue
            n = self.misses[name] = self.misses.get(name, 0) + 1
            log(f"{name} missed heartbeat ({n}/{MISSES_TO_RESTART})")
            if n >= MISSES_TO_RESTART:
                self.misses[name] = 0
                self.dispatch(self.on_hang, name, start)

    def probe_loop(self, probes: dict, interval_s: float) -> None:
        while True:
            try:
                self.probe_once(probes)
            except Exception as exc:  # noqa: BLE001
                log(f"heartbeat round failed: {exc!r}")
            time.sleep(interval_s)

    @staticmethod
    def _thread(fn, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True).start()

    @staticmethod
    def _timer(delay: float, fn) -> None:
        t = threading.Timer(max(0.0, delay), fn)
        t.daemon = True
        t.start()

    def load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        self.policy.services = {name: ServiceState(**s) for name, s in raw.items()}

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({n: asdict(s) for n, s in self.policy.services.items()}))
        tmp.replace(self.state_path)

    def resume(self) -> None:
        now = self.clock()
        with self.lock:
            for name, s in self.policy.services.items():
                if s.pending_at is not None:
                    log(f"{name}: resuming pending {'trial' if s.pending_trial else 'restart'}")
                    self.schedule(s.pending_at - now, lambda n=name, due=s.pending_at: self.fire(n, due))
                if s.half_open_since is not None:
                    self.schedule(s.half_open_since + self.policy.window_s - now, lambda n=name: self.check_closed(n))

    def on_event(self, eventname: str, name: str, expected: str) -> None:
        with self.lock:
            if eventname == "PROCESS_STATE_EXITED" and expected == "0":
                action, delay = self.policy.on_exit(name, self.clock())
            elif eventname == "PROCESS_STATE_FATAL":
                action, delay = self.policy.on_fatal(name)
            else:
                return
            if action == "restart":
                log(f"{name} crashed; supervisord restarts it now")
                self.save()
                return
        self._recover(name, action, delay, fatal=eventname == "PROCESS_STATE_FATAL", what="crashed again")

    def on_hang(self, name: str, start_at: float) -> None:
        """A hung process never exits, so it is stopped here and counted as a crash."""
        try:
            info = self.proxy().supervisor.getProcessInfo(name)
        except xmlrpc.client.Fault:
            return
        if info["statename"] != "RUNNING" or float(info["start"]) != start_at:
            return
        if name not in LIVE_CALL_EXEMPT:
            calls = self.live_calls()
            if calls is None or calls > 0:
                self._defer(name, start_at, calls)
                return
        with self.lock:
            self.armed.pop(name, None)
            action, delay = self.policy.on_exit(name, self.clock())
        self._recover(name, "delay" if action == "restart" else action, delay, fatal=False, what="hung (no heartbeat)")

    def _defer(self, name: str, start_at: float, calls: int | None) -> None:
        why = "live-call count unavailable" if calls is None else f"{calls} live calls"
        log(f"{name} hung; restart deferred ({why})")
        with self.lock:
            if self.deferred.get(name) == start_at:
                return
            self.deferred[name] = start_at
        msg = f"{name} is hung but restart is deferred ({why}); it will retry while it stays hung"
        notify(msg)

    def _recover(self, name: str, action: str, delay: float, *, fatal: bool, what: str) -> None:
        with self.lock:
            s = self.policy.state(name)
            s.pending_at, s.pending_trial = self.clock() + delay, action == "open"
            s.stop_token = FATAL if fatal else None
            self.save()
        if not fatal:
            token = stop_service(self.proxy(), name)
            with self.lock:
                s.stop_token = token
                self.save()
        if action == "delay":
            log(f"{name} {what}; restarting in {delay:.0f}s")
        else:
            msg = f"{name} keeps failing; circuit open, next trial start in {delay:.0f}s"
            log(msg)
            notify(msg)
        due = s.pending_at
        self.schedule(delay, lambda: self.fire(name, due))

    def fire(self, name: str, due: float) -> None:
        with self.lock:
            s = self.policy.state(name)
            if s.pending_at != due:
                return  # already handled, or superseded
            proxy = self.proxy()
            info = proxy.supervisor.getProcessInfo(name)
            if s.stop_token == FATAL:
                ours = info["statename"] == FATAL
            else:
                ours = info["statename"] == "STOPPED" and (s.stop_token is None or abs(float(info["stop"]) - s.stop_token) < 1)
            trial = s.pending_trial
            s.pending_at, s.pending_trial, s.stop_token = None, False, None
            if ours:
                start_service(proxy, name)
                if trial:
                    s.half_open_since = self.clock()
                    self.schedule(self.policy.window_s + 1, lambda: self.check_closed(name))
            else:
                s.opens, s.crashes, s.half_open_since = 0, [], None  # a human took over
            self.save()
        log(f"{name}: {'trial start (half-open)' if trial else 'restarted after backoff'}" if ours
            else f"{name}: changed by hand since ({info['statename']}); leaving it alone")

    def check_closed(self, name: str) -> None:
        with self.lock:
            s = self.policy.state(name)
            if s.half_open_since is None:
                return
            remaining = s.half_open_since + self.policy.window_s - self.clock()
            if remaining > 0:  # timers can fire a hair early
                self.schedule(remaining + 1, lambda: self.check_closed(name))
                return
            self.policy.close_if_survived(name, self.clock())
            self.save()
        log(f"{name}: survived {self.policy.window_s:.0f}s after trial; circuit closed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-crashes", type=int, default=5)
    parser.add_argument("--window", type=float, default=600)
    parser.add_argument("--backoff-cap", type=float, default=300)
    parser.add_argument("--cooldown", type=float, default=300)
    parser.add_argument("--cooldown-cap", type=float, default=3600)
    parser.add_argument("--state", type=Path, default=Path.home() / ".yuviz" / "crash-watch.json")
    parser.add_argument("--no-heartbeat", action="store_true")
    args = parser.parse_args()

    policy = Policy(args.max_crashes, args.window, args.backoff_cap, args.cooldown, args.cooldown_cap)
    url = os.environ["SUPERVISOR_SERVER_URL"]
    dsn = dotenv("POSTGRES_DSN", f"postgresql://{os.environ.get('USER', '')}@localhost:5432/voiceai")
    watcher = Watcher(policy, lambda: rpc_proxy(url), args.state, live_calls=lambda: count_live_calls(dsn))
    watcher.load()
    watcher.resume()
    if not args.no_heartbeat:
        threading.Thread(target=watcher.probe_loop, args=(default_probes(), PROBE_INTERVAL_S), daemon=True).start()
    me = os.environ.get("SUPERVISOR_PROCESS_NAME")
    log(f"watching: backoff to {args.backoff_cap:.0f}s, circuit opens at {args.max_crashes} crashes in {args.window:.0f}s, "
        f"heartbeat {'off' if args.no_heartbeat else f'every {PROBE_INTERVAL_S}s, restart after {MISSES_TO_RESTART} misses'}")
    while True:
        sys.stdout.write("READY\n")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            return
        header = parse_tokens(line)
        payload = sys.stdin.read(int(header.get("len", 0)))
        first_line = payload.split("\n", 1)[0]
        event = parse_tokens(first_line) if first_line else {}
        name = event.get("processname", "")
        if name and name != me:
            try:
                watcher.on_event(header.get("eventname", ""), name, event.get("expected", ""))
            except Exception as exc:  # noqa: BLE001
                log(f"{name}: handling {header.get('eventname')} failed: {exc!r}")
        sys.stdout.write("RESULT 2\nOK")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
