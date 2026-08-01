from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient

from engine.config import Config


def make_trading_client(config: Config) -> TradingClient:
    return TradingClient(config.alpaca_api_key, config.alpaca_secret_key, paper=config.alpaca_paper)


def make_data_client(config: Config) -> StockHistoricalDataClient:
    return StockHistoricalDataClient(config.alpaca_api_key, config.alpaca_secret_key)
