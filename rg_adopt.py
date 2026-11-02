# rg_adopt.py（2026-11-02）：把 /run 沒恢復的幣種接管回來（v19.0～v20.1.0 重開時 rungrid 存檔被蓋成空的）。
# 做法：用 bot 自己的程式（run_bot_next.py）照 rungridevent（每一次送單、成交、條件單、出場、資金費）把每一組重建一遍，
#   再查 OKX 現況：關機期間成交的補上、還掛著的進場單撤掉（bot 開起來照現價重掛）、條件單照 OKX 的觸發價、
#   持倉張數／現貨顆數跟 OKX 對過 → 寫回 rungrid 存檔 → bot 開起來照原本的重開恢復接著跑（以 OKX 為準對帳）。
# 不平倉、不動持倉、不動條件單；只撤 BOT 自己還沒成交的進場單（gr 開頭）。對不上就什麼都不改、印出原因。
# 跑法（VPS）：sudo /srv/1111bot/.venv/bin/python /tmp/rg_adopt.py STRKUSDT STXUSDT WLDUSDT
import os, sys, json, time, asyncio, subprocess, shutil, importlib.util
from decimal import Decimal as D
from datetime import datetime, timezone, timedelta

TZ8 = timezone(timedelta(hours=8))
WAIT_OK = ("canceled", "filled", "mmp_canceled")


def read_jsonl(path):
    out = []
    try:
        with open(path) as f:
            for x in f:
                try:
                    out.append(json.loads(x))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return out


def parse_nm(nm):
    c, n = nm[0], int(nm[1:])
    return {"L": ("sp", -n), "U": ("sp", n), "S": ("sw", n), "D": ("sw", -n)}[c]


def Dn(v):
    return None if v in (None, "", "None") else D(str(v))


class Clock:
    """重建時 bot 裡面的 time.time() ＝那一筆事件的時間（設 SL 時間、移動時間才會跟原本一樣）。"""
    def __init__(self, real):
        self.real, self.t = real, None

    def time(self):
        return self.real.time() if self.t is None else self.t

    def __getattr__(self, k):
        return getattr(self.real, k)


def hm(t):
    return datetime.fromtimestamp(t, TZ8).strftime("%m/%d %H:%M:%S")


def find_open(G, nm, acct):
    ps = [p for p in G["pos"] if not p["out"] and B_nm(p) == nm and p["acct"] == acct]
    return ps[-1] if ps else None


B_nm = None


def replay(B, G, evs, clk, warn):
    """照事件重建：持倉（rg_pos_new，跟 bot 成交時一模一樣）、條件單（rg_algo_ok）、出場、資金費（rg_funding）。回傳還沒結束的進場單。"""
    orders, amb, ended = {}, [], {}
    n_exit = 0
    for e in evs:
        clk.t = float(e["t"])
        k = e.get("k")
        if k == "place":
            kd, kk = parse_nm(e["nm"])
            o = B.rg_onew(G, kd, kk, D(str(e["px"])), float(e["t"]))
            if o["acct"] != e.get("acct"):
                warn.append(f"{e['nm']} 掛單帳戶 {e.get('acct')}，照規則應該是 {o['acct']}")
                o["acct"] = e.get("acct")
            if Dn(e.get("sz")) is not None and Dn(e.get("sz")) != o["sz"]:
                o["sz"] = Dn(e.get("sz"))
            o.update(clid=e.get("clid"), oid=e.get("oid"), t_s=float(e["t"]))
            if e.get("oid"):
                orders[e["oid"]] = o
            elif str(e.get("code")) in ("-1", "None") or e.get("code") is None:
                amb.append(o)                              # 送單沒回應（之後用自訂單號找回）
        elif k == "fill":
            o = orders.pop(e.get("oid"), None) or ended.pop(e.get("oid"), None)
            if o is None:
                o = next((x for x in amb if B.gd_nm(x["kd"], x["k"]) == e["nm"] and x["acct"] == e.get("acct")), None)
                if o is not None:
                    amb.remove(o)
            if o is None:
                kd, kk = parse_nm(e["nm"])
                o = B.rg_onew(G, kd, kk, D(str(e["ent"])), float(e.get("t_in") or e["t"]))
                o["acct"] = e.get("acct")
                warn.append(f"{e['nm']} 成交找不到掛單紀錄（用成交價補）")
            od = {"avgPx": e.get("ent"), "accFillSz": e.get("sz"), "fillTime": str(int(float(e.get("t_in") or e["t"]) * 1000)),
                  "fee": e.get("fee"), "feeCcy": e.get("feeCcy") or "", "ordId": e.get("oid"), "state": e.get("state")}
            G["px"][o["kd"]] = D(str(e["ent"]))
            p = B.rg_pos_new(G, o, od)
            G["pos"].append(p)
            if o["kd"] == "sp" and o["k"] == 0 and G["org"] is None:
                G["org"] = {"px": o["px"], "t": p["t_in"]}
        elif k == "entry_end":                             # 進場單結束（bot 先記 entry_end，有成交的再記 fill）
            o = orders.pop(e.get("oid"), None)
            if o is not None:
                ended[e.get("oid")] = o
        elif k == "amb_gone":
            amb = [x for x in amb if x.get("clid") != e.get("clid")]
        elif k == "algo_place":
            if str(e.get("code")) == "0" and str(e.get("sCode")) == "0" and e.get("aid"):
                p = find_open(G, e["nm"], e.get("acct"))
                if p is None:
                    continue
                p["aid"], p["t_aid"] = e["aid"], float(e["t"])
                if p["kind"] == "sp" and Dn(e.get("sz")) is not None:
                    p["ssz"] = Dn(e.get("sz"))
                B.rg_algo_ok(p, Dn(e.get("sl")), Dn(e.get("tp")))
                p["sl_t"] = p["tp_t"] = float(e["t"])
        elif k == "algo_amend":
            if str(e.get("sCode")) == "0":
                p = next((x for x in G["pos"] if not x["out"] and x.get("aid") == e.get("aid")), None)
                if p is None:
                    continue
                sl, tp = Dn(e.get("sl")), Dn(e.get("tp"))
                if sl != p["sl"]:
                    p["sl_t"] = float(e["t"])
                if tp != p["tp"]:
                    p["tp_t"] = float(e["t"])
                B.rg_algo_ok(p, sl, tp)
        elif k in ("algo_gone", "algo_cancel"):
            for p in G["pos"]:
                if not p["out"] and p.get("aid") and p.get("aid") == e.get("aid"):
                    p["aid_x"], p["aid"] = p["aid"], None
        elif k == "exit":
            p = find_open(G, e["nm"], e.get("acct"))
            if p is None:
                warn.append(f"{e['nm']} {hm(float(e['t']))} 出場找不到持倉")
                continue
            if p["kind"] == "sw" and Dn(e.get("sz")) is not None and Dn(e.get("sz")) != p.get("sz"):
                warn.append(f"{e['nm']} {hm(float(e['t']))} 只平掉一部分（{e.get('sz')}／{p.get('sz')}），這個工具不處理")
            p["out"] = (float(e.get("t_out") or e["t"]), Dn(e.get("px")), e.get("why"))
            if e.get("xoid"):
                G["seen"].append(e["xoid"])
            if p["kind"] == "sp" and Dn(e.get("sold")) is not None:          # 現貨賣剩的零頭（留在帳戶）
                G["_dust"][p["acct"]] = G["_dust"].get(p["acct"], D(0)) + (p.get("got") or p["qty"]) - Dn(e.get("sold"))
            n_exit += 1
        elif k == "funding":
            B.rg_funding(G, e.get("acct"), e.get("side"), D(str(e.get("amt"))))
        elif k == "set_lev":
            if str(e.get("code")) == "0" and str(e.get("sCode") or "0") == "0":
                G["lev_set"][f"{e.get('acct')}|{e.get('side')}"] = str(e.get("lever"))
    clk.t = None
    G["pos"] = [p for p in G["pos"] if not p["out"]]
    G["seen"] = G["seen"][-300:]
    return list(orders.values()) + amb, n_exit


async def okx_order(B, G, o):
    if not o.get("oid"):
        return {}
    r = await B.api("GET", f"/api/v5/trade/order?instId={G[o['kd']]['iid']}&ordId={o['oid']}", acct=o["acct"])
    return B.rg_d0(r) if str(r.get("code")) == "0" else {}


async def adopt(B, syms, off=D("0.1"), live=True, out=print):
    """接管：回傳 (要寫進存檔的組 [G], 有沒有問題)。live＝真的撤單（False＝只查、不改）。"""
    global B_nm
    B_nm = lambda p: B.gd_nm(p["kind"], p["k"])
    evs_all = read_jsonl(B.RG_EV_FILE)
    logs_all = read_jsonl(B.RG_LOG_FILE)
    try:
        cur = json.load(open(B.RG_FILE)) if os.path.exists(B.RG_FILE) else []
    except Exception:
        cur = []
    have_syms = {d.get("sym") for d in cur} | set(B.RD)
    real_ev, real_time, real_print = B.rg_ev, B.time, B.print
    clk = Clock(real_time)
    groups, bad = [], False
    B.rg_ev = lambda *a, **k: None                         # 重建時不再寫事件紀錄、不印 bot 自己的訊息
    B.print = lambda *a, **k: None
    try:
        for sym in syms:
            warn = []
            out(f"━━━━━━━━━━\n{sym}")
            if sym in have_syms:
                out("  存檔裡已經有這個幣（bot 已經在管）→ 不動")
                continue
            recs = [r for r in logs_all if r.get("sym") == sym and r.get("real")]
            if not recs:
                out("  ❌ 找不到這個幣的 /run 出場紀錄（不知道方向、每層U、開始時間）→ 不動")
                bad = True
                continue
            t_run = max(float(r["t_run"]) for r in recs)
            recs = [r for r in recs if abs(float(r["t_run"]) - t_run) < 0.01]
            dir_ = recs[-1]["dir"]
            amt = D(str(next((r["amt"] for r in recs if not r.get("up")), recs[-1]["amt"])))
            evs = sorted([e for e in evs_all if e.get("sym") == sym and float(e.get("t") or 0) >= t_run - 5], key=lambda e: float(e["t"]))
            out(f"  開始 {hm(t_run)}｜{dir_}｜每層 {amt}U｜事件 {len(evs)} 筆｜出場紀錄 {len(recs)} 筆")
            has = {x: x in dir_ for x in B.GD_DIRS}
            sp, sw = await B.gd_specs(sym, has)
            G = B.gd_new({"sym": sym, "dir": dir_, "amt": amt, "off": off}, sp, sw, B.CHAT_ID, t_run, None)
            B.rg_init(G)
            G["_ev"], G["_dust"] = [], {}
            if sw:
                G["mmr"] = await B.rn_mmr({"iid": sw["iid"]})
            G["fee"] = {}
            for a in B.GD_XACCT:
                f = await B.fd_fee_for(a, sym)
                G["fee"][f"sp:{a}"] = f.get("sp") or B.GD_FEE_DEF["sp"]
                G["fee"][a] = f.get("sw") or B.GD_FEE_DEF["sw"]
            B.time = clk
            try:
                left, n_exit = replay(B, G, evs, clk, warn)
            finally:
                B.time = real_time
            for r in recs:                                 # 累計（/status）＝出場紀錄（跟 bot 記的一樣）
                g, net, fund = D(str(r["gp"])), D(str(r["np"])), D(str(r["fp"]))
                B.gd_agg_add(G, r["nm"], {"g": g, "fee": g - net + fund, "net": net, "gu": D(str(r["gu"])), "feeu": D(str(r["feeu"])),
                                          "netu": D(str(r["u"])), "fu": D(str(r["fu"])), "fund": fund, "fn": int(r.get("fn") or 0)})
            if n_exit != len(recs):
                warn.append(f"事件裡的出場 {n_exit} 筆，出場紀錄 {len(recs)} 筆，不一樣")
            if G["org"] is None and G["has"]["L"]:
                warn.append("找不到 L00 成交（原點）")
            out(f"  原點 {G['org']['px'] if G['org'] else '-'}｜關機前持倉 {len(G['pos'])} 單：" + "、".join(f"{B_nm(p)}{'(有SL/TP)' if p.get('aid') else ''}" for p in G["pos"]))
            # ---- 查 OKX：關機期間的進場單、條件單、持倉 ----
            iids = {G[kd]["iid"] for kd in B.gd_kinds(G)}
            new_pos, n_cxl = [], 0
            for o in left:
                od = await okx_order(B, G, o)
                st = od.get("state")
                nm = B.gd_nm(o["kd"], o["k"])
                if st in ("live", "partially_filled"):
                    if not live:
                        out(f"  {nm} 進場單還掛著 {o['px']}（接管時會撤掉，bot 開起來照現價重掛）")
                        continue
                    await B.api("POST", "/api/v5/trade/cancel-order", {"instId": G[o["kd"]]["iid"], "ordId": o["oid"]}, acct=o["acct"])
                    for _ in range(10):
                        od = await okx_order(B, G, o)
                        if od.get("state") in WAIT_OK:
                            break
                        await asyncio.sleep(0.5)
                    st = od.get("state")
                    if st not in WAIT_OK:
                        warn.append(f"{nm} 進場單撤不掉（{st}）")
                        continue
                    n_cxl += 1
                acc = B.rg_D(od.get("accFillSz"))
                if st in WAIT_OK:                          # 寫回存檔時補記事件（下次重建才接得上）
                    G["_ev"].append(("entry_end", {"nm": nm, "acct": o["acct"], "oid": o["oid"], "state": st, "acc": str(acc), "by": "rg_adopt"}))
                if acc > 0:
                    G["px"][o["kd"]] = B.rg_D(od.get("avgPx")) or o["px"]
                    p = B.rg_pos_new(G, o, od)
                    G["pos"].append(p)
                    new_pos.append(nm)
                    G["_ev"].append(("fill", {"nm": nm, "acct": o["acct"], "oid": od.get("ordId"), "ent": str(p["ent"]), "sz": str(acc),
                                              "fee": od.get("fee"), "feeCcy": (od.get("feeCcy") or "").upper(), "t_in": p["t_in"],
                                              "state": st, "by": "rg_adopt"}))
                    if o["kd"] == "sp" and o["k"] == 0 and G["org"] is None:
                        G["org"] = {"px": o["px"], "t": p["t_in"]}
                elif st is None:
                    warn.append(f"{nm} 進場單 OKX 查不到")
            if new_pos:
                out("  關機期間成交（補上）：" + "、".join(new_pos))
            if n_cxl:
                out(f"  撤掉還掛著的進場單 {n_cxl} 張（bot 開起來照現價重掛）")
            known = {o.get("oid") for o in left}
            for a in B.GD_XACCT:
                r = await B.api("GET", "/api/v5/trade/orders-pending?limit=100", acct=a)
                for od in (r.get("data") or []):
                    if od.get("instId") in iids and str(od.get("clOrdId") or "").startswith(B.RG_PFX) and od.get("ordId") not in known:
                        if B.rg_D(od.get("accFillSz")) > 0:
                            warn.append(f"{a} 有一張 /run 的進場單事件裡找不到、而且部分成交了 {od.get('instId')} {od.get('px')}")
                        elif live:
                            await B.api("POST", "/api/v5/trade/cancel-order", {"instId": od["instId"], "ordId": od["ordId"]}, acct=a)
                            out(f"  {a} 撤掉事件裡找不到的 /run 進場單 {od.get('instId')} {od.get('side')} {od.get('px')}")
            live_alg = {}
            for a in B.GD_XACCT:
                r = await B.rg_algo_pending(a)
                for x in (r.get("data") or []):
                    if x.get("instId") in iids:
                        live_alg[x.get("algoId")] = (a, x)
            gone = []
            for p in G["pos"]:
                aid = p.get("aid")
                if not aid:
                    continue
                if aid in live_alg:
                    x = live_alg[aid][1]
                    p["sl"], p["tp"] = Dn(x.get("slTriggerPx")), Dn(x.get("tpTriggerPx"))
                    p["sl_g"] = B.rn_g(p["side"], p["ent"], p["sl"]) if p["sl"] is not None else None
                    p["tp_g"] = B.rn_g(p["side"], p["ent"], p["tp"]) if p["tp"] is not None else None
                else:
                    r = await B.api("GET", f"/api/v5/trade/orders-algo-history?ordType=oco&algoId={aid}", acct=p["acct"])
                    st = (B.rg_d0(r) or {}).get("state")
                    gone.append((p, st))
            claimed = {p.get("aid") for p in G["pos"]}
            for aid, (a, x) in live_alg.items():
                if aid not in claimed and str(x.get("algoClOrdId") or "").startswith(B.RG_APFX):
                    warn.append(f"{a} 條件單 {x.get('instId')} SL {x.get('slTriggerPx')}|TP {x.get('tpTriggerPx')} 沒有對應的持倉")
            for p, st in gone:
                out(f"  {B_nm(p)} 條件單關機期間{'已觸發(出場)' if st == 'effective' else ('被取消' if st == 'canceled' else f'不在 OKX({st})')}"
                    "→ bot 開起來照 OKX 補記")
            trig = {id(p) for p, st in gone if st == "effective"}
            # ---- 現價（最高／最低毛利率）、跟 OKX 對張數、顆數 ----
            for kd in B.gd_kinds(G):
                px = await B.get_last(G[kd]["iid"])
                G["px"][kd] = px
                for p in G["pos"]:
                    if p["kind"] == kd and px:
                        gg = B.rn_g(p["side"], p["ent"], px)
                        p["mfe"], p["mae"] = max(p["mfe"], gg), min(p["mae"], gg)
            for a in B.GD_XACCT:
                if G.get("sw"):
                    r = await B.api("GET", f"/api/v5/account/positions?instType=SWAP&instId={G['sw']['iid']}", acct=a)
                    okx = {x.get("posSide"): abs(B.rg_D(x.get("pos"))) for x in (r.get("data") or [])}
                    for side in ("S", "L"):
                        bot = sum((p["sz"] for p in G["pos"] if p["kind"] == "sw" and p["acct"] == a and p["side"] == side and id(p) not in trig), D(0))
                        have = okx.get(B.rg_ps(side), D(0))
                        if bot or have:
                            ok = bot == have
                            out(f"  {a} 合約{'空' if side == 'S' else '多'}單 BOT {bot}張｜OKX {have}張 {'✅' if ok else '❌'}")
                            if not ok:
                                warn.append(f"{a} 合約{'空' if side == 'S' else '多'}單張數對不上")
                if G.get("sp"):
                    base = sym[:-4]
                    r = await B.api("GET", f"/api/v5/account/balance?ccy={base}", acct=a)
                    x = next((d for d in ((r.get("data") or [{}])[0].get("details") or []) if d.get("ccy") == base), {})
                    have = B.rg_D(x.get("cashBal"))
                    bot = sum((p.get("got") or p["qty"] for p in G["pos"] if p["kind"] == "sp" and p["acct"] == a and id(p) not in trig), D(0))
                    tol = (G["sp"].get("lot") or B.GD_LOT0) * 2
                    dust = G["_dust"].get(a, D(0)) + sum(((p.get("got") or p["qty"]) - (p.get("ssz") or 0) for p in G["pos"]
                                                          if p["kind"] == "sp" and p["acct"] == a and id(p) in trig and p.get("ssz")), D(0))
                    top = bot + dust + max(G["sp"].get("minsz") or D(0), tol) + tol   # 開始前不到最小數量的零頭也算
                    if bot or have > tol:
                        ok = bot - tol <= have <= top
                        out(f"  {a} 現貨 BOT {bot}顆｜OKX {have}顆 {'✅' if ok else '❌'}" + ("（多的是之前賣剩的零頭）" if ok and have > bot + tol else ""))
                        if not ok:
                            warn.append(f"{a} 現貨顆數對不上（OKX {'比 BOT 少' if have < bot - tol else f'比 BOT 多 {have - bot - dust} 顆，不是零頭'}）")
            for w in warn:
                out(f"  ⚠️ {w}")
            if warn:
                bad = True
                out("  ❌ 這個幣有對不上的地方 → 不接管" + ("（只撤了上面那幾張還沒成交的進場單）" if live and n_cxl else "（什麼都沒改）"))
                continue
            out(f"  ✅ 接管：持倉 {len(G['pos'])} 單" + (f"（{sum(1 for p in G['pos'] if p.get('aid'))} 單有 SL/TP）" if G["pos"] else ""))
            groups.append(G)
    finally:
        B.rg_ev, B.time, B.print = real_ev, real_time, real_print
    return groups, bad


async def two_pass(B, syms, off=D("0.1"), out=print):
    """先全部只查（不改任何東西）→ 對得上的幣才真的接管（撤還沒成交的進場單、補關機期間成交的）。對不上的印出原因。"""
    buf, cur = {}, [None]

    def keep(x):
        if x.startswith("━━━━━━━━━━\n"):
            cur[0] = x.split("\n", 1)[1]
        buf.setdefault(cur[0], []).append(x)
    g0, bad0 = await adopt(B, syms, off=off, live=False, out=keep)
    ok_syms = [G["sym"] for G in g0]
    for s in syms:
        if s not in ok_syms:
            out("\n".join(buf.get(s) or [f"━━━━━━━━━━\n{s}"]))
    if not ok_syms:
        return [], True
    g1, bad1 = await adopt(B, ok_syms, off=off, live=True, out=out)
    return g1, bad0 or bad1


def write(B, groups, stamp):
    """寫回 rungrid 存檔（原本的備份 .bak-adopt-日期時間）。"""
    f = B.RG_FILE
    cur = json.load(open(f)) if os.path.exists(f) else []
    if os.path.exists(f):
        shutil.copy2(f, f"{f}.bak-adopt-{stamp}")
    data = cur + [B.gd_dump(G) for G in groups]
    tmp = f + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, f)
    for G in groups:
        for k, kw in G.pop("_ev", []):
            B.rg_ev(k, G, **kw)
        B.rg_ev("adopt", G, pos=[B.gd_nm(p["kind"], p["k"]) for p in G["pos"]], by="rg_adopt.py")
    return f


def own_like(path, ref):
    try:
        st = os.stat(ref)
        if os.geteuid() == 0:
            os.chown(path, st.st_uid, st.st_gid)
    except Exception:
        pass


def load_bot(path, acct):
    os.environ["ACCT"] = acct
    spec = importlib.util.spec_from_file_location("run_bot", path)
    B = importlib.util.module_from_spec(spec)
    sys.modules["run_bot"] = B
    spec.loader.exec_module(B)
    return B


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    acct = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--acct=")), "o1111o")
    off = D(next((a.split("=", 1)[1].rstrip("%") for a in sys.argv[1:] if a.startswith("--off=")), "0.1"))
    syms = [a.upper() for a in args if a.upper().endswith("USDT")]
    if not syms:
        print("用法:sudo /srv/1111bot/.venv/bin/python /tmp/rg_adopt.py STRKUSDT STXUSDT WLDUSDT")
        sys.exit(1)
    if os.geteuid() != 0:
        print("已中止:要用 sudo 跑,什麼都沒動")
        sys.exit(1)
    unit = f"1111bot-{acct}-normal.service"
    path = "/srv/1111bot/run_bot_next.py" if acct == "o1111o" else "/srv/1111bot/run_bot.py"
    active = lambda: subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip() == "active"
    was = active()
    print(f"停止 {acct} 的 bot(bot 停的時候不會寫存檔)" if was else f"{acct} 的 bot 本來就沒在跑")
    if was:
        subprocess.run(["systemctl", "stop", unit])
        time.sleep(2)
        if active():
            print("已中止:bot 停不下來,什麼都沒動")
            sys.exit(1)
    try:
        B = load_bot(path, acct)
        print(f"程式 {path} {B.VERSION}")
        import httpx
        B.HTTP = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0),
                                   transport=httpx.AsyncHTTPTransport(local_address=B.OKX_V4, limits=httpx.Limits(max_connections=20)))
        B.load_state()

        async def go():
            await B.okx_specs_all()
            await B.okx_spot_specs_all()
            return await two_pass(B, syms, off)
        groups, bad = asyncio.run(go())
        if groups:
            stamp = datetime.now(TZ8).strftime("%Y%m%d-%H%M%S")
            f = write(B, groups, stamp)
            own_like(f, os.path.dirname(f))
            own_like(f"{f}.bak-adopt-{stamp}", os.path.dirname(f))
            own_like(B.RG_EV_FILE, os.path.dirname(f))
            print("━━━━━━━━━━")
            print(f"✅ 已寫回存檔:{' '.join(G['sym'] for G in groups)}(原本的存檔備份 .bak-adopt-{stamp})")
        else:
            print("━━━━━━━━━━")
            print("沒有接管任何幣種(存檔沒有改)")
        if bad:
            print("⚠️ 有幣種沒接管,把上面整段貼給 Claude")
    except Exception as e:
        print("已中止:", type(e).__name__, e, "(存檔沒有改)")
    finally:
        if was:
            subprocess.run(["systemctl", "start", unit])
            time.sleep(8)
            print(f"bot 已經開回來:{acct}" if active() else f"⚠️ {acct} 的 bot 沒開起來")


if __name__ == "__main__":
    main()
