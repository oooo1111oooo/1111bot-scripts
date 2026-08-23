#!/usr/bin/env python3
"""B4-2 均K訊號判定：PRE/POST/EXIT燈號 + POST/EXIT振幅累加。純計算，不下單。
測試 3m/5m/15m。10m 用相同邏輯兩根5m相加(本輪暫不測)。"""
from decimal import Decimal, getcontext
from datetime import datetime, timezone, timedelta
import httpx

getcontext().prec = 20
BASE = "https://www.okx.com"
INST = "ETH-USDT-SWAP"
TZ8 = timezone(timedelta(hours=8))

def fetch(bar, n=120):
    r = httpx.get(BASE+f"/api/v5/market/candles?instId={INST}&bar={bar}&limit={n}",timeout=15).json()
    rows = list(reversed(r["data"]))
    return [{"ts":int(k[0]),"o":Decimal(k[1]),"h":Decimal(k[2]),
             "l":Decimal(k[3]),"c":Decimal(k[4])} for k in rows]

def calc_ha(kl):
    ha=[]
    for i,k in enumerate(kl):
        hc=(k["o"]+k["h"]+k["l"]+k["c"])/4
        ho=(k["o"]+k["c"])/2 if i==0 else (ha[i-1]["ho"]+ha[i-1]["hc"])/2
        hh=max(k["h"],ho,hc); hl=min(k["l"],ho,hc)
        color="G" if hc>=ho else "R"
        amp=(hh-hl)/k["c"]*100  # 單根振幅%（HA高-低 / 收盤）
        ha.append({"ts":k["ts"],"ho":ho,"hh":hh,"hl":hl,"hc":hc,"color":color,"amp":amp})
    return ha

def emoji(c): return "🟩" if c=="G" else "🟥"

def judge_entry(ha, direction, PRE, POST, amp_req, idx):
    """判斷 idx 這根(已收盤)是否構成進場。
    Long: PRE根紅 + POST根綠, POST振幅累加>=門檻。Short相反。"""
    need = PRE+POST
    if idx < need-1: return None
    seg = ha[idx-need+1:idx+1]  # 參與判定的 PRE+POST 根
    pre_seg = seg[:PRE]; post_seg = seg[PRE:]
    if direction=="L":
        pre_ok = all(x["color"]=="R" for x in pre_seg)
        post_ok= all(x["color"]=="G" for x in post_seg)
    else:
        pre_ok = all(x["color"]=="G" for x in pre_seg)
        post_ok= all(x["color"]=="R" for x in post_seg)
    amp_sum = sum((x["amp"] for x in post_seg), Decimal(0))
    amp_ok = amp_sum >= amp_req
    return {"seg":seg,"pre_ok":pre_ok,"post_ok":post_ok,
            "amp_sum":amp_sum,"amp_ok":amp_ok,
            "hit":pre_ok and post_ok and amp_ok}

def judge_exit(ha, direction, EXIT, amp_req, idx):
    """進場後：連續EXIT根反向燈 + EXIT振幅累加>=門檻。"""
    if idx < EXIT-1: return None
    seg = ha[idx-EXIT+1:idx+1]
    # Long 出場看紅(反向)，Short 出場看綠
    want = "R" if direction=="L" else "G"
    color_ok = all(x["color"]==want for x in seg)
    amp_sum = sum((x["amp"] for x in seg), Decimal(0))
    amp_ok = amp_sum >= amp_req
    return {"seg":seg,"color_ok":color_ok,"amp_sum":amp_sum,
            "amp_ok":amp_ok,"hit":color_ok and amp_ok}

def t8(ts): return datetime.fromtimestamp(ts/1000,TZ8).strftime("%H:%M")

def demo(bar, PRE, POST, EXIT, amp_in, amp_out, direction="L"):
    print(f"\n{'='*60}")
    print(f"【{bar}】方向={direction} PRE={PRE} POST={POST} EXIT={EXIT} "
          f"進場振幅門檻={amp_in}% 出場門檻={amp_out}%")
    print("="*60)
    kl=fetch(bar,120); ha=calc_ha(kl)
    last_closed = len(ha)-2  # 最後一根未收盤，用倒數第2根當「當下已收盤」

    print(f"最新 6 根 HA（最後一根 {t8(ha[-1]['ts'])} 未收盤，判定用前一根）:")
    for x in ha[-6:]:
        mark = "←未收盤" if x["ts"]==ha[-1]["ts"] else ""
        print(f"  {t8(x['ts'])} {emoji(x['color'])} 振幅{x['amp']:.4f}% {mark}")

    print(f"\n進場判定（以 {t8(ha[last_closed]['ts'])} 為當下收盤根）:")
    e=judge_entry(ha,direction,PRE,POST,Decimal(str(amp_in)),last_closed)
    if e:
        seg_str=" ".join(emoji(x["color"]) for x in e["seg"])
        print(f"  參與判定 {PRE}+{POST} 根: {seg_str}")
        print(f"  PRE({PRE}根反轉前): {'✓' if e['pre_ok'] else '✗'}")
        print(f"  POST({POST}根反轉後): {'✓' if e['post_ok'] else '✗'}")
        print(f"  POST振幅累加: {e['amp_sum']:.4f}% / 門檻{amp_in}%  {'✓' if e['amp_ok'] else '✗'}")
        print(f"  → 進場判定: {'★ 成立(下一根開盤 taker 進場)' if e['hit'] else '未成立'}")

    print(f"\n出場判定（假設已持倉，以 {t8(ha[last_closed]['ts'])} 為當下收盤根）:")
    x=judge_exit(ha,direction,EXIT,Decimal(str(amp_out)),last_closed)
    if x:
        seg_str=" ".join(emoji(c["color"]) for c in x["seg"])
        print(f"  參與判定 {EXIT} 根反向燈: {seg_str}")
        print(f"  燈號({EXIT}根{'紅' if direction=='L' else '綠'}): {'✓' if x['color_ok'] else '✗'}")
        print(f"  EXIT振幅累加: {x['amp_sum']:.4f}% / 門檻{amp_out}%  {'✓' if x['amp_ok'] else '✗'}")
        print(f"  → 出場判定: {'★ 成立(下一根開盤 taker 出場)' if x['hit'] else '未成立'}")

def main():
    print("均K 訊號判定測試（3m/5m/15m）— 以 ETH 即時資料回放")
    print("提醒：可對照 OKX HA 圖逐根核對燈號與振幅")
    demo("5m",  PRE=3, POST=2, EXIT=3, amp_in="0.05", amp_out="0.05", direction="L")
    demo("3m",  PRE=2, POST=2, EXIT=2, amp_in="0.03", amp_out="0.03", direction="L")
    demo("15m", PRE=2, POST=2, EXIT=2, amp_in="0.10", amp_out="0.10", direction="L")
    print(f"\n{'='*60}")
    print("✓ B4-2 完成（純計算，未下任何單）")
    print("  10m：確認上述邏輯後，用兩根5m相加(00:00對齊)套用相同判定")

if __name__=="__main__":
    main()
