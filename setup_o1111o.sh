#!/usr/bin/env bash
# ============================================================
# o1111o（OKX 主帳戶）設定：bash /srv/1111bot/setup_o1111o.sh
#   在 VPS 上輸入 OKX API Key、Secret Key、Passphrase、TG bot token（畫面不顯示，不經過 GitHub、不經過對話）
#   → 寫進 config/accounts.env、bots.env → 查 OKX（只查不下單）→ 照 o6666o 建 o1111o 的服務（跑測試版 run_bot_next.py）→ 啟動
#   任何一步不對 → 還原成設定之前的樣子；o2222o～o6666o 完全不動。
#   再跑一次＝把 o1111o 的四個值換成新的（換 API Key 也用這個）。
#   只查 OKX（不輸入、不改任何東西）：bash /srv/1111bot/setup_o1111o.sh check
# ============================================================
B="${BOT_DIR:-/srv/1111bot}"; C="$B/config"; SRC=o6666o; NEW=o1111o
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"; SV="1111bot-$NEW-normal.service"
PY="$B/.venv/bin/python"; [ -x "$PY" ] || PY=python3
cd "$B" || { echo "❌ 找不到 $B"; exit 1; }
okx_check() {   # 用設定檔裡 o1111o 的值查 OKX：0＝正常、2＝Key／Secret／Passphrase 不對、3＝其他（連線、IP）
sudo "$PY" - "$C/accounts.env" "$NEW" <<'PY'
import sys, os, re, json, hmac, hashlib, base64, urllib.request, urllib.error
from datetime import datetime, timezone
d = {}
for line in open(sys.argv[1]):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1); d[k] = v
a = sys.argv[2]
def get(path):
    n = datetime.now(timezone.utc); ts = n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond//1000:03d}Z"
    sg = base64.b64encode(hmac.new(d[f"OKX_{a}_SECRET"].encode(), f"{ts}GET{path}".encode(), hashlib.sha256).digest()).decode()
    h = {"OK-ACCESS-KEY": d[f"OKX_{a}_API_KEY"], "OK-ACCESS-SIGN": sg, "OK-ACCESS-TIMESTAMP": ts,
         "OK-ACCESS-PASSPHRASE": d[f"OKX_{a}_PASSPHRASE"], "Content-Type": "application/json"}
    url = os.environ.get("OKX_BASE", "https://www.okx.com") + path
    try:
        try:
            import httpx                      # 跟 bot 用同一套連線（bot 用 httpx 連得到 OKX）
            r = httpx.get(url, headers=h, timeout=15); code, body = r.status_code, r.text
        except ImportError:
            q = urllib.request.Request(url, headers={**h, "User-Agent": "1111bot/1.0"})
            try:
                with urllib.request.urlopen(q, timeout=15) as r: code, body = r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e: code, body = e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return {"code": "net", "msg": f"{type(e).__name__} {e}"[:150]}
    try: return json.loads(body)
    except Exception:
        return {"code": f"HTTP{code}", "msg": " ".join(re.sub(r"<[^>]+>", " ", body).split())[:150]}
HINT = {"50111": "API Key 不對", "50113": "Secret Key 不對", "50105": "Passphrase 不對", "50101": "這把是模擬盤的 API Key",
        "50110": "這台 VPS 的 IP 不在這把 API Key 綁定的 IP 裡（OKX → API → 編輯 → 綁定 IP）", "50100": "這把 API Key 被凍結",
        "net": "連不到 OKX（網路），稍後再跑一次：bash /srv/1111bot/setup_o1111o.sh check"}
c = get("/api/v5/account/config")
if str(c.get("code")) != "0":
    k = str(c.get("code"))
    print(f"   ❌ OKX 回覆 code={k} {c.get('msg') or ''}")
    print(f"   → {HINT.get(k, '請把這兩行貼給 Claude' if not k.startswith('HTTP') else '這不是 OKX API 的回覆（連線被擋），請把這兩行貼給 Claude')}")
    sys.exit(2 if k in ("50111", "50113", "50105", "50101") else 3)
x = (c.get("data") or [{}])[0]
perm = set((x.get("perm") or "").split(","))
LV = {"1": "現貨模式 ❌ 不能做合約（OKX → 交易設定 → 帳戶模式 改成「合約模式」）", "2": "合約模式 ✅",
      "3": "跨幣種保證金模式 ✅", "4": "組合保證金模式 ✅"}
print("   OKX 回覆：正常 ✅")
print("   帳戶　　：" + ("主帳戶 ✅" if x.get("uid") and x.get("uid") == x.get("mainUid") else "子帳戶 ⚠️（這把 API Key 不是主帳戶的）"))
print("   API 名稱：" + (x.get("label") or "（沒有）"))
print("   權限　　：" + "、".join([w for p, w in (("read_only", "讀取"), ("trade", "交易"), ("withdraw", "提現")) if p in perm])
      + (" ⚠️ 有提現權限，建議到 OKX 拿掉" if "withdraw" in perm else (" ✅" if "trade" in perm else " ⚠️ 沒有交易權限，/run 不能下單")))
print("   綁定 IP ：" + ((x.get("ip") or "") + " ✅" if x.get("ip") else "沒有綁定 ⚠️ 建議綁這台 VPS 的 IP（沒綁 IP 的交易 API Key，OKX 14 天沒用會自動刪除）"))
print("   帳戶模式：" + LV.get(str(x.get("acctLv")), str(x.get("acctLv"))))
print("   持倉模式：" + ("雙向持倉 ✅" if x.get("posMode") == "long_short_mode" else "單向持倉 ⚠️ /run 需要雙向持倉（/runt 不受影響）"))
b = get("/api/v5/account/balance?ccy=USDT")
bd = ((b.get("data") or [{}])[0].get("details") or [{}])
print("   交易帳戶 USDT 權益：" + (str(round(float(bd[0].get("eq") or 0), 4)) if str(b.get("code")) == "0" else "查不到"))
PY
}
if [ "${1:-}" = check ]; then   # bash setup_o1111o.sh check ＝只查 OKX，不輸入、不改任何東西
  sudo grep -q "^OKX_${NEW}_API_KEY=" "$C/accounts.env" || { echo "❌ o1111o 還沒設定（accounts.env 裡沒有 o1111o）"; exit 1; }
  echo "③ 查 OKX（只查、不下單；用設定檔裡 o1111o 現在的值）"
  okx_check; rc=$?
  [ "$rc" = 0 ] && echo "✅ OKX 正常"
  exit $rc
fi
for f in "$C/accounts.env" "$C/bots.env" "$C/acct_$SRC.env" "$B/run_bot.py"; do
  sudo test -f "$f" || { echo "❌ 找不到 $f，未變更"; exit 1; }
done
UNIT_SRC=$(systemctl show -p FragmentPath --value "1111bot-$SRC-normal.service" 2>/dev/null)
{ [ -n "$UNIT_SRC" ] && [ -f "$UNIT_SRC" ]; } || { echo "❌ 找不到 o6666o 的服務設定，未變更"; exit 1; }
UNIT_NEW="$UNIT_DIR/$SV"
AGAIN=0
if sudo grep -q "^OKX_${NEW}_" "$C/accounts.env" || sudo grep -q "^BOT_${NEW}_" "$C/bots.env" || [ -e "$UNIT_NEW" ]; then
  AGAIN=1; echo "（o1111o 設定過了：這次把 o1111o 的四個值換成新輸入的；o2222o～o6666o 不動）"
fi
BK="$B/_old/setup-o1111o-$(date +%Y%m%d-%H%M%S)"; sudo mkdir -p "$BK"
sudo cp -p "$C/accounts.env" "$C/bots.env" "$BK/"
sudo test -e "$C/acct_$NEW.env" && sudo cp -p "$C/acct_$NEW.env" "$BK/"
[ -e "$UNIT_NEW" ] && sudo cp -p "$UNIT_NEW" "$BK/"
HAD_NEXT=0; [ -e "$B/run_bot_next.py" ] && HAD_NEXT=1
echo "① 設定檔已備份到 $BK"
undo() {
  echo "❌ $1 → 還原成這次設定之前的樣子（o2222o～o6666o 沒有動到）"
  sudo cp -p "$BK/accounts.env" "$C/accounts.env"; sudo cp -p "$BK/bots.env" "$C/bots.env"
  if [ -e "$BK/acct_$NEW.env" ]; then sudo cp -p "$BK/acct_$NEW.env" "$C/"; else sudo rm -f "$C/acct_$NEW.env"; fi
  if [ -e "$BK/$SV" ]; then sudo cp -p "$BK/$SV" "$UNIT_NEW"; else sudo systemctl disable --now "$SV" >/dev/null 2>&1; sudo rm -f "$UNIT_NEW"; fi
  [ "$HAD_NEXT" = 1 ] || sudo rm -f "$B/run_bot_next.py"
  sudo systemctl daemon-reload
  [ "$AGAIN" = 1 ] && sudo systemctl restart "$SV"
  echo "   檢查好再把同一段整段貼一次（四個值重新貼）"
  exit 1
}
ask() {
  local v
  while :; do
    read -rsp "   $1: " -u 3 v; echo >&2
    v="$(printf '%s' "$v" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -n "$v" ] && break
    echo "   不可以空白，請再輸入一次" >&2
  done
  printf '%s' "$v"
}
exec 3<"${TTY_IN:-/dev/tty}"
echo "② 請輸入 o1111o 的四個值（貼上後按 Enter；畫面不會顯示）"
K=$(ask "OKX API Key")
S=$(ask "OKX Secret Key")
P=$(ask "OKX Passphrase")
T=$(ask "TG bot token（o1111o 的 bot）")
[[ "$T" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] || { echo "❌ TG bot token 格式不對（應該像 1234567890:AAH…），未變更"; exit 1; }
for v in "$K" "$S"; do [[ "$v" =~ [[:space:]] ]] && { echo "❌ API Key／Secret 裡面有空白，請重新複製，未變更"; exit 1; }; done
addln() {   # $1＝檔案 $2＝o1111o 舊的那幾行的開頭（先拿掉）；新的幾行從管線進來，接在最後面
  sudo grep -q "^$2" "$1" && sudo sed -i "/^$2/d" "$1"
  [ -n "$(sudo tail -c1 "$1")" ] && echo | sudo tee -a "$1" >/dev/null
  sudo tee -a "$1" >/dev/null
}
printf 'OKX_%s_API_KEY=%s\nOKX_%s_SECRET=%s\nOKX_%s_PASSPHRASE=%s\n' "$NEW" "$K" "$NEW" "$S" "$NEW" "$P" | addln "$C/accounts.env" "OKX_${NEW}_"
printf 'BOT_%s_NORMAL=%s\n' "$NEW" "$T" | addln "$C/bots.env" "BOT_${NEW}_"
unset K S P T
n1=$(sudo grep -c "^OKX_${NEW}_" "$C/accounts.env"); n2=$(sudo grep -c "^BOT_${NEW}_NORMAL=" "$C/bots.env")
[ "$n1" = 3 ] && [ "$n2" = 1 ] || undo "寫入設定檔失敗"
echo "   已寫入 accounts.env（3 行）、bots.env（1 行）"
echo "③ 查 OKX（只查、不下單）"
okx_check; rc=$?
[ "$rc" = 2 ] && undo "OKX 的值不對"
[ "$rc" = 0 ] || [ "$rc" = 3 ] || undo "查 OKX 的程式沒有正常跑完"
echo "④ 照 o6666o 建立 o1111o 的帳戶設定、服務（o1111o 跑測試版 run_bot_next.py）"
sudo sed "s/$SRC/$NEW/g" "$C/acct_$SRC.env" | sudo tee "$C/acct_$NEW.env" >/dev/null
sudo chown --reference="$C/acct_$SRC.env" "$C/acct_$NEW.env"; sudo chmod --reference="$C/acct_$SRC.env" "$C/acct_$NEW.env"
sudo grep -q "^ACCT=$NEW" "$C/acct_$NEW.env" || undo "acct_$NEW.env 裡沒有 ACCT=$NEW"
sed -e "s/$SRC/$NEW/g" -e 's/run_bot\.py/run_bot_next.py/g' "$UNIT_SRC" | sudo tee "$UNIT_NEW" >/dev/null
grep -q "run_bot_next.py" "$UNIT_NEW" && grep -q "$NEW" "$UNIT_NEW" || undo "o1111o 服務設定不完整"
[ -e "$B/run_bot_next.py" ] || cp -p "$B/run_bot.py" "$B/run_bot_next.py" || undo "建立測試版 run_bot_next.py 失敗"
grep -E '^(Description|ExecStart|EnvironmentFile)=' "$UNIT_NEW" | sed 's/^/   /'
echo "⑤ 啟動 o1111o"
sudo systemctl daemon-reload
sudo systemctl enable "$SV" >/dev/null 2>&1
T0=$(date '+%Y-%m-%d %H:%M:%S')
sudo systemctl restart "$SV"
sleep "${WAIT_SEC:-15}"
st=$(systemctl is-active "$SV" 2>/dev/null)
v=$(sudo journalctl -u "$SV" --since "$T0" --no-pager 2>/dev/null | grep -o '移動任務已啟動（v[0-9.]*）' | tail -1)
echo "   o1111o  $st  ${v:-（沒有印出版本）}"
if [ "$st" = "active" ] && [ -n "$v" ]; then
  echo "✅ o1111o 設定完成（TG token 正確才會印出版本）"
  echo "   到 TG 的 o1111o bot 打 /coins（看到主帳戶餘額）、/check"
  [ "$rc" = 3 ] && echo "   ⚠️ 上面 ③ 查 OKX 沒有過：照 ③ 的說明到 OKX 改好，再打 /coins 看餘額"
  exit 0
fi
echo "   最近的錯誤（token 已遮掉）："
sudo journalctl -u "$SV" --since "$T0" --no-pager 2>/dev/null \
  | grep -iE "error|fail|traceback|invalid|unauthorized|keyerror|exception" | tail -15 \
  | sed -E 's/[0-9]{6,}:[A-Za-z0-9_-]{20,}/<token>/g; s/^/   /'
undo "o1111o 沒有正常啟動（最常見：TG bot token 貼錯）"
