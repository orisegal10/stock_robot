"""
Live paper trading test — places a real BUY order for 1 share via IB Gateway.
Run on the VPS: python test_buy.py
"""
import sys
import time
from loguru import logger
from ib_insync import IB, Stock, MarketOrder, StopOrder

HOST = "ibgateway"
PORT = 4004
CLIENT_ID = 99   # different client_id to avoid conflict with the running bot
SYMBOL = "APLD"
SHARES = 1

logger.remove()
logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")

ib = IB()

logger.info("Connecting to IB Gateway at {}:{}", HOST, PORT)
try:
    ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=30, readonly=False)
except Exception as e:
    logger.error("Connection failed: {}", e)
    sys.exit(1)

logger.info("Connected. Portfolio value:")
for item in ib.accountSummary():
    if item.tag == "NetLiquidation":
        logger.info("  NetLiquidation: ${}", item.value)
        break

# Qualify contract
contract = Stock(SYMBOL, "SMART", "USD")
ib.qualifyContracts(contract)
logger.info("Contract qualified: {} (conId={})", contract.symbol, contract.conId)

# Get current price
ticker = ib.reqMktData(contract, "", False, False)
ib.sleep(2)
price = ticker.last or ticker.close or ticker.bid
logger.info("Current price: {}", price)

if not price:
    logger.error("Could not get price for {} — aborting", SYMBOL)
    ib.disconnect()
    sys.exit(1)

stop_price = round(price * 0.98, 2)   # 2% stop loss
logger.info("Placing BUY {} x {} @ market | Stop: ${}", SHARES, SYMBOL, stop_price)

# Place market buy order
buy_order = MarketOrder("BUY", SHARES)
trade = ib.placeOrder(contract, buy_order)
ib.sleep(2)

logger.info("Buy order status: {} | filled: {} @ ${}",
            trade.orderStatus.status,
            trade.orderStatus.filled,
            trade.orderStatus.avgFillPrice)

# Place stop loss
stop_order = StopOrder("SELL", SHARES, stop_price)
stop_trade = ib.placeOrder(contract, stop_order)
ib.sleep(1)
logger.info("Stop loss order status: {}", stop_trade.orderStatus.status)

logger.info("Test complete. Check your IBKR paper account to verify the orders.")
logger.info("To cancel the orders, go to IBKR paper account > Orders > Cancel All")

ib.disconnect()
