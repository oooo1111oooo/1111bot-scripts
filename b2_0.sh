#!/usr/bin/env bash
set -e
ROOT=/srv/1111bot
ENVFILE=$ROOT/config/accounts.env
s(){ echo; echo "########## $1 ##########"; }

s "OKX 金鑰輸入（只寫入本機，不經網路）"
echo "將寫入：$ENVFILE"
echo "此檔已被 .gitignore 排除，不會上傳 GitHub"
echo "每個帳戶需要 3 個值：api_key / secret / passphrase"
echo "直接貼上、按 Enter。輸入時不顯示內容（保護隱私）"
echo ""

> $ENVFILE
chmod 600 $ENVFILE

for acct in o2222o o3333o o4444o o5555o; do
  echo "===== 帳戶 $acct ====="
  read -rp "  $acct api_key: " k
  read -rsp "  $acct secret: " sc; echo
  read -rsp "  $acct passphrase: " pp; echo
  {
    echo "OKX_${acct}_API_KEY=$k"
    echo "OKX_${acct}_SECRET=$sc"
    echo "OKX_${acct}_PASSPHRASE=$pp"
  } >> $ENVFILE
  echo "  ✓ $acct 已寫入"
  echo ""
done

s "驗收：確認格式（只顯示 KEY 名稱，不顯示值）"
grep -oE '^OKX_[a-z0-9]+_(API_KEY|SECRET|PASSPHRASE)' $ENVFILE
echo ""
echo "共寫入 $(grep -c '=' $ENVFILE) 個值（應為 12）"

s "驗收：確認不進版控"
cd $ROOT
if git status -s | grep -q 'accounts.env'; then echo "DANGER: 金鑰檔被追蹤！"; else echo "OK: accounts.env 不進版控"; fi

s "檔案權限（應為 600 = 只有你能讀）"
ls -l $ENVFILE

s END
