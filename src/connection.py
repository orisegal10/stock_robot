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
        _ib.disconnectedEvent += _on_disconnect
        return True
    except Exception as exc:
        logger.error("Failed to connect to IB Gateway: {}", exc)
        return False


def disconnect() -> None:
    if _ib.isConnected():
        _ib.disconnect()
        logger.info("Disconnected from IB Gateway")


def _on_disconnect() -> None:
    logger.warning("Lost connection to IB Gateway — attempting reconnect in 10s")
    import time
    time.sleep(10)
    try:
        connect()
        logger.info("Reconnected to IB Gateway")
    except Exception as exc:
        logger.error("Reconnect failed: {}", exc)


def is_connected() -> bool:
    return _ib.isConnected()
