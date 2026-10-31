# runt_clear.py（2026-10-23；v20.0 2026-10-24 也能用）：一次清空 6 個帳戶的 /runt（模擬）。
# 1. 停掉 6 個 bot（bot 停的時候會把 /runt 最後存一次檔）
# 2. 每個帳戶 /runt 還抱著的單，用「現在 OKX 的現價」模擬強制平倉（市價出場），寫進今天的逐單紀錄 runtlog（/summary 看得到，出場原因＝強制平倉）
#    算法＝v18.5.6～v20.0 gd_money（模擬）：那一單的金額、帳戶費率（存檔裡的）、現貨買進手續費從幣扣、賣出照現貨數量單位往下取；
#    合約 S 做空、D 做多；實際槓桿＝數量×進場價÷金額。帳戶：存檔 v＜200（v18～v19）＝現貨在這個帳戶、合約每 11 層一個帳戶（兩套）；
#    v≥200（v20.0）＝現貨、合約都是每 10 層一個帳戶 o1111o～o5555o，費率 現貨 "sp:帳戶"、合約 帳戶
# 3. /runt 存檔清空（掛單一起清掉；原本的存檔備份成 runtgrid_帳戶.json.bak-日期-時間）、6 個 bot 共用的模擬佔用紀錄 fd_sim 刪掉（bot 開起來會重寫）
# 4. 6 個 bot 開回來
# 只查 OKX 公開的現價、數量單位，不下單、不碰 OKX 帳戶；不碰實盤 /run 的檔案（rungrid、rungridlog）。
# 跑法（VPS）：sudo /srv/1111bot/.venv/bin/python /tmp/runt_clear.py
import json, os, sys, time, subprocess, asyncio, shutil
from decimal import Decimal as D, ROUND_HALF_UP, ROUND_FLOOR
from datetime import datetime, timezone, timedelta

DATA = os.environ.get("RUNT_DATA", "/srv/1111bot/data")
ACCTS = ["o1111o", "o2222o", "o3333o", "o4444o", "o5555o", "o6666o"]
SETS = {"o1111o": ("o1111o", "o2222o", "o3333o"), "o4444o": ("o4444o", "o5555o", "o6666o")}   # v18～v19
X20 = ("o1111o", "o2222o", "o3333o", "o4444o", "o5555o")                                         # v20.0
TZ8 = timezone(timedelta(hours=8))
FEE_DEF = {"sp": ("0.08", "0.10"), "sw": ("0.02", "0.05")}
LOT0 = D("0.00000001")
WHY = "強制平倉"
q2 = lambda v: D(v).quantize(D("0.01"), rounding=ROUND_HALF_UP)
q4 = lambda v: D(v).quantize(D("0.0001"), rounding=ROUND_HALF_UP)
su = lambda v: f"{'-' if v < 0 else '+'}{abs(q4(v)):.4f}U"
unit = lambda a: f"1111bot-{a}-normal.service"


def nm(kind, k):
    if kind == "sw":
        return f"S{k:02d}" if k > 0 else f"D{-k:02d}"
    return f"L{-k:02d}" if k <= 0 else f"U{k:02d}"


def pct(v):
    d = D(str(v)).normalize()
    return str(d.quantize(D(1)) if d == d.to_integral_value() else d)


def kacct(acct, kind, k, v=180):
    """跟 gd_kacct 一樣：v20.0（v≥200）＝現貨、合約都是每 10 層一個帳戶 o1111o～o5555o（L00 在 o1111o）；
    v18～v19＝現貨在這個帳戶、合約＝那一套的第 1～11 層、12～22、23～33 層的帳戶。"""
    if v >= 200:
        return X20[max(abs(k) - 1, 0) // 10]
    if kind != "sw":
        return acct
    main = next((m for m, xs in SETS.items() if acct in xs), "o1111o")
    return SETS[main][(abs(k) - 1) // 11]


async def okx():
    import httpx
    px, lot = {}, {}
    async with httpx.AsyncClient(timeout=20) as c:
        for t in ("SPOT", "SWAP"):
            r = (await c.get(f"https://www.okx.com/api/v5/market/tickers?instType={t}")).json()
            if str(r.get("code")) != "0":
                raise RuntimeError(f"OKX 現價查不到 {t} {r.get('code')} {r.get('msg')}")
            for x in r.get("data") or []:
                if x.get("last"):
                    px[x["instId"]] = D(x["last"])
        r = (await c.get("https://www.okx.com/api/v5/public/instruments?instType=SPOT")).json()
        if str(r.get("code")) != "0":
            raise RuntimeError(f"OKX 現貨規格查不到 {r.get('code')} {r.get('msg')}")
        for x in r.get("data") or []:
            if x.get("lotSz"):
                lot[x["instId"]] = D(x["lotSz"])
    return px, lot


def money(g, p, px, lot, acct):
    """跟 v18.5.6～v20.0 gd_money（模擬）同一個算法。"""
    kind, k = p["kind"], int(p["k"])
    v = int(g.get("v") or 0)
    amt = D(p.get("amt") or p.get("lu") or g["amt"])
    if v >= 200:
        key = f"sp:{kacct(acct, kind, k, v)}" if kind == "sp" else kacct(acct, kind, k, v)
    else:
        key = "sp" if kind == "sp" else kacct(acct, kind, k)
    fe = (g.get("fee") or {}).get(key) or FEE_DEF["sp" if kind == "sp" else "sw"]
    fi, fo = D(fe[0]), D(fe[1])
    ent, qty = D(p["ent"]), D(p["qty"])
    if kind == "sp":
        got = qty * (1 - fi / 100) if p.get("got") is None else D(p["got"])
        u = lot or LOT0
        sell = (got / u).to_integral_value(rounding=ROUND_FLOOR) * u
        gross = qty * (px - ent)
        fee = qty * fi / 100 * ent + sell * px * fo / 100
    else:
        gross = qty * ((ent - px) if k > 0 else (px - ent))
        fee = qty * ent * fi / 100 + qty * px * fo / 100
    fu = D(p.get("fu") or "0")
    gp, fp, fd = q2(gross / amt * 100), q2(fee / amt * 100), q2(fu / amt * 100)
    gu, feeu, fuq = q4(gross), q4(fee), q4(fu)
    return {"g": gp, "fee": fp, "net": gp - fp + fd, "gu": gu, "feeu": feeu, "netu": gu - feeu + fuq, "fu": fuq, "fund": fd,
            "fn": int(p.get("fn") or 0), "amt": amt}


def flat(acct, data, px_of, lot_of, t_out):
    """一個帳戶的存檔 → (逐單紀錄, 每一組 [標題, 單數, 淨損益U], 掛單張數, 查不到現價的單)。不寫檔。"""
    day = datetime.fromtimestamp(t_out, TZ8).strftime("%Y-%m-%d")
    recs, rows, miss, n_ord = [], [], [], 0
    for g in data:
        sym = g["sym"]
        n_ord += sum(1 for o in (g.get("ord") or {}).values() if o) + (1 if g.get("o00") else 0)
        n, tot = 0, D(0)
        for p in g.get("pos") or []:
            kind, k = p["kind"], int(p["k"])
            iid = sym[:-4] + ("-USDT" if kind == "sp" else "-USDT-SWAP")
            px = px_of.get(iid)
            if px is None:
                miss.append(f"{acct} {sym} {nm(kind, k)}")
                continue
            m = money(g, p, px, lot_of.get(sym[:-4] + "-USDT"), acct)
            side = "L" if (kind == "sp" or k < 0) else "S"
            ent, qty = D(p["ent"]), D(p["qty"])
            gg = (px / ent - 1) * 100 if side == "L" else (1 - px / ent) * 100
            mfe, mae = max(D(p.get("mfe") or 0), gg), min(D(p.get("mae") or 0), gg)
            org = g.get("org") or {}
            t_in = float(p["t_in"])
            recs.append({"d": day, "g": 1, "sym": sym, "dir": g["dir"], "kind": kind, "side": side, "k": k, "lvl": k, "nm": nm(kind, k),
                         "lev": str(q2(qty * ent / m["amt"])) if kind == "sw" else str(D(p["lev"])), "acct": kacct(acct, kind, k, int(g.get("v") or 0)),
                         "amt": str(m["amt"]), "up": 1 if p.get("up") else 0,
                         "org": round(float(org["t"]), 3) if org.get("t") is not None else None, "t_run": round(float(g["t_run"]), 3),
                         "ent": str(ent), "px": str(px), "t_in": round(t_in, 3), "t_out": round(t_out, 3), "hold": round(t_out - t_in, 1),
                         "why": WHY, "n_mv": int(p.get("n_mv") or 0), "n_tp": int(p.get("n_tp") or 0), "mfe": str(q2(mfe)), "mae": str(q2(mae)),
                         "gp": str(m["g"]), "np": str(m["net"]), "fp": str(m["fund"]), "gu": str(m["gu"]), "feeu": str(m["feeu"]),
                         "u": str(m["netu"]), "fu": str(m["fu"]), "fn": m["fn"]})
            n += 1
            tot += m["netu"]
        rows.append((f"{sym} {g['dir']} {pct(g['amt'])}U" + ("(停止中)" if g.get("stop") else ""), n, tot))
    return recs, rows, n_ord, miss


def own_like(path, ref):
    """用 sudo 跑時，新寫的檔案的擁有者改成跟原本的檔案（或 data 資料夾）一樣，bot 之後才寫得進去。"""
    try:
        st = os.stat(ref)
        if os.geteuid() == 0:
            os.chown(path, st.st_uid, st.st_gid)
    except Exception:
        pass


def is_active(a):
    return subprocess.run(["systemctl", "is-active", unit(a)], capture_output=True, text=True).stdout.strip() == "active"


def main():
    test = bool(os.environ.get("RUNT_TEST"))
    on = [a for a in ACCTS if is_active(a)] if not test else []
    if not test:
        if os.geteuid() != 0:
            print("已中止:要用 sudo 跑(sudo /srv/1111bot/.venv/bin/python /tmp/runt_clear.py),什麼都沒動")
            sys.exit(1)
        print("停止 bot:", " ".join(on) if on else "(本來就沒有在跑的)")
        if on:
            subprocess.run(["systemctl", "stop"] + [unit(a) for a in on])
        still = [a for a in on if is_active(a)]
        if still:
            print("已中止:這幾個 bot 停不下來:", " ".join(still), ",什麼都沒動,bot 開回來")
            subprocess.run(["systemctl", "start"] + [unit(a) for a in on])
            sys.exit(1)
    ok = False
    try:
        px_of, lot_of = asyncio.run(okx())
        t_out = time.time()
        hm = datetime.fromtimestamp(t_out, TZ8).strftime("%H:%M:%S")
        stamp = datetime.fromtimestamp(t_out, TZ8).strftime("%Y%m%d-%H%M%S")
        todo, miss = [], []
        for a in ACCTS:
            f = f"{DATA}/runtgrid_{a}.json"
            data = json.load(open(f)) if os.path.exists(f) else []
            if not data:
                todo.append((a, f, None))
                continue
            recs, rows, n_ord, ms = flat(a, data, px_of, lot_of, t_out)
            miss += ms
            todo.append((a, f, (recs, rows, n_ord)))
        if miss:
            print("已中止:這幾單查不到 OKX 現價,沒有動任何檔案:" + "、".join(miss))
            return
        allu, alln, allo = D(0), 0, 0
        for a, f, x in todo:
            if x is None:
                print(f"{a}:沒有進行中的 runt")
                continue
            recs, rows, n_ord = x
            bak = f"{f}.bak-{stamp}"
            shutil.copy2(f, bak)
            own_like(bak, f)
            lf = f"{DATA}/runtlog_{a}.jsonl"
            new = not os.path.exists(lf)
            with open(lf, "a") as fh:
                for r in recs:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            if new:
                own_like(lf, f)
            with open(f, "w") as fh:
                json.dump([], fh)
            n = sum(r[1] for r in rows)
            u = sum((r[2] for r in rows), D(0))
            allu += u
            alln += n
            allo += n_ord
            print(f"{a}:{len(rows)} 組,模擬強制平倉 {n} 單({hm} 的現價,市價出場),掛單 {n_ord} 張清掉")
            for h, k, v in rows:
                print(f" {h}|{k}單|淨損益 {su(v)}")
            print(f" 合計 淨損益 {su(u)}")
            print(f" 存檔已清空(原本的存檔:runtgrid_{a}.json.bak-{stamp})")
        for x in os.listdir(DATA):
            if x.startswith("fd_sim_") and x.endswith(".json"):
                os.remove(f"{DATA}/{x}")
        print(f"全部:模擬強制平倉 {alln} 單|掛單 {allo} 張清掉|淨損益 {su(allu)}")
        print("已寫進今天的逐單紀錄:/summary 看得到,出場原因寫「強制平倉」")
        ok = True
    except Exception as e:
        print("已中止:", type(e).__name__, e, ",存檔沒有清空")
    finally:
        if on:
            subprocess.run(["systemctl", "start"] + [unit(a) for a in on])
            time.sleep(5)
            up = [a for a in on if is_active(a)]
            down = [a for a in on if a not in up]
            print("bot 已經開回來:", " ".join(up) + ("" if not down else f" ⚠️沒開起來:{' '.join(down)}"))
    if ok:
        print("✅ /runt 全部清空了(實盤 /run 的檔案沒有動)")


if __name__ == "__main__":
    main()
