import base64
import json
import random
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from lib import CFG, Decoder, Http, Rpc, b58e, db, late, status

W, A, S, U = CFG["window"], CFG["addr"], CFG["sample"], CFG["urls"]
T0 = int(datetime.fromisoformat(W["start"]).replace(tzinfo=timezone.utc).timestamp())
DAYS = [T0 + 86400 * i for i in range(W["days"])]
SOL = {A["wsol"], "11111111111111111111111111111111", None}
OUT = Path(__file__).parent / "out"


def day_str(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def q_of(e, quote):
    return e.get("sol_amount", 0) if quote == A["wsol"] else e.get("quote_amount", 0)


def creates_of(dec, tx):
    evs, keys = dec.events(tx)
    m, out = tx["meta"], []
    for c in evs:
        if c["_"] != "CreateEvent":
            continue
        mint, signer = c["mint"], c["user"]
        creator = c.get("creator") or signer
        quote = A["wsol"] if c.get("quote_mint") in SOL else c["quote_mint"]
        dev_q = dev_t = oth_q = 0
        oth = set()
        for t in evs:
            if t["_"] == "TradeEvent" and t.get("mint") == mint and t.get("is_buy"):
                if t["user"] in (creator, signer):
                    dev_q += q_of(t, quote)
                    dev_t += t["token_amount"]
                else:
                    oth_q += q_of(t, quote)
                    oth.add(t["user"])
        out.append((mint, tx["blockTime"], tx["slot"], tx["transaction"]["signatures"][0], creator, keys[0],
                    c["bonding_curve"], c["name"][:64], c["symbol"][:32], c["uri"][:200], quote, c.get("token_program"),
                    int(bool(c.get("is_mayhem_mode"))), int(bool(c.get("is_cashback_enabled"))),
                    int(bool(c.get("is_holder_reward"))), c.get("creator_fee_bps"), dev_q, dev_t, oth_q, len(oth),
                    m["preBalances"][0] - m["postBalances"][0], m["fee"]))
    return out


def run(name, fn, items, write, rpc=None, every=250, workers=None):
    con, n = db(), 0
    with ThreadPoolExecutor(workers or CFG["rpc"]["workers"]) as ex:
        futs = [ex.submit(fn, x) for x in items]
        for f in as_completed(futs):
            write(con, f.result())
            n += 1
            if n % every == 0 or n == len(items):
                con.commit()
                status(name, f"{n:,}/{len(items):,} done | credits {rpc.credits if rpc else 0:,}")
            if late():
                for g in futs:
                    g.cancel()
                break
    con.commit()
    status(name, f"{n:,}/{len(items):,} done{' (deadline)' if late() else ''} | credits {rpc.credits if rpc else 0:,}")


def smoke():
    OUT.mkdir(exist_ok=True)
    rpc, dec, http, r = Rpc(), Decoder(), Http(2), {}
    t = time.time()
    rpc.call("getSlot", [])
    r["rpc_ms"] = round((time.time() - t) * 1000)
    now = int(time.time())
    t = time.time()
    txs = [tx for p in rpc.gtfa(A["mint_auth"], {"blockTime": {"gte": now - 3600, "lt": now - 1800}}) for tx in p]
    r["gtfa_30min"] = {"txs": len(txs), "sec": round(time.time() - t, 1), "credits": rpc.credits}
    t = time.time()
    rows = [x for tx in txs for x in creates_of(dec, tx)]
    r["decode"] = {"creates": len(rows), "ms_per_tx": round((time.time() - t) * 1000 / max(1, len(txs)), 2)}
    rent = sorted(x[20] - x[21] for x in rows if x[16] == 0)
    if rent:
        r["rent_nodev_lamports"] = {k: rent[int(q * (len(rent) - 1))] for k, q in (("p05", .05), ("p25", .25), ("p50", .5), ("p75", .75), ("p95", .95))}
        r["rent_nodev_n"] = len(rent)
    r["dev_buy_share"] = round(sum(1 for x in rows if x[16] > 0) / max(1, len(rows)), 3)
    r["quotes"] = Counter(x[10] for x in rows).most_common(5)
    r["mayhem"] = sum(x[12] for x in rows)
    r["token_programs"] = Counter(x[11] for x in rows).most_common(3)
    if rows:
        sig = next(rpc.gtfa(rows[0][0], full=False))
        r["sig_fields"] = sorted(sig[0]) if sig else None
        r["sig_n_first_mint"] = len(sig)
    mig = [tx for p in rpc.gtfa(A["migr_fee"], {"blockTime": {"gte": now - 7200, "lt": now - 600}}) for tx in p]
    ev = Counter(e["_"] for tx in mig for e in dec.events(tx)[0])
    r["migr_2h"] = {"txs": len(mig), "events": dict(ev)}
    mints = [x[0] for x in rows[:30]]
    for name, url in (("dex", U["dex"] + ",".join(mints)), ("pf", U["pf"] + mints[0]), ("gecko", U["gecko"] + mints[0])):
        t = time.time()
        j = http.get(url)
        r[name] = {"ok": j is not None, "ms": round((time.time() - t) * 1000),
                   "keys": sorted(j)[:60] if isinstance(j, dict) else (len(j) if isinstance(j, list) else None)}
    r["credits_total"] = rpc.credits
    (OUT / "smoke.json").write_text(json.dumps(r, indent=1))
    print(json.dumps(r, indent=1))


def ledger():
    rpc, dec, con = Rpc(), Decoder(), db()
    done = {k for (k,) in con.execute("SELECT key FROM done WHERE task='ledger'")}
    todo = [d for d in DAYS if str(d) not in done]
    pages = Counter()

    def day(d):
        rows, ok = [], True
        for page in rpc.gtfa(A["mint_auth"], {"blockTime": {"gte": d, "lt": d + 86400}}):
            for tx in page:
                rows += creates_of(dec, tx)
            pages[d] += 1
            if sum(pages.values()) % 25 == 0:
                print(f"pages {sum(pages.values())} credits {rpc.credits:,}", flush=True)
            if late():
                ok = False
                break
        return d, rows, ok

    with ThreadPoolExecutor(CFG["rpc"]["workers"]) as ex:
        for f in as_completed([ex.submit(day, d) for d in todo]):
            d, rows, ok = f.result()
            con.executemany(f"INSERT OR REPLACE INTO creates VALUES({','.join('?' * 22)})", rows)
            if ok:
                con.execute("INSERT OR IGNORE INTO done VALUES('ledger', ?)", (str(d),))
            con.commit()
            n = con.execute("SELECT COUNT(*) FROM done WHERE task='ledger'").fetchone()[0]
            status("ledger", f"{day_str(d)} creates {len(rows):,} complete={ok} | days done {n}/{len(DAYS)} | credits {rpc.credits:,}")


def migr():
    rpc, dec, con = Rpc(), Decoder(), db()
    n, t1 = 0, int(time.time()) - 3600
    for page in rpc.gtfa(A["migr_fee"], {"blockTime": {"gte": T0, "lt": t1}}):
        rows = [(e["mint"], e["timestamp"], tx["transaction"]["signatures"][0], e["sol_amount"], e["pool"], e.get("quote_mint"))
                for tx in page for e in dec.events(tx)[0] if e["_"] == "CompletePumpAmmMigrationEvent"]
        con.executemany("INSERT OR REPLACE INTO migr VALUES(?,?,?,?,?,?)", rows)
        con.commit()
        n += len(rows)
    status("migr", f"migrations {n:,} through {day_str(t1)} | credits {rpc.credits:,}")


def curves():
    rpc, con = Rpc(), db()
    rows = con.execute("SELECT mint, curve FROM creates WHERE mint NOT IN (SELECT mint FROM curves)").fetchall()

    def job(ch):
        v = rpc.call("getMultipleAccounts", [[c for _, c in ch], {"encoding": "base64"}])["value"]
        out = []
        for (mint, _), a in zip(ch, v):
            if a is None:
                out.append((mint, None, None, None, None, None))
                continue
            b = base64.b64decode(a["data"][0])
            vq, rt, rq = (int.from_bytes(b[i:i + 8], "little") for i in (16, 24, 32))
            out.append((mint, b[48], vq, rq, rt, b58e(b[49:81])))
        return out

    run("curves", job, [rows[i:i + 100] for i in range(0, len(rows), 100)],
        lambda con, res: con.executemany("INSERT OR REPLACE INTO curves VALUES(?,?,?,?,?,?)", res), rpc, every=500)


def sample():
    con = db()
    if con.execute("SELECT COUNT(*) FROM sample").fetchone()[0]:
        print("sample exists")
        return
    days = con.execute("SELECT COUNT(*) FROM done WHERE task='ledger'").fetchone()[0]
    missing = con.execute("SELECT COUNT(*) FROM creates WHERE mint NOT IN (SELECT mint FROM curves)").fetchone()[0]
    if days < len(DAYS) or missing:
        sys.exit(f"sample blocked: ledger days {days}/{len(DAYS)}, curves missing {missing:,}")
    rnd = random.Random(S["seed"])
    grads = [m for (m,) in con.execute("SELECT mint FROM curves WHERE complete=1 ORDER BY mint")]
    non = [m for (m,) in con.execute("SELECT mint FROM curves WHERE complete=0 ORDER BY mint")]
    g, n = rnd.sample(grads, min(len(grads), S["grads"])), rnd.sample(non, min(len(non), S["non"]))
    con.executemany("INSERT INTO sample(mint, stratum) VALUES(?,?)", [(m, "g") for m in g] + [(m, "n") for m in n])
    con.commit()
    status("sample", f"graduates {len(g):,} of {len(grads):,} | non-graduates {len(n):,} of {len(non):,}")


def trades():
    rpc, dec, con = Rpc(), Decoder(), db()
    rows = con.execute("""SELECT s.mint, m.pool, c.quote FROM sample s JOIN creates c USING(mint)
        LEFT JOIN migr m USING(mint) WHERE s.pages IS NULL""").fetchall()

    def job(r):
        mint, pool, quote = r
        out, pages, full = [], 0, False
        for page in rpc.gtfa(mint, max_pages=S["max_pages"]):
            pages += 1
            full = len(page) >= 1000
            for tx in page:
                for e in dec.events(tx)[0]:
                    if e["_"] == "TradeEvent" and e.get("mint") == mint:
                        out.append((mint, e["timestamp"], tx["slot"], e["user"], int(e["is_buy"]), q_of(e, quote),
                                    e["token_amount"], e.get("fee", 0), e.get("creator_fee", 0), 0))
                    elif e["_"] in ("BuyEvent", "SellEvent") and pool and e.get("pool") == pool:
                        b = e["_"] == "BuyEvent"
                        out.append((mint, e["timestamp"], tx["slot"], e["user"], int(b),
                                    e["quote_amount_in"] if b else e["quote_amount_out"],
                                    e["base_amount_out"] if b else e["base_amount_in"],
                                    e.get("protocol_fee", 0) + e.get("lp_fee", 0), e.get("coin_creator_fee", 0), 1))
        return mint, out, pages, int(full and pages >= S["max_pages"])

    def write(con, res):
        mint, out, pages, capped = res
        con.executemany("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?)", out)
        con.execute("UPDATE sample SET pages=?, capped=? WHERE mint=?", (pages, capped, mint))

    run("trades", job, rows, write, rpc, every=200)


def meta():
    pf_http, ipfs, con = Http(CFG["http"]["pf_rps"]), Http(CFG["http"]["ipfs_rps"]), db()
    rows = con.execute("SELECT s.mint, c.uri FROM sample s JOIN creates c USING(mint) WHERE s.mint NOT IN (SELECT mint FROM meta)").fetchall()

    def uri_json(uri):
        if not uri or not uri.startswith("http"):
            return {}
        urls = [uri]
        if "/ipfs/" in uri:
            urls += [g + uri.split("/ipfs/", 1)[1] for g in U["gateways"] if not uri.startswith(g)]
        for u in urls:
            j = ipfs.get(u, tries=2, timeout=20)
            if isinstance(j, dict):
                return j
        return {}

    def job(r):
        mint, uri = r
        p = pf_http.get(U["pf"] + mint)
        p = p if isinstance(p, dict) else {}
        m = uri_json(uri)
        ok = (1 if p else 0) | (2 if m else 0)
        desc = m.get("description") or ""
        return (mint, ok, int(bool(p.get("twitter") or m.get("twitter"))), int(bool(m.get("telegram"))),
                int(bool(p.get("website") or m.get("website"))), len(desc), p.get("image_uri") or m.get("image"),
                p.get("ath_market_cap"), p.get("reply_count"), desc[:500])

    run("meta", job, rows, lambda con, res: con.execute("INSERT OR REPLACE INTO meta VALUES(?,?,?,?,?,?,?,?,?,?)", res),
        every=250, workers=4)


def snap():
    http, con = Http(CFG["http"]["dex_rps"]), db()
    mints = [m for (m,) in con.execute("""SELECT mint FROM curves WHERE complete=1 UNION SELECT mint FROM sample
        EXCEPT SELECT mint FROM snap""")]
    now = int(time.time())

    def job(ch):
        best = {}
        for p in http.get(U["dex"] + ",".join(ch)) or []:
            m, liq = (p.get("baseToken") or {}).get("address"), (p.get("liquidity") or {}).get("usd") or 0
            if m in ch and (m not in best or liq > best[m][4]):
                best[m] = (m, now, p.get("dexId"), p.get("marketCap") or p.get("fdv"), liq,
                           (p.get("volume") or {}).get("h24"), sum(((p.get("txns") or {}).get("h24") or {}).values()))
        return list(best.values()) + [(m, now, None, 0, 0, 0, 0) for m in ch if m not in best]

    run("snap", job, [mints[i:i + 30] for i in range(0, len(mints), 30)],
        lambda con, res: con.executemany("INSERT OR REPLACE INTO snap VALUES(?,?,?,?,?,?,?)", res), every=100, workers=4)


def report():
    import report as r
    r.main()


def count():
    con = db()
    for t in ("creates", "migr", "curves", "sample", "trades", "meta", "snap"):
        print(t, con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    print("ledger days", [day_str(int(k)) for (k,) in con.execute("SELECT key FROM done WHERE task='ledger' ORDER BY key")])


if __name__ == "__main__":
    globals()[sys.argv[1]]()
