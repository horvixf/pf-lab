import bisect
import json
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from lib import CFG, db

WSOL = CFG["addr"]["wsol"]
LAM = 1e9
TEST = int(datetime.fromisoformat(CFG["window"]["test_from"]).replace(tzinfo=timezone.utc).timestamp())
OUT = Path(__file__).parent / "out"
RND = random.Random(7)
BOOT = 1000


def pct(x):
    return None if x is None else round(100 * x, 3)


def wilson(k, n, z=1.96):
    if not n:
        return None
    p, d = k / n, 1 + z * z / n
    c, h = p + z * z / (2 * n), z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return {"n": n, "k": k, "pct": pct(p), "ci95": [pct((c - h) / d), pct((c + h) / d)]}


def cluster_rate(pairs):
    agg = defaultdict(lambda: [0, 0])
    for g, y in pairs:
        agg[g][0] += y
        agg[g][1] += 1
    y, n, g = sum(a[0] for a in agg.values()), sum(a[1] for a in agg.values()), len(agg)
    if n == 0 or g < 2:
        return None
    r = y / n
    se = math.sqrt(g / (g - 1) * sum((a[0] - r * a[1]) ** 2 for a in agg.values())) / n
    return {"n": n, "k": y, "creators": g, "pct": pct(r), "ci95": [pct(r - 1.96 * se), pct(r + 1.96 * se)]}


def ztest(k1, n1, k2, n2):
    if not n1 or not n2:
        return None
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) or 1e-12
    z = (k1 / n1 - k2 / n2) / se
    return {"a": wilson(k1, n1), "b": wilson(k2, n2), "z": round(z, 2), "p": float(f"{math.erfc(abs(z) / math.sqrt(2)):.2g}")}


def bucket(v, edges, labels):
    return labels[bisect.bisect_right(edges, v)]


def universe(con):
    rows = con.execute("""SELECT c.mint, c.ts, c.slot, c.creator, c.payer, lower(trim(c.name)), c.quote, c.mayhem,
        c.cashback, c.holder, c.dev_q, c.oth_n, c.payer_delta, c.tx_fee, k.complete, m.ts, m.quote_amt
        FROM creates c LEFT JOIN curves k USING(mint) LEFT JOIN migr m USING(mint)""").fetchall()
    per_creator = Counter(r[3] for r in rows)
    names = defaultdict(list)
    for r in sorted(rows, key=lambda r: r[1]):
        names[r[5]].append(r[1])
    U = {}
    for (mint, ts, slot, creator, payer, name, quote, mayhem, cashback, holder, dev_q, oth_n, pdelta, fee,
         complete, mts, mq) in rows:
        sol = quote == WSOL
        grad = complete == 1
        gsec = mts - ts if (grad and mts) else None
        t = names[name]
        prior = bisect.bisect_left(t, ts) - bisect.bisect_left(t, ts - 86400)
        self_bond = sol and dev_q >= 84 * LAM
        mayhem_low = bool(grad and mayhem and (mq or 0) < 10 * LAM)
        instant = gsec is not None and gsec <= 5
        U[mint] = {
            "ts": ts, "slot": slot, "creator": creator, "payer": payer, "sol": sol, "mayhem": mayhem,
            "plain": sol and not mayhem and not cashback and not holder, "dev": dev_q / LAM if sol else None,
            "oth_n": oth_n, "pdelta": pdelta, "fee": fee, "grad": grad, "curve_known": complete is not None,
            "migr": mts is not None, "gsec": gsec, "self_bond": self_bond, "mayhem_low": mayhem_low, "instant": instant,
            "organic_u": grad and sol and not self_bond and not mayhem_low and not instant and dev_q < LAM and oth_n == 0,
            "launches": per_creator[creator], "copycat": prior > 0, "hour": datetime.fromtimestamp(ts, timezone.utc).hour,
            "test": ts >= TEST,
        }
    return U


def sampled(con, U):
    S = {}
    for mint, stratum, capped in con.execute("SELECT mint, stratum, capped FROM sample WHERE pages IS NOT NULL"):
        if mint in U:
            S[mint] = {"stratum": stratum, "capped": capped, "cfee": 0, "buy": 0, "sell": 0, "create_buy": 0, "pre_buy": 0,
                       "n_curve": 0, "n_amm": 0, "same_slot": 0, "buyers": set(), "dev_tok": 0}
    wal = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0]))
    for mint, ts, slot, user, buy, q, tok, fee, cfee, venue in con.execute("SELECT * FROM trades"):
        s = S.get(mint)
        if s is None:
            continue
        u = U[mint]
        w = wal[mint][user]
        w[0] += 1
        w[1 if buy else 2] += q
        w[3] += cfee
        dev = user in (u["creator"], u["payer"])
        s["cfee"] += cfee
        s["n_curve" if venue == 0 else "n_amm"] += 1
        if buy:
            s["buyers"].add(user)
        if dev:
            if buy:
                s["buy"] += q + fee + cfee
                s["dev_tok"] += tok
                if u["gsec"] is None or ts <= u["ts"] + u["gsec"]:
                    s["pre_buy"] += q
                if slot == u["slot"]:
                    s["create_buy"] += q + fee + cfee
            else:
                s["sell"] += q - fee - cfee
                s["dev_tok"] -= tok
        elif slot == u["slot"]:
            s["same_slot"] += 1
    seen = Counter(w for m in wal for w in wal[m])
    for mint, s in S.items():
        u, strict, bot = U[mint], 0, 0
        s["strict_w"] = {}
        for w, (n, bq, sq, cf) in wal[mint].items():
            if w in (u["creator"], u["payer"]):
                continue
            if seen[w] >= 2 or n <= 3 or bq + sq == 0 or abs(bq - sq) / (bq + sq) > 0.5:
                strict += cf
                s["strict_w"][w] = cf
            else:
                bot += cf
        s["cfee_strict"], s["cfee_botlike"] = strict, bot
    meta = {r[0]: r for r in con.execute("SELECT * FROM meta")}
    images = Counter(r[6] for r in meta.values() if r[6])
    snap = {r[0]: r for r in con.execute("SELECT * FROM snap")}
    for mint, s in S.items():
        u = U[mint]
        s["n_buyers"] = len(s.pop("buyers"))
        cost = max(0, u["pdelta"] - s["create_buy"]) if u["sol"] else None
        s["cost"] = cost / LAM if cost is not None else None
        s["cfee_sol"] = s["cfee"] / LAM
        s["net"] = (s["cfee"] + s["sell"] - s["buy"] - cost) / LAM if cost is not None else None
        s["fee_net"] = (s["cfee"] - cost) / LAM if cost is not None else None
        s["fee_net_strict"] = (s["cfee_strict"] - cost) / LAM if cost is not None else None
        s["organic"] = u["organic_u"] and s["n_curve"] > 5 and s["same_slot"] < 3
        s["organic2"] = s["organic"] and s["pre_buy"] <= LAM
        m = meta.get(mint)
        s["meta_ok"] = bool(m and m[1])
        if s["meta_ok"]:
            s["twitter"], s["telegram"], s["website"], s["desc"] = bool(m[2]), bool(m[3]), bool(m[4]), m[5] or 0
            s["img_reuse"] = bool(m[6] and images[m[6]] > 1)
            s["ath"] = m[7]
        sn = snap.get(mint)
        s["mcap"] = sn[3] if sn else None
    return S


def estimate(U, S, L, M=None, f=None, boot=BOOT):
    f = f or (lambda s: s["net"])
    strata = {}
    for st in ("g", "n"):
        base = [(f(s), bool(M(s)) if M else True) for m, s in S.items() if s["stratum"] == st and L(U[m]) and f(s) is not None]
        size = sum(1 for u in U.values() if (u["grad"] if st == "g" else u["curve_known"] and not u["grad"]) and L(u))
        if base:
            strata[st] = (size, base)
    if not strata:
        return None

    def point(draw):
        tot = num = 0.0
        parts = {}
        for st, (size, base) in strata.items():
            b = draw(base)
            sub = [v for v, ok in b if ok]
            wt = size * len(sub) / len(b)
            if sub:
                num += wt * sum(sub) / len(sub)
            tot += wt
            parts[st] = (wt, sub)
        return (num / tot if tot else None), parts

    mean, parts = point(lambda b: b)
    if mean is None:
        return None
    reps = sorted(x for x in (point(lambda b: RND.choices(b, k=len(b)))[0] for _ in range(boot)) if x is not None)
    se = (sum((x - mean) ** 2 for x in reps) / max(1, len(reps) - 1)) ** 0.5
    w = [(v, parts[st][0] / len(parts[st][1])) for st in parts if parts[st][1] for v in parts[st][1]]
    w.sort()
    tw = sum(x for _, x in w)

    def q(p):
        acc = 0
        for v, x in w:
            acc += x
            if acc >= p * tw:
                return round(v, 5)

    size_g, size_n = (parts.get("g", (0, []))[0], parts.get("n", (0, []))[0])
    return {"mean": round(mean, 5), "se": round(se, 5), "t": round(mean / se, 2) if se else None,
            "ci95": [round(reps[int(.025 * len(reps))], 5), round(reps[int(.975 * len(reps)) - 1], 5)] if reps else None,
            "p50": q(.5), "p75": q(.75), "p90": q(.9), "p99": q(.99),
            "share_pos": round(sum(x for v, x in w if v > 0) / tw, 4) if tw else None,
            "mean_by_stratum": {st: round(sum(parts[st][1]) / len(parts[st][1]), 5) for st in parts if parts[st][1]},
            "est_launches": round(size_g + size_n), "grad_share": round(size_g / (size_g + size_n), 4) if size_g + size_n else None,
            "n_sampled": {st: len(parts[st][1]) for st in parts}}


def funding(con, U, S):
    tg = con.execute("SELECT mint, addr, role, cfee FROM targets").fetchall()
    fund = {a: f for a, f, *_ in con.execute("SELECT * FROM funding")}
    if not tg or not fund:
        return None
    coins = defaultdict(list)
    deg = defaultdict(set)
    for m, a, role, cf in tg:
        coins[m].append((a, role, cf))
        if role == "trader" and fund.get(a):
            deg[fund[a]].add(m)
    hubs = {f for f, ms in deg.items() if len(ms) >= CFG["fund"]["hub_coins"]}
    rows, agg = [], {st: Counter() for st in ("g", "n")}
    for m, items in coins.items():
        u, s = U[m], S[m]
        dev = {u["creator"], u["payer"]}
        dev_f = {fund.get(u["creator"]), fund.get(u["payer"])} - {None}
        c = Counter()
        for a, role, cf in items:
            if role != "trader":
                continue
            f = fund.get(a)
            kind = "direct" if f in dev else "hub" if f in hubs else "sibling" if f and f in dev_f else "traced" if f else "unknown"
            c[kind] += cf
            c["top"] += cf
            if kind in ("direct", "sibling") and a in s["strict_w"]:
                c["strict_linked"] += cf
        s["cfee_strict_adj"] = s["cfee_strict"] - c["strict_linked"]
        c["all"] = s["cfee"]
        agg[s["stratum"]].update(c)
        rows.append((m, s["stratum"], dict(c)))
    share = {st: {k: round(v / max(1, a["all"]), 4) for k, v in a.items() if k != "all"} | {"coins": sum(1 for r in rows if r[1] == st)}
             for st, a in agg.items()}
    hub_fee = Counter()
    for m, a, role, cf in tg:
        if role == "trader" and fund.get(a) in hubs:
            hub_fee[fund[a]] += cf
    return {"share_of_coin_fees": share, "hubs": [(h, len(deg[h]), round(v / LAM, 3)) for h, v in hub_fee.most_common(15)],
            "funded_found": sum(1 for v in fund.values() if v), "addresses": len(fund)}


def logit(rows, names, iters=25):
    k = len(names)
    beta = [0.0] * k
    for _ in range(iters):
        g, H = [0.0] * k, [[0.0] * k for _ in range(k)]
        for x, y in rows:
            z = max(-30, min(30, sum(b * v for b, v in zip(beta, x))))
            p = 1 / (1 + math.exp(-z))
            for i in range(k):
                g[i] += (y - p) * x[i]
                for j in range(k):
                    H[i][j] += p * (1 - p) * x[i] * x[j]
        inv = invert(H)
        if inv is None:
            return None
        step = [sum(inv[i][j] * g[j] for j in range(k)) for i in range(k)]
        beta = [b + s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < 1e-8:
            break
    return {n: {"coef": round(b, 3), "se": round(math.sqrt(inv[i][i]), 3), "z": round(b / math.sqrt(inv[i][i]), 2),
                "odds": round(math.exp(b), 2)} for i, (n, b) in enumerate(zip(names, beta))}


def invert(M):
    n = len(M)
    A = [row[:] + [float(i == j) for j in range(n)] for i, row in enumerate(M)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(A[r][c]))
        if abs(A[p][c]) < 1e-12:
            return None
        A[c], A[p] = A[p], A[c]
        piv = A[c][c]
        A[c] = [v / piv for v in A[c]]
        for r in range(n):
            if r != c and A[r][c]:
                fct = A[r][c]
                A[r] = [a - fct * b for a, b in zip(A[r], A[c])]
    return [row[n:] for row in A]


def main():
    OUT.mkdir(exist_ok=True)
    con = db()
    U = universe(con)
    S = sampled(con, U)
    R = {}
    sol = [u for u in U.values() if u["sol"]]
    grads = [u for u in U.values() if u["grad"]]
    R["coverage"] = {
        "launches": len(U), "sol_share": round(len(sol) / len(U), 4), "mayhem_share": round(sum(u["mayhem"] for u in U.values()) / len(U), 4),
        "curve_known": sum(u["curve_known"] for u in U.values()), "graduates": len(grads),
        "grads_with_migr_event": sum(u["migr"] for u in grads), "per_day": Counter(datetime.fromtimestamp(u["ts"], timezone.utc).strftime("%m-%d") for u in U.values()),
        "sampled": Counter(s["stratum"] for s in S.values()), "trades_capped": sum(s["capped"] or 0 for s in S.values()),
        "meta_ok": sum(s["meta_ok"] for s in S.values()), "creators": len({u["creator"] for u in U.values()}),
    }
    nod = sorted(u["pdelta"] - u["fee"] for u in sol if u["dev"] == 0)
    R["rent_nodev_sol"] = {k: round(nod[int(q * (len(nod) - 1))] / LAM, 5) for k, q in (("p10", .1), ("p50", .5), ("p90", .9))} if nod else None
    lab = Counter()
    for u in grads:
        lab["self_bond" if u["self_bond"] else "mayhem_low" if u["mayhem_low"] else "instant" if u["instant"] else
            "dev_ge_1sol" if (u["dev"] or 0) >= 1 else "third_party_in_create" if u["oth_n"] else "non_sol" if not u["sol"] else "organic_u"] += 1
    R["graduate_labels"] = dict(lab)
    R["grad_rate_any"] = cluster_rate([(u["creator"], int(u["grad"])) for u in U.values() if u["curve_known"]])
    R["grad_rate_organic_u"] = cluster_rate([(u["creator"], int(u["organic_u"])) for u in sol if u["curve_known"]])
    sg = [s for m, s in S.items() if s["stratum"] == "g" and U[m]["organic_u"]]
    if sg:
        keep = sum(s["organic"] for s in sg) / len(sg)
        R["organic_refine"] = {"organic_u_in_sample": len(sg), "pass_full_filter": wilson(sum(s["organic"] for s in sg), len(sg)),
                               "grad_rate_organic_pct": round(R["grad_rate_organic_u"]["pct"] * keep, 3)}
    gs = sorted(u["gsec"] for u in grads if u["gsec"] is not None)
    R["time_to_grad_min"] = {k: round(gs[int(q * (len(gs) - 1))] / 60, 2) for k, q in (("p10", .1), ("p25", .25), ("p50", .5), ("p75", .75), ("p90", .9))} if gs else None

    feats = {
        "dev_buy_sol": lambda u: ("0" if u["dev"] == 0 else bucket(u["dev"], [0.1, 0.5, 1, 3, 84], ["<0.1", "0.1-0.5", "0.5-1", "1-3", "3-84", ">=84"])) if u["dev"] is not None else None,
        "creator_launches": lambda u: bucket(u["launches"], [2, 5, 20, 100], ["1", "2-4", "5-19", "20-99", "100+"]),
        "copycat_24h": lambda u: u["copycat"],
        "hour_utc": lambda u: bucket(u["hour"], [6, 12, 18], ["00-05", "06-11", "12-17", "18-23"]),
        "mayhem": lambda u: bool(u["mayhem"]),
    }
    R["features_universe"] = {}
    for name, fn in feats.items():
        out = {}
        for split in ("train", "test"):
            groups = defaultdict(list)
            for u in sol:
                if u["curve_known"] and u["test"] == (split == "test"):
                    v = fn(u)
                    if v is not None:
                        groups[str(v)].append((u["creator"], int(u["organic_u"])))
            out[split] = {k: cluster_rate(v) for k, v in sorted(groups.items())}
        R["features_universe"][name] = out

    win = [(m, s) for m, s in S.items() if s["stratum"] == "g" and s["organic"] and s.get("meta_ok")]
    ctl = [(m, s) for m, s in S.items() if s["stratum"] == "n" and U[m]["sol"] and s.get("meta_ok")]
    R["features_sample"] = {"winners": len(win), "controls": len(ctl)}
    tests = {"twitter": lambda s: s["twitter"], "website": lambda s: s["website"], "telegram": lambda s: s["telegram"],
             "x_and_web": lambda s: s["twitter"] and s["website"], "no_socials": lambda s: not (s["twitter"] or s["website"] or s["telegram"]),
             "desc_any": lambda s: s["desc"] > 0, "desc_50": lambda s: s["desc"] >= 50, "img_reuse": lambda s: s["img_reuse"]}
    for name, fn in tests.items():
        row = {}
        for split in ("train", "test"):
            ww = [s for m, s in win if U[m]["test"] == (split == "test")]
            cc = [s for m, s in ctl if U[m]["test"] == (split == "test")]
            row[split] = ztest(sum(map(fn, ww)), len(ww), sum(map(fn, cc)), len(cc))
        R["features_sample"][name] = row

    names = ["const", "twitter", "website", "telegram", "desc_any", "dev_buy_pos", "creator_20plus", "copycat", "hour_18_23"]
    rows = []
    for m, s in S.items():
        if s.get("meta_ok") and U[m]["sol"] and not U[m]["test"] and ((s["stratum"] == "g" and s["organic"]) or s["stratum"] == "n"):
            u = U[m]
            rows.append(([1, s["twitter"], s["website"], s["telegram"], s["desc"] > 0, (u["dev"] or 0) > 0, u["launches"] >= 20,
                          u["copycat"], u["hour"] >= 18], int(s["stratum"] == "g")))
    R["logit_train"] = {"n": len(rows), "coef": logit([([float(v) for v in x], y) for x, y in rows], names) if rows else None}

    small = lambda u: u["plain"] and (u["dev"] or 0) <= 0.1
    R["fees_per_launch_sol"] = {
        "all_sol": estimate(U, S, lambda u: u["sol"], f=lambda s: s["cfee_sol"]),
        "small_plain": estimate(U, S, small, f=lambda s: s["cfee_sol"]),
    }
    R["net_per_launch_sol"] = {
        "all_sol": estimate(U, S, lambda u: u["sol"]),
        "small_plain": estimate(U, S, small),
        "dev_buy_3plus": estimate(U, S, lambda u: u["plain"] and (u["dev"] or 0) >= 3),
    }
    R["cost_per_launch_sol"] = estimate(U, S, small, f=lambda s: s["cost"])

    strat = {
        "S1_small_few_launches": (lambda u: small(u) and u["launches"] <= 4, lambda s: True),
        "S2_S1_x_web": (lambda u: small(u) and u["launches"] <= 4, lambda s: s.get("meta_ok") and s["twitter"] and s["website"]),
        "S3_S1_original": (lambda u: small(u) and u["launches"] <= 4 and not u["copycat"], lambda s: True),
        "S4_S2_original": (lambda u: small(u) and u["launches"] <= 4 and not u["copycat"], lambda s: s.get("meta_ok") and s["twitter"] and s["website"]),
    }
    R["strategies"] = {}
    for name, (L, M) in strat.items():
        R["strategies"][name] = {
            "train": estimate(U, S, lambda u, L=L: L(u) and not u["test"], M),
            "test": estimate(U, S, lambda u, L=L: L(u) and u["test"], M),
        }
        t = R["strategies"][name]["test"]
        R["strategies"][name]["gate_pass"] = bool(t and t["mean"] > 0 and (t["t"] or 0) > 2 and t["est_launches"] >= 300)

    fee_net = lambda s: s["fee_net"]
    base = lambda u: small(u) and u["launches"] <= 4
    legit = {
        "S1_small_few_launches": (base, None),
        "S2_S1_x_web": (base, lambda s: s.get("meta_ok") and s["twitter"] and s["website"]),
        "S3_S1_original": (lambda u: base(u) and not u["copycat"], None),
        "S4_S2_original": (lambda u: base(u) and not u["copycat"], lambda s: s.get("meta_ok") and s["twitter"] and s["website"]),
        "S5_S4_evening_exploratory": (lambda u: base(u) and not u["copycat"] and u["hour"] >= 18, lambda s: s.get("meta_ok") and s["twitter"] and s["website"]),
    }
    R["legit_fee_only"] = {}
    for name, (L, M) in legit.items():
        row = {}
        for split in ("train", "test", "all"):
            Ls = lambda u, L=L, split=split: L(u) and (not u["grad"] or u["organic_u"]) and (split == "all" or u["test"] == (split == "test"))
            Mlow = lambda s, M=M: (M is None or M(s)) and (s["stratum"] == "n" or s["organic"])
            row[split] = {"upper": estimate(U, S, Ls, M, fee_net), "lower": estimate(U, S, Ls, Mlow, fee_net),
                          "strict_lower": estimate(U, S, Ls, Mlow, lambda s: s["fee_net_strict"])}
        t = row["test"]["lower"]
        row["gate_pass_lower"] = bool(t and t["mean"] > 0 and (t["t"] or 0) > 2 and t["est_launches"] >= 300)
        t = row["test"]["upper"]
        row["gate_pass_upper"] = bool(t and t["mean"] > 0 and (t["t"] or 0) > 2 and t["est_launches"] >= 300)
        t = row["test"]["strict_lower"]
        row["gate_pass_strict"] = bool(t and t["mean"] > 0 and (t["t"] or 0) > 2 and t["est_launches"] >= 300)
        R["legit_fee_only"][name] = row
    strict = lambda s: s["fee_net_strict"]
    rob = {}
    for name, L in (("S1", base), ("S1_dev0", lambda u: base(u) and u["dev"] == 0), ("S2", base)):
        M = (lambda s: s.get("meta_ok") and s["twitter"] and s["website"]) if name == "S2" else None
        for split in ("all", "test"):
            Ls = lambda u, L=L, split=split: L(u) and (not u["grad"] or u["organic_u"]) and (split == "all" or u["test"])
            M2 = lambda s, M=M: (M is None or M(s)) and (s["stratum"] == "n" or s["organic2"])
            rob[f"{name}_{split}_organic2"] = estimate(U, S, Ls, M2, strict)
            og2 = sorted((s["cfee_strict"], m) for m, s in S.items() if s["stratum"] == "g" and s["organic2"] and Ls(U[m]))
            ng = sorted((s["cfee_strict"], m) for m, s in S.items() if s["stratum"] == "n" and Ls(U[m]))
            tot_ng = sum(v for v, _ in ng) or 1
            rob[f"{name}_{split}_nongrad_top_share"] = {k: round(sum(v for v, _ in ng[-k:]) / tot_ng, 3) for k in (1, 5, 10)}
            for k_g, k_n in ((1, 0), (3, 0), (0, 5), (3, 5)):
                drop = {m for _, m in og2[-k_g:]} if k_g else set()
                drop |= {m for _, m in ng[-k_n:]} if k_n else set()
                S2_ = {m: s for m, s in S.items() if m not in drop}
                rob[f"{name}_{split}_drop_g{k_g}_n{k_n}"] = estimate(U, S2_, Ls, M2, strict, boot=300)
    R["robust"] = {k: (v if not isinstance(v, dict) or "mean" not in v else
                       {x: v[x] for x in ("mean", "se", "t", "ci95", "mean_by_stratum", "grad_share", "est_launches", "n_sampled")})
                   for k, v in rob.items()}
    R["funding"] = funding(con, U, S)
    if R["funding"]:
        for s in S.values():
            s.setdefault("cfee_strict_adj", s["cfee_strict"])
            s["fee_net_adj"] = (s["cfee_strict_adj"] - s["cost"] * LAM) / LAM if s["cost"] is not None else None
        adj = lambda s: s["fee_net_adj"]
        R["funding"]["adjusted"] = {}
        for name, L, M in (("S1", base, None), ("S2", base, lambda s: s.get("meta_ok") and s["twitter"] and s["website"])):
            for split in ("all", "test"):
                Ls = lambda u, L=L, split=split: L(u) and (not u["grad"] or u["organic_u"]) and (split == "all" or u["test"])
                M2 = lambda s, M=M: (M is None or M(s)) and (s["stratum"] == "n" or s["organic2"])
                e = estimate(U, S, Ls, M2, adj)
                R["funding"]["adjusted"][f"{name}_{split}"] = e and {x: e[x] for x in ("mean", "se", "t", "ci95", "mean_by_stratum", "grad_share", "n_sampled")}
    og = [s for s in S.values() if s["stratum"] == "g" and s["organic"]]
    R["organic_grads"] = {"n": len(og), "capped": sum(s["capped"] or 0 for s in og),
                          "fee_sol_mean": round(sum(s["cfee_sol"] for s in og) / max(1, len(og)), 4),
                          "organic2": sum(s["organic2"] for s in og),
                          "strict_fee_sol_mean": round(sum(s["cfee_strict"] for s in og) / LAM / max(1, len(og)), 4),
                          "botlike_fee_share": round(sum(s["cfee_botlike"] for s in og) / max(1, sum(s["cfee"] for s in og)), 4),
                          "fee_sol_sorted": sorted(round(s["cfee_sol"], 3) for s in og)}

    dur = defaultdict(Counter)
    for m, s in S.items():
        if s["stratum"] != "g":
            continue
        key = "organic" if s["organic"] else "artefact"
        mc = s["mcap"]
        dur[key]["n"] += 1
        dur[key]["listed"] += mc is not None and mc > 0
        for th in (30e3, 100e3, 1e6):
            dur[key][f"mcap_ge_{int(th / 1000)}k"] += (mc or 0) >= th
        dur[key]["ath_ge_100k"] += (s.get("ath") or 0) >= 100e3
    R["durability_graduates"] = {k: dict(v) for k, v in dur.items()}
    nn = [s for m, s in S.items() if s["stratum"] == "n" and U[m]["sol"]]
    R["nongrad_fee_split"] = {"n": len(nn), "botlike_share": round(sum(s["cfee_botlike"] for s in nn) / max(1, sum(s["cfee"] for s in nn)), 4),
                              "dev_share": round(1 - sum(s["cfee_strict"] + s["cfee_botlike"] for s in nn) / max(1, sum(s["cfee"] for s in nn)), 4)}
    R["dev_still_holding_share"] = round(sum(1 for s in S.values() if s["dev_tok"] > 0) / max(1, len(S)), 4)
    (OUT / "report.json").write_text(json.dumps(R, indent=1, default=str))
    print(json.dumps(R, indent=1, default=str))


if __name__ == "__main__":
    main()
