import json
import os
import sqlite3
import struct
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
CFG = tomllib.loads((ROOT / "config.toml").read_text())
DEADLINE = time.time() + float(os.environ.get("MAX_MIN", "100000")) * 60
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
A58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
I58 = {c: i for i, c in enumerate(A58)}
CPI = bytes.fromhex("e445a52e51cb9a1d")
INTS = {"u8": 1, "i8": 1, "u16": 2, "i16": 2, "u32": 4, "i32": 4, "u64": 8, "i64": 8, "u128": 16, "i128": 16}

SCHEMA = """
CREATE TABLE IF NOT EXISTS creates(
  mint TEXT PRIMARY KEY, ts INTEGER, slot INTEGER, sig TEXT, creator TEXT, payer TEXT, curve TEXT,
  name TEXT, symbol TEXT, uri TEXT, quote TEXT, token_program TEXT, mayhem INTEGER, cashback INTEGER,
  holder INTEGER, fee_bps INTEGER, dev_q INTEGER, dev_tok INTEGER, oth_q INTEGER, oth_n INTEGER,
  payer_delta INTEGER, tx_fee INTEGER);
CREATE TABLE IF NOT EXISTS migr(mint TEXT PRIMARY KEY, ts INTEGER, sig TEXT, quote_amt INTEGER, pool TEXT, quote TEXT);
CREATE TABLE IF NOT EXISTS curves(mint TEXT PRIMARY KEY, complete INTEGER, vq INTEGER, rq INTEGER, rt INTEGER, creator TEXT);
CREATE TABLE IF NOT EXISTS sample(mint TEXT PRIMARY KEY, stratum TEXT, pages INTEGER, capped INTEGER);
CREATE TABLE IF NOT EXISTS trades(
  mint TEXT, ts INTEGER, slot INTEGER, user TEXT, buy INTEGER, q INTEGER, tok INTEGER, fee INTEGER, cfee INTEGER, venue INTEGER);
CREATE TABLE IF NOT EXISTS meta(
  mint TEXT PRIMARY KEY, ok INTEGER, twitter INTEGER, telegram INTEGER, website INTEGER, desc_len INTEGER,
  image TEXT, ath REAL, replies INTEGER, descr TEXT);
CREATE TABLE IF NOT EXISTS snap(mint TEXT PRIMARY KEY, ts INTEGER, dex TEXT, mcap REAL, liq REAL, vol24 REAL, txns24 INTEGER);
CREATE TABLE IF NOT EXISTS done(task TEXT, key TEXT, PRIMARY KEY(task, key));
CREATE TABLE IF NOT EXISTS targets(mint TEXT, addr TEXT, role TEXT, cfee INTEGER, n INTEGER, PRIMARY KEY(mint, addr, role));
CREATE TABLE IF NOT EXISTS funding(addr TEXT PRIMARY KEY, funder TEXT, ts INTEGER, sig TEXT, lamports INTEGER);
"""


def late():
    return time.time() > DEADLINE


def db():
    con = sqlite3.connect(ROOT / "pf.db", timeout=120)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def b58e(b):
    n, s = int.from_bytes(b, "big"), []
    while n:
        n, r = divmod(n, 58)
        s.append(A58[r])
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + "".join(reversed(s))


def b58d(s):
    n = 0
    for c in s:
        n = n * 58 + I58[c]
    return b"\0" * (len(s) - len(s.lstrip("1"))) + n.to_bytes((n.bit_length() + 7) // 8, "big")


def fetch(req, timeout):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class Limiter:
    def __init__(self, rps):
        self.gap = 1 / rps
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            self.next = max(self.next, now)
            delay = self.next - now
            self.next += self.gap
        if delay > 0:
            time.sleep(delay)


class Http:
    def __init__(self, rps):
        self.lim = Limiter(rps)

    def get(self, url, tries=6, timeout=60):
        for attempt in range(tries):
            self.lim.wait()
            try:
                return fetch(urllib.request.Request(url, headers=UA), timeout)
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504, 520, 522, 530):
                    return None
            except (OSError, ValueError):
                pass
            time.sleep(min(60, 2**attempt))
        return None


class Rpc:
    def __init__(self):
        self.url = CFG["rpc"]["url"] + os.environ["HELIUS_KEY"]
        self.lim = Limiter(CFG["rpc"]["rps"])
        self.credits = 0
        self.lock = threading.Lock()

    def call(self, method, params, cost=1):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        for attempt in range(10):
            self.lim.wait()
            j = None
            try:
                j = fetch(urllib.request.Request(self.url, data=body, headers={**UA, "Content-Type": "application/json"}), 300)
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"{method} http {e.code}") from None
            except (OSError, ValueError):
                pass
            if j is not None and "error" not in j:
                with self.lock:
                    self.credits += cost
                return j["result"]
            if j is not None and j["error"].get("code") not in (-32429, -32005, -32603, -32000, 429):
                raise RuntimeError(f"{method} {j['error']}")
            time.sleep(min(60, 2**attempt))
        raise RuntimeError(f"{method} gave up")

    def gtfa(self, addr, filters=None, full=True, max_pages=0, limit=1000):
        opt = {"transactionDetails": "full" if full else "signatures", "sortOrder": "asc", "limit": limit,
               "filters": {"status": "succeeded", "tokenAccounts": "none", **(filters or {})}}
        if full:
            opt.update(encoding="json", maxSupportedTransactionVersion=1)
        pages = 0
        while True:
            r = self.call("getTransactionsForAddress", [addr, opt], 0)
            d = r.get("data") or []
            with self.lock:
                self.credits += max(10, -(-len(d) // 100) * 10) if full else 10
            pages += 1
            yield d
            if not r.get("paginationToken") or not d or (max_pages and pages >= max_pages):
                return
            opt["paginationToken"] = r["paginationToken"]


class Idl:
    def __init__(self, url):
        j = fetch(urllib.request.Request(url, headers=UA), 60)
        self.types = {t["name"]: t["type"] for t in j["types"]}
        self.events = {bytes(e["discriminator"]): e["name"] for e in j["events"]}

    def decode(self, b):
        name = self.events.get(b[:8])
        if name is None:
            return None
        out, o = {"_": name}, 8
        for f in self.types[name]["fields"]:
            if o >= len(b):
                break
            try:
                out[f["name"]], o = self.read(f["type"], b, o)
            except (IndexError, struct.error, KeyError):
                break
        return out

    def read(self, t, b, o):
        if isinstance(t, str):
            if t in INTS:
                n = INTS[t]
                if o + n > len(b):
                    raise IndexError
                return int.from_bytes(b[o:o + n], "little", signed=t[0] == "i"), o + n
            if t == "bool":
                return b[o] == 1, o + 1
            if t == "pubkey":
                if o + 32 > len(b):
                    raise IndexError
                return b58e(b[o:o + 32]), o + 32
            n = struct.unpack_from("<I", b, o)[0]
            return b[o + 4:o + 4 + n].decode("utf-8", "replace"), o + 4 + n
        if "vec" in t:
            n, o, v = struct.unpack_from("<I", b, o)[0], o + 4, []
            for _ in range(n):
                x, o = self.read(t["vec"], b, o)
                v.append(x)
            return v, o
        if "option" in t:
            return (None, o + 1) if b[o] == 0 else self.read(t["option"], b, o + 1)
        if "array" in t:
            v = []
            for _ in range(t["array"][1]):
                x, o = self.read(t["array"][0], b, o)
                v.append(x)
            return v, o
        d = self.types[t["defined"]["name"]]
        if d["kind"] == "enum":
            return d["variants"][b[o]]["name"], o + 1
        r = {}
        for f in d["fields"]:
            r[f["name"]], o = self.read(f["type"], b, o)
        return r, o


class Decoder:
    def __init__(self):
        self.idl = {CFG["addr"][k]: Idl(CFG["idl"][k]) for k in ("pump", "amm")}

    def events(self, tx):
        m, msg = tx["meta"], tx["transaction"]["message"]
        la = m.get("loadedAddresses") or {}
        keys = msg["accountKeys"] + la.get("writable", []) + la.get("readonly", [])
        out = []
        for g in m.get("innerInstructions") or []:
            for ix in g["instructions"]:
                idl = self.idl.get(keys[ix["programIdIndex"]])
                if idl is not None and ix.get("data"):
                    d = b58d(ix["data"])
                    if d[:8] == CPI:
                        e = idl.decode(d[8:])
                        if e is not None:
                            out.append(e)
        return out, keys


_ISSUE = {}


def status(name, text):
    print(text, flush=True)
    tok, repo = os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not tok or not repo:
        return
    h = {**UA, "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"}
    api = f"https://api.github.com/repos/{repo}/issues"

    def req(method, url, body=None):
        return fetch(urllib.request.Request(url, method=method, headers=h, data=json.dumps(body).encode() if body else None), 20)

    try:
        num = _ISSUE.get(name)
        if num is None:
            num = next((i["number"] for i in req("GET", api + "?state=open&per_page=100") if i["title"] == f"status: {name}"), None)
            if num is None:
                num = req("POST", api, {"title": f"status: {name}", "body": text})["number"]
            _ISSUE[name] = num
        req("PATCH", f"{api}/{num}", {"body": f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC\n\n{text}"})
    except Exception as x:
        print("status update failed", repr(x), file=sys.stderr, flush=True)
