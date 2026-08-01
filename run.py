from engine.runner import main
from engine.strategy import MovingAverageCrossoverStrategy

if __name__ == "__main__":
    main(symbols=["SPY"], strategy=MovingAverageCrossoverStrategy(short_window=20, long_window=50))
