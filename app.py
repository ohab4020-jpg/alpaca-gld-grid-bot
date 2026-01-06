import os
import math
import sqlite3
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# =========================
# LOGGING (RENDER SAFE)
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)

# =========================
# CONFIG (EDIT ONLY THIS)
# =========================
SYMBOL = "GLD"
LOWER_BAND = 380
UPPER_BAND = 430
GRID_PERCENT = 0.006
ORDER_USD = 500
MAX_CAPITAL = 10000

PAPER_TRADING = True
DB_FILE = f"gridbot_{SYMBOL}.db"

# =========================
# SECURITY / CONTROL
# =========================
RUN_TOKEN = os.getenv("RUN_TOKEN")  # required to hit /run
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "true").lower() == "true"

# =========================
# ALPACA CLIENTS
# =========================
ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")

if not ALPACA_KEY or not ALPACA_SECRET:
    raise RuntimeError("Missing ALPACA_KEY or ALPACA_SECRET")

trading = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=PAPER_TRADING)
data_client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)

app = Flask(__name__)

# =========================
# HEALTH CHECK (RENDER)
# =========================
@app.route("/healthz", methods=["GET"])
def healthz():
    return "ok", 200

# =========================
# DATABASE
# =========================
def db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS lots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        buy_order_id TEXT,
        buy_status TEXT,
        buy_limit_price REAL,
        buy_filled_price REAL,
        qty INTEGER,
        buy_created_at TEXT,
        sell_order_id TEXT,
        sell_status TEXT,
        sell_limit_price REAL,
        sell_filled_price REAL,
        sell_created_at TEXT
    )
    """)
    conn.commit()
    conn.close()
    log.info(f"{SYMBOL} | Database initialized")

def now():
    return datetime.now(timezone.utc).isoformat()

def round_price(p):
    return round(p, 2)

# =========================
# PRICE
# =========================
def get_price():
    req = StockLatestTradeRequest(symbol_or_symbols=SYMBOL)
    trade = data_client.get_stock_latest_trade(req)[SYMBOL]
    price = float(trade.price)
    log.info(f"{SYMBOL} | Price check: {price}")
    return price

# =========================
# ORDER SYNC
# =========================
def sync_orders():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM lots WHERE buy_status='BUY_SUBMITTED'")
    for r in cur.fetchall():
        try:
            o = trading.get_order_by_id(r["buy_order_id"])
            if o.status == "filled":
                cur.execute(
                    "UPDATE lots SET buy_status='BOUGHT', buy_filled_price=? WHERE id=?",
                    (float(o.filled_avg_price), r["id"])
                )
                log.info(f"{SYMBOL} | BUY filled @ {o.filled_avg_price}")
        except:
            pass

    cur.execute("SELECT * FROM lots WHERE sell_status='SELL_SUBMITTED'")
    for r in cur.fetchall():
        try:
            o = trading.get_order_by_id(r["sell_order_id"])
            if o.status == "filled":
                cur.execute(
                    "UPDATE lots SET sell_status='SOLD', sell_filled_price=? WHERE id=?",
                    (float(o.filled_avg_price), r["id"])
                )
                log.info(f"{SYMBOL} | SELL filled @ {o.filled_avg_price}")
        except:
            pass

    conn.commit()
    conn.close()

def deployed_capital():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(SUM(buy_filled_price * qty), 0)
        FROM lots
        WHERE buy_status='BOUGHT' AND sell_status IS NULL
    """)
    used = cur.fetchone()[0]

    cur.execute("""
        SELECT COALESCE(SUM(buy_limit_price * qty), 0)
        FROM lots
        WHERE buy_status='BUY_SUBMITTED'
    """)
    reserved = cur.fetchone()[0]

    conn.close()
    return used + reserved

def open_buy_exists(price):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM lots
        WHERE buy_status='BUY_SUBMITTED'
        AND ABS(buy_limit_price - ?) < 0.0001
    """, (price,))
    exists = cur.fetchone()[0] > 0
    conn.close()
    return exists

# =========================
# MAIN CYCLE
# =========================
def run_cycle():
    clock = trading.get_clock()
    if not clock.is_open:
        log.info(f"{SYMBOL} | Market closed — monitoring only")
        return

    price = get_price()
    sync_orders()

    conn = db()
    cur = conn.cursor()

    # SELL LOGIC
    cur.execute("""
        SELECT * FROM lots
        WHERE buy_status='BOUGHT'
        AND (sell_status IS NULL OR sell_status!='SOLD')
    """)
    for r in cur.fetchall():
        target = round_price(r["buy_filled_price"] * (1 + GRID_PERCENT))
        if price >= target:
            if not TRADING_ENABLED:
                log.info(f"{SYMBOL} | SELL blocked — trading disabled")
                continue
            try:
                o = trading.submit_order(
                    LimitOrderRequest(
                        symbol=SYMBOL,
                        qty=r["qty"],
                        side=OrderSide.SELL,
                        limit_price=target,
                        time_in_force=TimeInForce.DAY
                    )
                )
                cur.execute("""
                    UPDATE lots SET
                        sell_status='SELL_SUBMITTED',
                        sell_order_id=?,
                        sell_limit_price=?,
                        sell_created_at=?
                    WHERE id=?
                """, (o.id, target, now(), r["id"]))
                log.info(f"{SYMBOL} | SELL placed @ {target}")
            except:
                pass

    capital = deployed_capital()

    # BUY LOGIC
    if not (LOWER_BAND <= price <= UPPER_BAND):
        log.info(f"{SYMBOL} | No trade — price outside band [{LOWER_BAND}, {UPPER_BAND}]")
        conn.close()
        return

    if capital >= MAX_CAPITAL:
        log.info(f"{SYMBOL} | BUY skipped — max capital reached (${MAX_CAPITAL})")
        conn.close()
        return

    cur.execute("""
        SELECT buy_filled_price FROM lots
        WHERE buy_status='BOUGHT'
        ORDER BY id DESC LIMIT 1
    """)
    row = cur.fetchone()
    anchor = row[0] if row else price
    buy_price = round_price(anchor * (1 - GRID_PERCENT))

    if price > buy_price:
        log.info(f"{SYMBOL} | Waiting for buy ≤ {buy_price}")
        conn.close()
        return

    if open_buy_exists(buy_price):
        log.info(f"{SYMBOL} | BUY skipped — duplicate grid level @ {buy_price}")
        conn.close()
        return

    qty = int(ORDER_USD // buy_price)
    if qty <= 0:
        conn.close()
        return

    if not TRADING_ENABLED:
        log.info(f"{SYMBOL} | BUY blocked — trading disabled")
        conn.close()
        return

    try:
        o = trading.submit_order(
            LimitOrderRequest(
                symbol=SYMBOL,
                qty=qty,
                side=OrderSide.BUY,
                limit_price=buy_price,
                time_in_force=TimeInForce.DAY
            )
        )
        cur.execute("""
            INSERT INTO lots (
                symbol, buy_order_id, buy_status,
                buy_limit_price, qty, buy_created_at
            ) VALUES (?, ?, 'BUY_SUBMITTED', ?, ?, ?)
        """, (SYMBOL, o.id, buy_price, qty, now()))
        log.info(f"{SYMBOL} | BUY placed: {qty} @ {buy_price}")
    except:
        pass

    conn.commit()
    conn.close()

# =========================
# ROUTES
# =========================
@app.route("/run")
def run():
    token = request.headers.get("X-RUN-TOKEN")
    if RUN_TOKEN and token != RUN_TOKEN:
        log.warning(f"{SYMBOL} | Unauthorized /run attempt blocked")
        return jsonify({"error": "unauthorized"}), 403

    run_cycle()
    return jsonify({"status": "ok", "symbol": SYMBOL})

# =========================
# START
# =========================
if __name__ == "__main__":
    init_db()
    log.info(f"{SYMBOL} | Bot started")
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
