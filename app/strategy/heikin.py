"""均K 訊號判定（B4-2 驗證，使用者已核對燈號正確）。
進場：PRE根反轉前色 + POST根反轉後色，POST振幅累加>=門檻。
出場：EXIT根反向色，EXIT振幅累加>=門檻。振幅只加 POST/EXIT。"""
from decimal import Decimal

def judge_entry(ha, direction, PRE, POST, amp_req, idx):
    need = PRE + POST
    if idx < need - 1:
        return None
    seg = ha[idx-need+1:idx+1]
    pre_seg, post_seg = seg[:PRE], seg[PRE:]
    if direction == "L":
        pre_ok  = all(x["color"] == "R" for x in pre_seg)
        post_ok = all(x["color"] == "G" for x in post_seg)
    else:
        pre_ok  = all(x["color"] == "G" for x in pre_seg)
        post_ok = all(x["color"] == "R" for x in post_seg)
    amp_sum = sum((x["amp"] for x in post_seg), Decimal(0))
    return {"seg": seg, "pre_ok": pre_ok, "post_ok": post_ok,
            "amp_sum": amp_sum, "amp_ok": amp_sum >= amp_req,
            "hit": pre_ok and post_ok and amp_sum >= amp_req}

def judge_exit(ha, direction, EXIT, amp_req, idx):
    if idx < EXIT - 1:
        return None
    seg = ha[idx-EXIT+1:idx+1]
    want = "R" if direction == "L" else "G"
    color_ok = all(x["color"] == want for x in seg)
    amp_sum = sum((x["amp"] for x in seg), Decimal(0))
    return {"seg": seg, "color_ok": color_ok,
            "amp_sum": amp_sum, "amp_ok": amp_sum >= amp_req,
            "hit": color_ok and amp_sum >= amp_req}
