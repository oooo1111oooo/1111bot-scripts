# -*- coding: utf-8 -*-
import io, sys, os

p = os.environ.get("TARGET", "/srv/1111bot/run_bot.py")
s = io.open(p, encoding="utf-8").read()
fails = []

CALC = 'def calc_amp(kl):\n    """每根回傳 (振幅%, 漲跌幅%)。\n    振幅% = (高-低) / 前一根收盤 * 100（恆正）\n    漲跌幅% = (收盤-前一根收盤) / 前一根收盤 * 100（帶正負）\n    第一根無前收，以本根開盤價替代。"""\n    out = []\n    for i, k in enumerate(kl):\n        base = kl[i-1]["c"] if i > 0 else k["o"]\n        if not base:\n            out.append((Decimal(0), Decimal(0))); continue\n        amp = (k["h"] - k["l"]) / base * 100\n        chg = (k["c"] - base) / base * 100\n        out.append((amp, chg))\n    return out\n'
XLSX = 'def build_amp_xlsx(sym, tf, kl, amps, path):\n    """產生兩個工作表：明細 + 統計。日期與時間分欄，便於樞紐分析。"""\n    from openpyxl import Workbook\n    from openpyxl.styles import Font, Alignment\n    FONT = "PingFang TC"; SIZE = 12\n    wb = Workbook()\n\n    ws = wb.active; ws.title = "明細"\n    heads = ["日期", "時間", "漲跌", "開", "高", "低", "收", "振幅%", "漲跌幅%"]\n    widths = {"A": 3.3, "B": 11, "C": 8.5, "D": 6, "E": 13, "F": 13, "G": 13, "H": 13, "I": 11, "J": 11}\n    for col, w in widths.items():\n        ws.column_dimensions[col].width = w\n    for i, h in enumerate(heads):\n        c = ws.cell(row=2, column=2 + i, value=h)\n        c.font = Font(name=FONT, size=SIZE, bold=True)\n        c.alignment = Alignment(horizontal="center")\n    r = 3\n    for k, (amp, chg) in zip(kl, amps):\n        dt = datetime.fromtimestamp(int(k["ts"]) / 1000, TZ8)\n        vals = [dt.date(), dt.strftime("%H:%M:%S"),\n                "\\U0001f7e9" if k["c"] >= k["o"] else "\\U0001f7e5",\n                float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),\n                float(amp) / 100, float(chg) / 100]\n        for i, v in enumerate(vals):\n            cc = ws.cell(row=r, column=2 + i, value=v)\n            cc.font = Font(name=FONT, size=SIZE)\n        ws.cell(row=r, column=2).number_format = "yyyy-mm-dd"\n        ws.cell(row=r, column=3).number_format = "@"\n        ws.cell(row=r, column=4).alignment = Alignment(horizontal="center")\n        ws.cell(row=r, column=9).number_format = "0.0000%"\n        ws.cell(row=r, column=10).number_format = \'0.0000%;[Red]-0.0000%\'\n        r += 1\n    ws.freeze_panes = "A3"\n\n    st = wb.create_sheet("統計")\n    for col, w in {"A": 3.3, "B": 18, "C": 20, "D": 12}.items():\n        st.column_dimensions[col].width = w\n    seg = [a for a, _ in amps]\n    chgs = [c for _, c in amps]\n    ss = sorted(seg); m = len(ss)\n    med = ss[m // 2] if m % 2 else (ss[m // 2 - 1] + ss[m // 2]) / 2\n    avg = sum(seg, Decimal(0)) / m\n    d0 = datetime.fromtimestamp(int(kl[0]["ts"]) / 1000, TZ8)\n    d1 = datetime.fromtimestamp(int(kl[-1]["ts"]) / 1000, TZ8)\n    up = sum(1 for c in chgs if c > 0); dn = sum(1 for c in chgs if c < 0)\n    rows = [("商品", sym, None), ("週期", tf, None), ("根數", m, None),\n            ("期間起", d0.strftime("%Y-%m-%d %H:%M"), None),\n            ("期間迄", d1.strftime("%Y-%m-%d %H:%M"), None),\n            ("漲/跌根數", "%d / %d" % (up, dn), None), ("", "", None),\n            ("平均振幅", float(avg) / 100, "pct"),\n            ("中位振幅", float(med) / 100, "pct"),\n            ("最大振幅", float(max(seg)) / 100, "pct"),\n            ("最小振幅", float(min(seg)) / 100, "pct"), ("", "", None),\n            ("最大漲幅", float(max(chgs)) / 100, "pct"),\n            ("最大跌幅", float(min(chgs)) / 100, "pct"), ("", "", None)]\n    rr = 2\n    for a, b, kind in rows:\n        st.cell(row=rr, column=2, value=a).font = Font(name=FONT, size=SIZE)\n        cb = st.cell(row=rr, column=3, value=b); cb.font = Font(name=FONT, size=SIZE)\n        if kind == "pct":\n            cb.number_format = \'0.0000%;[Red]-0.0000%\'\n        rr += 1\n    for i, h in enumerate(["振幅達標門檻", "根數", "佔比"]):\n        st.cell(row=rr, column=2 + i, value=h).font = Font(name=FONT, size=SIZE, bold=True)\n    rr += 1\n    for b in AMP_BINS:\n        n = sum(1 for a in seg if a >= b)\n        st.cell(row=rr, column=2, value="\\u2265 " + str(b) + "%").font = Font(name=FONT, size=SIZE)\n        st.cell(row=rr, column=3, value=n).font = Font(name=FONT, size=SIZE)\n        cd = st.cell(row=rr, column=4, value=n / m)\n        cd.font = Font(name=FONT, size=SIZE); cd.number_format = "0.00%"\n        rr += 1\n    wb.save(path)\n'

# 1) 取代 calc_amp
i = s.find("def calc_amp(kl):")
j = s.find("async def _reply_long(u, head, lines, tail):")
if i == -1 or j == -1 or j <= i:
    fails.append("FAIL: 找不到 calc_amp 區塊")
else:
    s = s[:i] + CALC + "\n" + s[j:]
    print("OK: 1. calc_amp -> 振幅% / 漲跌幅%")

# 2) 取代 build_amp_xlsx
i = s.find("def build_amp_xlsx(")
j = s.find("def send_amp_mail(")
if i == -1 or j == -1 or j <= i:
    fails.append("FAIL: 找不到 build_amp_xlsx 區塊")
else:
    s = s[:i] + XLSX + "\n" + s[j:]
    print("OK: 2. Excel 日期時間分欄 + 欄位改名")

# 3) TG 用法說明
a = '\u6b04\u4f4d\uff1a\u6f32\u8dcc\uff5c\u632f\u5e45%\uff08\u524d\u6536\u70ba\u5206\u6bcd\uff09\uff5c\u632f\u5e45%\uff08\u672c\u6536\u70ba\u5206\u6bcd\uff09'
if s.count(a) == 1:
    s = s.replace(a, '\u6b04\u4f4d\uff1a\u65e5\u671f\uff5c\u6642\u9593\uff5c\u6f32\u8dcc\uff5c\u958b\u9ad8\u4f4e\u6536\uff5c\u632f\u5e45%\uff5c\u6f32\u8dcc\u5e45%')
    print("OK: 3. 用法說明")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")
