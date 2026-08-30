# -*- coding: utf-8 -*-
import io, sys, os, re

p = os.environ.get("TARGET", "/srv/1111bot/run_bot.py")
s = io.open(p, encoding="utf-8").read()
fails = []

def rep(old, new, label):
    global s
    n = s.count(old)
    if n != 1:
        fails.append("FAIL: %s (found %d)" % (label, n))
        return
    s = s.replace(old, new)
    print("OK: " + label)

MOD = '# ---------- /amp 振幅報表（Excel + Email） ----------\nAMP_MAX = 2000       # 單次最多抓幾根\nAMP_BINS = [Decimal(str(x)) for x in\n            ("0.1","0.2","0.3","0.4","0.5","0.6","0.7","0.8","0.9","1.0","1.2","1.5")]\n\nasync def get_klines_paged(iid, bar, want):\n    """分頁往前抓 K 線（OKX 單次上限 300），回傳舊->新、只含已收線。"""\n    out = []\n    after = ""\n    for _ in range(12):\n        q = f"/api/v5/market/candles?instId={iid}&bar={bar}&limit=300"\n        if after:\n            q += f"&after={after}"\n        r = await pub(q)\n        if r.get("code") != "0":\n            break\n        batch = r.get("data") or []\n        if not batch:\n            break\n        got = []\n        for c in batch:\n            try:\n                if len(c) >= 9 and str(c[8]) != "1":\n                    continue\n                got.append({"ts": int(c[0]), "o": Decimal(c[1]), "h": Decimal(c[2]),\n                            "l": Decimal(c[3]), "c": Decimal(c[4])})\n            except Exception:\n                continue\n        if not got:\n            break\n        out.extend(got)                      # OKX 回傳為新->舊\n        after = str(min(int(x["ts"]) for x in got))\n        if len(out) >= want + 10:\n            break\n        await asyncio.sleep(0.15)\n    out.sort(key=lambda x: x["ts"])          # 轉成舊->新\n    seen = set(); uniq = []\n    for k in out:\n        if k["ts"] in seen:\n            continue\n        seen.add(k["ts"]); uniq.append(k)\n    return uniq[-want:] if want else uniq\n\nasync def klines_paged_for_tf(iid, tf, want):\n    """依 TF 分頁取 K 線。10m 由兩根 5m 合成。"""\n    if tf == "10m":\n        return _merge2(await get_klines_paged(iid, "5m", want * 2 + 4))\n    bar = NATIVE_BARS.get(tf)\n    if not bar:\n        return None\n    return await get_klines_paged(iid, bar, want)\n\ndef build_amp_xlsx(sym, tf, kl, amps, path):\n    """產生兩個工作表：明細 + 統計。"""\n    from openpyxl import Workbook\n    from openpyxl.styles import Font, Alignment\n    FONT = "PingFang TC"; SIZE = 12\n    wb = Workbook()\n\n    ws = wb.active; ws.title = "明細"\n    heads = ["時間", "漲跌", "開", "高", "低", "收", "振幅%(前收)", "振幅%(本收)"]\n    widths = {"A": 3.3, "B": 17, "C": 6, "D": 14, "E": 14, "F": 14, "G": 14, "H": 14, "I": 14}\n    for col, w in widths.items():\n        ws.column_dimensions[col].width = w\n    for i, h in enumerate(heads):\n        c = ws.cell(row=2, column=2 + i, value=h)\n        c.font = Font(name=FONT, size=SIZE, bold=True)\n        c.alignment = Alignment(horizontal="center")\n    r = 3\n    for k, (a1, a2) in zip(kl, amps):\n        t = datetime.fromtimestamp(int(k["ts"]) / 1000, TZ8).strftime("%Y-%m-%d %H:%M")\n        vals = [t, "漲" if k["c"] >= k["o"] else "跌",\n                float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),\n                float(a1) / 100, float(a2) / 100]\n        for i, v in enumerate(vals):\n            cc = ws.cell(row=r, column=2 + i, value=v)\n            cc.font = Font(name=FONT, size=SIZE)\n        for col in (8, 9):\n            ws.cell(row=r, column=col).number_format = "0.0000%"\n        r += 1\n    ws.freeze_panes = "A3"\n\n    st = wb.create_sheet("統計")\n    for col, w in {"A": 3.3, "B": 18, "C": 14, "D": 12}.items():\n        st.column_dimensions[col].width = w\n    seg = [a for a, _ in amps]\n    ss = sorted(seg); m = len(ss)\n    med = ss[m // 2] if m % 2 else (ss[m // 2 - 1] + ss[m // 2]) / 2\n    avg = sum(seg, Decimal(0)) / m\n    t0 = datetime.fromtimestamp(int(kl[0]["ts"]) / 1000, TZ8).strftime("%Y-%m-%d %H:%M")\n    t1 = datetime.fromtimestamp(int(kl[-1]["ts"]) / 1000, TZ8).strftime("%Y-%m-%d %H:%M")\n    rows = [("商品", sym, None), ("週期", tf, None), ("根數", m, None),\n            ("期間起", t0, None), ("期間迄", t1, None), ("", "", None),\n            ("平均振幅", float(avg) / 100, "pct"),\n            ("中位振幅", float(med) / 100, "pct"),\n            ("最大振幅", float(max(seg)) / 100, "pct"),\n            ("最小振幅", float(min(seg)) / 100, "pct"),\n            ("", "", None),\n            ("達標門檻", "根數", "佔比")]\n    rr = 2\n    for a, b, kind in rows:\n        ca = st.cell(row=rr, column=2, value=a); ca.font = Font(name=FONT, size=SIZE, bold=(kind is None and b == "根數"))\n        cb = st.cell(row=rr, column=3, value=b); cb.font = Font(name=FONT, size=SIZE)\n        if kind == "pct":\n            cb.number_format = "0.0000%"\n        if a == "達標門檻":\n            ca.font = Font(name=FONT, size=SIZE, bold=True)\n            cb.font = Font(name=FONT, size=SIZE, bold=True)\n            cc = st.cell(row=rr, column=4, value="佔比"); cc.font = Font(name=FONT, size=SIZE, bold=True)\n        rr += 1\n    for b in AMP_BINS:\n        n = sum(1 for a in seg if a >= b)\n        st.cell(row=rr, column=2, value="≥ " + str(b) + "%").font = Font(name=FONT, size=SIZE)\n        st.cell(row=rr, column=3, value=n).font = Font(name=FONT, size=SIZE)\n        cd = st.cell(row=rr, column=4, value=n / m)\n        cd.font = Font(name=FONT, size=SIZE); cd.number_format = "0.00%"\n        rr += 1\n    wb.save(path)\n\ndef send_amp_mail(path, name, sym, tf, m, avg, med):\n    """寄出振幅報表。回傳 (ok, 訊息)。"""\n    import smtplib\n    from email.message import EmailMessage\n    env = {}\n    try:\n        for line in open("/srv/1111bot/.env"):\n            line = line.strip()\n            if "=" in line and not line.startswith("#"):\n                k, v = line.split("=", 1)\n                env[k.strip()] = v.strip().strip(\'"\').strip("\'")\n    except Exception as e:\n        return False, "讀 .env 失敗：%s" % e\n    user = env.get("GMAIL_USER")\n    pwd = (env.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")\n    to = env.get("REPORT_TO") or user\n    if not user or not pwd:\n        return False, "未設定 GMAIL_USER / GMAIL_APP_PASSWORD"\n    msg = EmailMessage()\n    msg["Subject"] = "OKX %s 振幅報表 %s %s（%d 根）" % (ACCT, sym, tf, m)\n    msg["From"] = user; msg["To"] = to\n    msg.set_content("商品 %s\\\\n週期 %s\\\\n根數 %d\\\\n平均振幅 %.4f%%\\\\n中位振幅 %.4f%%\\\\n" % (sym, tf, m, avg, med))\n    msg.add_attachment(open(path, "rb").read(),\n                       maintype="application",\n                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",\n                       filename=name)\n    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as sv:\n        sv.login(user, pwd); sv.send_message(msg)\n    return True, to\n\n'
CMD = 'async def cmd_amp(u, c):\n    """原K 振幅報表（Excel 寄信）。用法：/amp SOLUSDT [根數 3~2000]"""\n    supported = "3m/5m/10m/15m/30m/60m"\n    if not c.args:\n        await reply(u, f"{E.BOT} 用法：/amp SOLUSDT 900\\n"\n                       f"根數 3~{AMP_MAX}（預設 300）\\n"\n                       f"產生 Excel（明細＋統計）寄到信箱\\n"\n                       f"支援週期：{supported}\\n"\n                       f"目前週期：{ACCOUNT_TF}")\n        return\n    sym = c.args[0].upper()\n    n = 300\n    if len(c.args) >= 2:\n        try: n = max(3, min(AMP_MAX, int(c.args[1])))\n        except Exception: pass\n    if ACCOUNT_TF != "10m" and ACCOUNT_TF not in NATIVE_BARS:\n        await reply(u, f"{E.BOT} 目前週期 {ACCOUNT_TF} 無原生 K 線\\n"\n                       f"可查週期：{supported}\\n請先 /timeframe 切換")\n        return\n    try:\n        spec = await get_spec(sym)\n    except Exception:\n        await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return\n    await reply(u, f"{E.BOT} 產生 {sym} {ACCOUNT_TF} 振幅報表中（{n} 根），請稍候…")\n    try:\n        kl = await klines_paged_for_tf(spec["iid"], ACCOUNT_TF, n)\n    except Exception as e:\n        await reply(u, f"{E.LOSS} K 線取得失敗：{type(e).__name__}"); return\n    if not kl:\n        await reply(u, f"{E.BOT} {sym} K 線取得失敗"); return\n    amps = calc_amp(kl)\n    m = len(kl)\n    seg = [a for a, _ in amps]\n    ss = sorted(seg)\n    med = ss[m // 2] if m % 2 else (ss[m // 2 - 1] + ss[m // 2]) / 2\n    avg = sum(seg, Decimal(0)) / m\n    day = now8().strftime("%Y%m%d")\n    name = f"OKX_{ACCT}_振幅_{sym}_{ACCOUNT_TF}_{m}根_{day}.xlsx"\n    path = f"/srv/1111bot/data/{name}"\n    try:\n        build_amp_xlsx(sym, ACCOUNT_TF, kl, amps, path)\n    except Exception as e:\n        await reply(u, f"{E.LOSS} 產生 Excel 失敗：{type(e).__name__}: {e}"); return\n    try:\n        ok, info = send_amp_mail(path, name, sym, ACCOUNT_TF, m, float(avg), float(med))\n    except Exception as e:\n        await reply(u, f"{E.LOSS} 寄送失敗：{type(e).__name__}: {e}\\n檔案已存於 VPS：{name}"); return\n    if not ok:\n        await reply(u, f"{E.LOSS} 未寄送：{info}\\n檔案已存於 VPS：{name}"); return\n    t0 = datetime.fromtimestamp(int(kl[0]["ts"]) / 1000, TZ8).strftime("%m/%d %H:%M")\n    t1 = datetime.fromtimestamp(int(kl[-1]["ts"]) / 1000, TZ8).strftime("%m/%d %H:%M")\n    await reply(u, f"{E.BOT} ✅ 振幅報表已寄出\\n"\n                   f"{sym} {ACCOUNT_TF}｜{m} 根\\n"\n                   f"期間：{t0} ~ {t1}\\n"\n                   f"平均 {avg:.4f}% | 中位 {med:.4f}%\\n"\n                   f"最大 {max(seg):.4f}% | 最小 {min(seg):.4f}%\\n"\n                   f"時間：{hhmmss()}")\n\n'

# 1) 移除舊的 cmd_amp（TG 明細版），改成 Excel 版
i = s.find("async def cmd_amp(u, c):")
j = s.find("async def cmd_coins(u, c):")
if i == -1 or j == -1 or j <= i:
    fails.append("FAIL: 找不到舊 cmd_amp 區塊")
else:
    s = s[:i] + CMD + s[j:]
    print("OK: 1. 取代 cmd_amp 為 Excel 版")

# 2) 插入模組（分頁抓K、Excel、寄信）
A = "async def cmd_amp(u, c):"
if s.count(A) == 1:
    s = s.replace(A, MOD + A, 1)
    print("OK: 2. 插入振幅模組")
else:
    fails.append("FAIL: cmd_amp 錨點數量 %d" % s.count(A))

# 3) Menu 說明改成 Excel 版
rep('"/amp \u5546\u54c1 \u6839\u6578  \u632f\u5e45\u6aa2\u8996\uff083~300\u6839\uff09\\n"',
    '"/amp \u5546\u54c1 \u6839\u6578  \u632f\u5e45\u5831\u8868 Excel \u5bc4\u4fe1\uff083~2000\u6839\uff09\\n"',
    "3. menu 文字")

rep('BotCommand("amp", "\u632f\u5e45\u6aa2\u8996 3~300\u6839")',
    'BotCommand("amp", "\u632f\u5e45\u5831\u8868 Excel")',
    "4. menu 按鈕")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")
