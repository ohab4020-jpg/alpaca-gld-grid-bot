import os
import math
import sqlite3
import logging
import requests
from datetime import datetime, timezone, date

from flask import Flask, jsonify, request

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("Leo")

# ============================================================
# TELEGRAM
# ============================================================
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

def tg(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg}
        )
    except Exception:
        pass

# ============================================================
# GLOBAL SETTINGS
# ============================================================
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "true").lower() == "true"

ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")

if not ALPACA_KEY or not ALPACA_SECRET:
    raise RuntimeError("Missing Alpaca API keys")

trading = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=PAPER_TRADING)
data_client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)

# ============================================================
# BOT CONFIG (FINAL — CONFIRMED)
# ============================================================
BOTS = {
    "GLD": {
        "lower": 365.76,
        "upper": 436.84,
        "grid_pct": 0.005,
        "order_usd": 1000,
        "max_capital": 35000
    },
    "SLV": {
        "lower": 63.00,
        "upper": 77.48,
        "grid_pct": 0.005,
        "order_usd": 1500,
        "max_capital": 60000
    }
}

# ============================================================
# APP
# ============================================================
app = Flask(__name__)

@app.route("/healthz")
def healthz():
    return "ok", 200

# ============================================================
# DATABASE
# ============================================================
def db(symbol):
    conn = sqlite3.connect(f"leo_{symbol}.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(symbol):
    conn = db(symbol)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buy_price REAL,
            sell_price REAL,
            qty INTEGER,
            buy_time TEXT,
            sell_time TEXT
        )
    """)
    conn.commit()
    conn.close()

# ============================================================
# PRICE
# ============================================================
def get_price(symbol):
    trade = data_client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=symbol)
    )[symbol]
    price = float(trade.price)
    log.info(f"📈 {symbol} PRICE = {price}")
    return price

# ============================================================
# CAPITAL
# ============================================================
def used_capital(symbol):
    conn = db(symbol)
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(SUM(buy_price * qty),0)
        FROM lots
        WHERE sell_price IS NULL
    """)
    val = cur.fetchone()[0]
    conn.close()
    return val

# ============================================================
# DAILY P&L
# ============================================================
def daily_pnl(symbol):
    today = date.today().isoformat()
    conn = db(symbol)
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(SUM((sell_price - buy_price) * qty),0)
        FROM lots
        WHERE sell_time LIKE ?
    """, (f"{today}%",))
    pnl = cur.fetchone()[0]
    conn.close()
    return round(pnl, 2)

# ============================================================
# CORE LOGIC
# ============================================================
def run_bot(symbol, cfg):
    price = get_price(symbol)
    cap_used = used_capital(symbol)

    conn = db(symbol)
    cur = conn.cursor()

    # ---------------- SELL ----------------
    cur.execute("SELECT * FROM lots WHERE sell_price IS NULL")
    for r in cur.fetchall():
        target = round(r["buy_price"] * (1 + cfg["grid_pct"]), 2)
        if price >= target:
            trading.submit_order(
                LimitOrderRequest(
                    symbol=symbol,
                    qty=r["qty"],
                    side=OrderSide.SELL,
                    limit_price=target,
                    time_in_force=TimeInForce.DAY
                )
            )
            cur.execute(
                "UPDATE lots SET sell_price=?, sell_time=? WHERE id=?",
                (target, datetime.now(timezone.utc).isoformat(), r["id"])
            )
            msg = f"🔴 SELL | {symbol}\nQty: {r['qty']} @ {target}"
            log.info(msg)
            tg(msg)

    # ---------------- BUY ----------------
    if not TRADING_ENABLED:
        conn.commit()
        conn.close()
        return

    if cfg["lower"] <= price <= cfg["upper"]:
        if cap_used + cfg["order_usd"] <= cfg["max_capital"]:
            buy_price = round(price * (1 - cfg["grid_pct"]), 2)
            qty = int(cfg["order_usd"] // buy_price)
            if qty > 0:
                trading.submit_order(
                    LimitOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=OrderSide.BUY,
                        limit_price=buy_price,
                        time_in_force=TimeInForce.DAY
                    )
                )
                cur.execute(
                    "INSERT INTO lots VALUES (NULL,?,?,?, ?,NULL)",
                    (buy_price, None, qty, datetime.now(timezone.utc).isoformat())
                )
                msg = f"🟢 BUY | {symbol}\nQty: {qty} @ {buy_price}"
                log.info(msg)
                tg(msg)

    conn.commit()
    conn.close()

    pnl = daily_pnl(symbol)
    log.info(f"📊 DAILY P&L | {symbol} = ${pnl}")
    tg(f"📊 DAILY P&L | {symbol}\n${pnl}")

# ============================================================
# ROUTE
# ============================================================
@app.route("/run")
def run():
    for symbol, cfg in BOTS.items():
        init_db(symbol)
        run_bot(symbol, cfg)
    return jsonify({"status": "ok", "bots": list(BOTS.keys())})

# ============================================================
# START
# ============================================================
if __name__ == "__main__":
    for s in BOTS:
        init_db(s)
    log.info("🦁 Leo started (Paper Trading)")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
