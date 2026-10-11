"""Bounded, cancellable and paced HTTPS exchanges with the HubSpot API.

Standard library only. Each exchange runs in a helper thread so cancellation,
Stop and the activation deadline can shut the socket down at once. Requests
made with one access token are paced to at most max_requests_per_10s in any
ten-second window, and search requests also to max_search_per_sec in any
second, across every node of this process. Reads are retried after HTTP 429,
5xx or a lost connection; writes only after HTTP 429, which HubSpot did not
process. Ambiguous writes are reported, never replayed.
"""

from collections import deque
import hashlib
import http.client
import json
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from . import logic

CHUNK = 65536
USER_AGENT = "BloxSmith-HubSpot/0.1.0"
THREAD_NAME = "hubspot-http"
GENERAL_WINDOW = 10.0
SEARCH_WINDOW = 1.0
_PACE_LOCK = threading.Lock()
_PACE = {}


class Failure(Exception):
    """A content-free transport outcome: code and the phase it happened in."""

    def __init__(self, code, phase):
        super().__init__(code)
        self.code, self.phase = code, phase


def reject_constant(_value):
    raise ValueError("Non-finite JSON number.")


def remaining(deadline, phase):
    left = deadline - time.monotonic()
    if left <= 0:
        raise Failure("timeout", phase)
    return max(0.001, left)


def pace_slot(key, rate, window, now=None):
    """Take a request slot now (0.0), or return how long to wait for one.

    The windows are shared by every session of the process, so several nodes
    using the same token respect HubSpot's per-account limits together.
    """
    with _PACE_LOCK:
        now = time.monotonic() if now is None else now
        times = _PACE.get(key)
        if times is None:
            if len(_PACE) >= 256:
                for stale in [item for item, stamps in _PACE.items() if not stamps or now - stamps[-1] >= GENERAL_WINDOW]:
                    del _PACE[stale]
            times = _PACE[key] = deque()
        while times and now - times[0] >= window:
            times.popleft()
        if len(times) < rate:
            times.append(now)
            return 0.0
        return max(0.001, window - (now - times[-rate]))


def retry_after(found):
    value = found.get("retry-after", "").strip()
    return int(value) if value.isdigit() and len(value) <= 6 else None


def perform(state, origin, ca_file, method, path, headers, body, deadline, limit):
    """One request/response without redirects, cookies, proxies or decompression."""
    parsed = urlsplit(origin)
    host, port = parsed.hostname, parsed.port
    try:
        if parsed.scheme == "https":
            context = ssl.create_default_context(cafile=ca_file or None)
            conn = http.client.HTTPSConnection(host, port or 443, timeout=remaining(deadline, "connect"), context=context)
        else:
            conn = http.client.HTTPConnection(host, port or 80, timeout=remaining(deadline, "connect"))
        state["conn"] = conn
        conn.connect()
        if state.get("aborted"):
            raise Failure("network", "connect")
        state["phase"] = "send"
        conn.putrequest(method, path, skip_host=False, skip_accept_encoding=True)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.putheader("Content-Length", str(len(body or b"")))
        conn.sock.settimeout(remaining(deadline, "send"))
        conn.endheaders(message_body=body or None)
        state["phase"] = "receive"
        conn.sock.settimeout(remaining(deadline, "receive"))
        response = conn.getresponse()
        chunks, total = [], 0
        while total <= limit:
            if conn.sock is not None:
                conn.sock.settimeout(remaining(deadline, "receive"))
            piece = response.read1(min(CHUNK, limit + 1 - total))
            if not piece:
                break
            total += len(piece)
            chunks.append(piece)
        found = {key.lower(): value for key, value in response.getheaders()}
        result = {"status": response.status, "retry_after": retry_after(found), "body": None, "body_error": None}
        media = found.get("content-type", "").split(";", 1)[0].strip().lower()
        if total > limit:
            result["body_error"] = "too_large"
        elif found.get("content-encoding", "identity").strip().lower() not in ("identity", ""):
            result["body_error"] = "encoded"
        elif total == 0:
            result["body_error"] = "empty"
        elif media != "application/json":
            result["body_error"] = "not_json"
        else:
            try:
                result["body"] = json.loads(b"".join(chunks).decode("utf-8", errors="strict"),
                                            parse_constant=reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                result["body_error"] = "invalid_json"
        return result
    except Failure:
        raise
    except ssl.SSLCertVerificationError:
        raise Failure("tls", state["phase"]) from None
    except ssl.SSLError:
        raise Failure("tls" if state["phase"] == "connect" else "network", state["phase"]) from None
    except (socket.timeout, TimeoutError):
        raise Failure("timeout", state["phase"]) from None
    except (OSError, http.client.HTTPException, ValueError, AttributeError):
        raise Failure("network", state["phase"]) from None
    finally:
        conn = state.get("conn")
        if conn is not None:
            conn.close()


def abort(state):
    """Shut the socket down from the supervising thread; the helper thread then fails fast."""
    state["aborted"] = True
    conn = state.get("conn")
    sock = getattr(conn, "sock", None) if conn is not None else None
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def exchange(origin, ca_file, method, path, headers, body, *, deadline, cancel, limit):
    """Run one request in a helper thread and supervise cancellation and the deadline."""
    state, box = {"phase": "connect", "conn": None}, {}

    def target():
        try:
            box["result"] = perform(state, origin, ca_file, method, path, headers, body, deadline, limit)
        except Failure as failure:
            box["failure"] = failure
        except Exception:  # Defensive: an unexpected library error is a transport failure.
            box["failure"] = Failure("network", state["phase"])

    worker = threading.Thread(target=target, name=THREAD_NAME, daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(0.05)
        if not worker.is_alive():
            break
        if cancel():
            phase = state["phase"]
            abort(state)
            worker.join(2)
            raise Failure("cancelled", phase)
        if time.monotonic() >= deadline + 0.5:
            phase = state["phase"]
            abort(state)
            worker.join(2)
            raise Failure("timeout", phase)
    if "failure" in box:
        raise box["failure"]
    return box["result"]


class Session:
    """One activation or watch poll: one deadline, one token, paced and bounded requests."""

    def __init__(self, config, token, cancel=lambda: False):
        self.config, self.token = config, token
        self.cancel = cancel if callable(cancel) else (lambda: False)
        self.deadline = time.monotonic() + config["timeout_sec"]
        self.secrets = [token]
        self.requests = 0
        # The pacing key identifies the account through a digest of its token; the token itself is never stored.
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        self.general_key = ("general", config["api_base_url"], digest)
        self.search_key = ("search", config["api_base_url"], digest)

    def pause(self, seconds):
        """Cancellation-aware wait before a paced request or a retry."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.cancel():
                logic.fail("cancelled", logic.MESSAGES["cancelled_before_send"])
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))

    def paced(self, key, rate, window):
        """Wait for a slot in one rate window, within the operation deadline."""
        while True:
            wait = pace_slot(key, rate, window)
            if wait <= 0:
                return
            if time.monotonic() + wait > self.deadline - 0.2:
                logic.fail("timeout")
            self.pause(wait)

    def send(self, method, path, headers, body, *, mutation, search):
        """One exchange mapped to a structured outcome; never raises a raw library error."""
        if self.cancel():
            logic.fail("cancelled", logic.MESSAGES["cancelled_before_send"])
        if self.deadline - time.monotonic() <= 0.2:
            logic.fail("timeout")
        if search:
            self.paced(self.search_key, self.config["max_search_per_sec"], SEARCH_WINDOW)
        self.paced(self.general_key, self.config["max_requests_per_10s"], GENERAL_WINDOW)
        if self.cancel():
            logic.fail("cancelled", logic.MESSAGES["cancelled_before_send"])
        self.requests += 1
        try:
            return exchange(self.config["api_base_url"], self.config["ca_file"], method, path, headers, body,
                            deadline=self.deadline, cancel=self.cancel, limit=self.config["max_response_bytes"])
        except Failure as failure:
            after_send = failure.phase == "receive"
            uncertain = mutation and after_send
            if failure.code == "tls":
                logic.fail("tls")
            if failure.code == "timeout":
                logic.fail("timeout", logic.MESSAGES["timeout_uncertain" if uncertain else "timeout"], uncertain=uncertain)
            if failure.code == "cancelled":
                logic.fail("cancelled", logic.MESSAGES["cancelled" if uncertain else "cancelled_before_send"],
                           uncertain=uncertain)
            logic.fail("network", logic.MESSAGES["network_uncertain" if uncertain else "network"], uncertain=uncertain,
                       retryable=not mutation)

    def retrying(self, attempt, *, mutation):
        """Run attempt() with bounded retries: 429 always; 5xx and lost connections for reads only."""
        attempts = 0
        while True:
            attempts += 1
            try:
                return attempt()
            except logic.HubSpotError as exc:
                exc.attempts = attempts
                retryable = exc.code == "rate_limited" or (not mutation and (
                    exc.code == "provider_unavailable" or getattr(exc, "retryable", False)))
                wait = exc.retry_after if exc.retry_after is not None else min(2 ** (attempts - 1), 8)
                if (retryable and attempts <= self.config["max_retries"] and wait <= self.config["max_retry_wait_sec"]
                        and time.monotonic() + wait + 1 < self.deadline):
                    self.pause(wait)
                    continue
                raise

    def request(self, method, path, query=None, payload=None, *, mutation, search=False, empty=False):
        """Call <api_base_url><path>[?query]; return (HTTP status, JSON body or None) or raise a coded error.

        empty=True accepts a bodyless 204 receipt, as batch archive returns.
        """
        target = path + ("?" + query if query else "")
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json", "Accept-Encoding": "identity",
                   "User-Agent": USER_AGENT, "Connection": "close"}
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"

        def attempt():
            outcome = self.send(method, target, headers, payload, mutation=mutation, search=search)
            status = outcome["status"]
            if 200 <= status < 300:
                if empty and status == 204 and outcome["body_error"] == "empty":
                    return status, None
                if outcome["body_error"] or not isinstance(outcome["body"], dict):
                    logic.fail("protocol", logic.MESSAGES["protocol_uncertain"] if mutation else None, uncertain=mutation)
                return status, outcome["body"]
            if status >= 500 and outcome["body_error"]:
                logic.fail("provider_unavailable", logic.MESSAGES["provider_unavailable_uncertain" if mutation else
                                                                  "provider_unavailable"], uncertain=mutation, http_status=status)
            logic.provider_failure(status, outcome["body"], self.secrets, mutation=mutation,
                                   retry_after=outcome["retry_after"])

        return self.retrying(attempt, mutation=mutation)

    def call(self, method, path, query=None, payload=None, *, mutation, search=False):
        """Like request(), for exchanges whose only success is a complete JSON document."""
        status, body = self.request(method, path, query, payload, mutation=mutation, search=search)
        if status == 207:
            logic.fail("protocol", logic.MESSAGES["protocol_uncertain"] if mutation else None, uncertain=mutation)
        return body
