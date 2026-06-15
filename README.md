# stock_robot

A Python bot for tracking and analysing stock prices.

## Features

- Fetch real-time stock quotes
- Calculate moving averages
- Send price alerts

## Setup

```bash
pip install -r requirements.txt
python main.py
```

## Usage

```python
from stock_robot import StockRobot

robot = StockRobot(ticker="AAPL")
robot.run()
```
