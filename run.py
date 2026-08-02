from engine.config import load_config
from engine.runner import main
from engine.strategy import MovingAverageCrossoverStrategy

if __name__ == "__main__":
    # symbols default to whatever run_research.py most recently selected - run that first
    # if the watchlist is empty. Pass symbols=[...] here to trade a fixed list instead.
    main(strategy=MovingAverageCrossoverStrategy(short_window=20, long_window=50), config=load_config())
