import os
import threading
import logging
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.live import StockDataStream

# =======================
# CONFIG
# =======================

BOTS = {
    "GLD": {
        "lower": 365.76,
        "upper": 436.84,
        "grid_pct": 0.005,      # 0.5%
        "order_usd": 1000,
        "max_capital": 35000
    },
    "SLV": {
        "lower": 63.00,
        "upper": 77.48,
        "grid_pct": 0.005,      # 0.5%
        "order_usd": 1500,
        "max_capital": 60000
    }
}

RUN_TOKEN = os.getenv("RUN_TOKEN")
ALPACA_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY")
PAPER = True

# =======================
# LOGGING
# =======================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =======================
# INIT
# =======================

app = Flask(__name__)
trading = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=PAPER)

run_lock = threading.Lock()

# =======================
# HELPERS
# =======================

def get_open_orders(symbol):
    return [o for o in trading.get_orders() if o.symbol == symbol and o.status == "accepted"]

def get_position_qty(symbol):
    try:
        pos = trading.get_open_position(symbol)
        return float(pos.qty)
    except:
        return 0.0

def grid_prices(lower, upper, pct):
    prices = []
    p = lower
    while p <= upper:
        prices.append(round(p, 2))
        p *= (1 + pct)
    return prices

# =======================
# CORE LOGIC
# =======================

def run_bot(symbol, cfg):
    price = trading.get_latest_trade(symbol).price
    logging.info(f"📈 {symbol} PRICE = {price}")

    grids = grid_prices(cfg["lower"], cfg["upper"], cfg["grid_pct"])
    open_orders = get_open_orders(symbol)
    open_prices = {float(o.limit_price) for o in open_orders}

    owned_qty = get_position_qty(symbol)

    for gp in grids:
        qty = round(cfg["order_usd"] / gp, 4)

        # BUY LOGIC
        if gp < price and gp not in open_prices:
            logging.info(f"🟢 BUY | {symbol} | {qty} @ {gp}")
            trading.submit_order(
                LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    limit_price=gp,
                    time_in_force=TimeInForce.GTC
                )
            )
            break  # one order per run

        # SELL LOGIC (LONG ONLY)
        if gp > price and owned_qty >= qty and gp not in open_prices:
            logging.info(f"🔴 SELL | {symbol} | {qty} @ {gp}")
            trading.submit_order(
                LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    limit_price=gp,
                    time_in_force=TimeInForce.GTC
                )
            )
            break

# =======================
# ROUTES
# =======================

@app.route("/run", methods=["GET"])
def run():
    if request.headers.get("X-RUN-TOKEN") != RUN_TOKEN:
        return jsonify({"error": "Unauthorized /run attempt blocked"}), 401

    if not run_lock.acquire(blocking=False):
        return jsonify({"status": "already running"}), 200

    try:
        for symbol, cfg in BOTS.items():
            run_bot(symbol, cfg)
        return jsonify({"bots": list(BOTS.keys()), "status": "ok"})
    finally:
        run_lock.release()

@app.route("/healthz")
def health():
    return "ok", 200

# =======================
# START
# =======================

if __name__ == "__main__":
    logging.info("🦁 Leo started (Paper Trading)")
    app.run(host="0.0.0.0", port=10000)
