"""Offline checks for the credentials preflight: python test_check_credentials.py"""
import io
from contextlib import redirect_stdout
import requests
from check_credentials import main, local_problems, explain, KEY_VAR, SECRET_VAR

K, S = "PKTESTKEY123", "s3cr3t-VALUE-xyz"
resp = lambda code: type("R", (), {"status_code": code})()

def run(env, get):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(env=env, get=get)
    return rc, buf.getvalue()

# --- local problems, no network ---
assert local_problems(K, S) == []
assert len(local_problems(None, S)) == 1 and KEY_VAR in local_problems(None, S)[0]
assert len(local_problems("", "")) == 2
assert "whitespace" in local_problems(K + "\n", S)[0] and "whitespace" in local_problems(K, " " + S)[0]

def no_network(*a, **k): raise AssertionError("must not call the network when env is invalid")
assert run({}, no_network)[0] == 1
assert run({KEY_VAR: K + "\n", SECRET_VAR: S}, no_network)[0] == 1

# --- accepted ---
rc, out = run({KEY_VAR: K, SECRET_VAR: S}, lambda url, **k: resp(200))
assert rc == 0 and "accepted" in out

# --- rejected: 401 on either endpoint fails, with the reason ---
rc, out = run({KEY_VAR: K, SECRET_VAR: S}, lambda url, **k: resp(401))
assert rc == 1 and "PAPER key pair" in out and "HTTP 401" in out
rc, out = run({KEY_VAR: K, SECRET_VAR: S}, lambda url, **k: resp(401 if "data." in url else 200))
assert rc == 1 and "market data: HTTP 401" in out and "paper account: HTTP 200" in out

# --- network failure and server errors fail with a distinct message ---
def boom(url, **k): raise requests.ConnectionError("x")
rc, out = run({KEY_VAR: K, SECRET_VAR: S}, boom)
assert rc == 1 and "request failed (ConnectionError)" in out
assert "server error" in explain(503) and "rate limited" in explain(429)

# --- values are never printed ---
for get in (lambda url, **k: resp(200), lambda url, **k: resp(401), boom):
    _, out = run({KEY_VAR: K, SECRET_VAR: S}, get)
    assert K not in out and S not in out
_, out = run({KEY_VAR: K + "\n", SECRET_VAR: S}, no_network)
assert K not in out and S not in out
print("credentials check ok")
