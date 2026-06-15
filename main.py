def get_price(ticker: str) -> float:
    """Return the latest price for a stock ticker."""
    # TODO: connect to a real data source
    prices = {"AAPL": 189.5, "TSLA": 245.0, "MSFT": 415.2}
    return prices.get(ticker.upper(), 0.0)


def moving_average(prices: list[float], window: int) -> float:
    """Return the simple moving average over the given window."""
    if len(prices) < window:
        return 0.0
    return sum(prices[-window:]) / window


if __name__ == "__main__":
    ticker = "AAPL"
    price = get_price(ticker)
    print(f"{ticker}: ${price}")
