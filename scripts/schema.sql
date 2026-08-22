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
