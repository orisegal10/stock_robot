import asyncio
from loguru import logger
from ib_insync import IB

from src.config import config

_ib = IB()


def get_ib() -> IB:
    return _ib


def connect() -> bool:
    host = config.get("ibkr", "host", default="127.0.0.1")
    port = config.get("ibkr", "port", default=4002)
    client_id = config.get("ibkr", "client_id", default=1)

    try:
        _ib.connect(host, port, clientId=client_id, timeout=60, readonly=False)
        logger.info("Connected to IB Gateway at {}:{} (clientId={})", host, port, client_id)
        # Register error handler for competing session warnings
        _ib.errorEvent += _on_error
        _ib.disconnectedEvent += _on_disconnect
        return True
    except Exception as exc:
        logger.error("Failed to connect to IB Gateway: {}", exc)
        return False


def _on_error(reqId, errorCode, errorString, contract) -> None:
    if errorCode == 10197:
        logger.warning("⚠️  Competing IBKR session detected (error 10197) — "
                       "market data paused. Log out of IBKR on other devices to resume.")
    elif errorCode == 10089:
        pass  # expected: delayed data available, not critical
    elif errorCode in (2104, 2106, 2158):
        pass  # market data farm connection notices, not critical
    else:
        logger.debug("IB error {}: {}", errorCode, errorString)


def disconnect() -> None:
    if _ib.isConnected():
        _ib.disconnect()
        logger.info("Disconnected from IB Gateway")


def _on_disconnect() -> None:
    import sys
    if sys.meta_path is None:
        return  # Python is shutting down, skip reconnect
    logger.warning("Lost connection to IB Gateway — will reconnect via main loop")


def is_connected() -> bool:
    return _ib.isConnected()
