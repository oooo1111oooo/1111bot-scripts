#!/usr/bin/env bash
set -e
ROOT=/srv/1111bot
DB=$ROOT/data/1111bot.sqlite3
s(){ echo; echo "########## $1 ##########"; }

s "0 清除先前殘留"
rm -f $DB $DB-wal $DB-shm $ROOT/scripts/schema.sql

s "1 建立 schema.sql"
cat > $ROOT/scripts/schema.sql <<'SQL'
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS strategies (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  account       TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK(kind IN ('normal','ha')),
  symbol        TEXT NOT NULL,
  direction     TEXT NOT NULL CHECK(direction IN ('L','S')),
  timeframe     TEXT NOT NULL,
  leverage      TEXT NOT NULL,
  margin        TEXT NOT NULL,
  params_json   TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'idle',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE(account, kind, symbol, direction)
);

CREATE TABLE IF NOT EXISTS orders (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id      INTEGER REFERENCES strategies(id),
  client_order_id  TEXT NOT NULL UNIQUE,
  okx_ord_id       TEXT,
  account          TEXT NOT NULL,
  symbol           TEXT NOT NULL,
  side             TEXT NOT NULL,
  pos_side         TEXT NOT NULL,
  ord_type         TEXT NOT NULL,
  price            TEXT,
  size             TEXT NOT NULL,
  state            TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders(strategy_id);
CREATE INDEX IF NOT EXISTS idx_orders_okx ON orders(okx_ord_id);

CREATE TABLE IF NOT EXISTS fills (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id      INTEGER REFERENCES orders(id),
  okx_fill_id   TEXT UNIQUE,
  account       TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  side          TEXT NOT NULL,
  pos_side      TEXT NOT NULL,
  fill_price    TEXT NOT NULL,
  fill_size     TEXT NOT NULL,
  fee           TEXT NOT NULL,
  filled_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id    INTEGER REFERENCES strategies(id),
  account        TEXT NOT NULL,
  kind           TEXT NOT NULL,
  symbol         TEXT NOT NULL,
  direction      TEXT NOT NULL,
  timeframe      TEXT,
  params_json    TEXT,
  entry_at       TEXT,
  exit_at        TEXT,
  entry_price    TEXT,
  exit_price     TEXT,
  exit_reason    TEXT,
  ambush_secs    INTEGER,
  hold_secs      INTEGER,
  gross_pnl      TEXT,
  pnl_rate       TEXT,
  fee_total      TEXT,
  net_pnl        TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_day ON trades(account, entry_at);

CREATE TABLE IF NOT EXISTS signals (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id   INTEGER REFERENCES strategies(id),
  account       TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  phase         TEXT NOT NULL,
  candles_json  TEXT NOT NULL,
  amp_required  TEXT,
  amp_actual    TEXT,
  result        TEXT NOT NULL,
  judged_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recon_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_at       TEXT NOT NULL,
  account       TEXT NOT NULL,
  symbol        TEXT,
  discrepancy   TEXT NOT NULL,
  action_taken  TEXT NOT NULL,
  detail_json   TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  level         TEXT NOT NULL,
  source        TEXT NOT NULL,
  message       TEXT NOT NULL,
  detail_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
SQL
echo "schema.sql 行數: $(wc -l < $ROOT/scripts/schema.sql)"

s "2 建立資料庫"
$ROOT/.venv/bin/python - <<PY
import sqlite3
db = sqlite3.connect("$DB")
with open("$ROOT/scripts/schema.sql") as f:
    db.executescript(f.read())
db.close()
print("資料庫建立於 $DB")
PY

s "3 列出所有表"
$ROOT/.venv/bin/python - <<PY
import sqlite3
db = sqlite3.connect("$DB")
tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("表數量:", len(tables))
for t in tables:
    cols = db.execute("PRAGMA table_info("+t+")").fetchall()
    print(" ", t, ":", len(cols), "欄")
db.close()
PY

s "4 精度存取測試"
$ROOT/.venv/bin/python - <<PY
import sqlite3
from decimal import Decimal
db = sqlite3.connect("$DB")
val = str(Decimal('63086.8') * Decimal('0.005'))
db.execute("INSERT INTO events(ts,level,source,message,detail_json) VALUES(?,?,?,?,?)",
           ("2026-08-22T00:00:00+08:00","INFO","test","precision", val))
db.commit()
got = db.execute("SELECT detail_json FROM events WHERE source='test'").fetchone()[0]
print("存入:", val, "| 讀出:", got, "| 一致:", val == got)
db.execute("DELETE FROM events WHERE source='test'")
db.commit()
db.close()
PY

s "5 確認 DB 不進版控"
cd $ROOT
if git status -s | grep -qE '\.sqlite3|data/'; then echo "DANGER: DB tracked"; else echo "OK: DB excluded from git"; fi

s "6 commit schema.sql"
cd $ROOT
git add scripts/schema.sql
git commit -q -m "B1-4: DB schema"
git log --oneline

s END
