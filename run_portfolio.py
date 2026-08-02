from engine.config import load_config
from engine.portfolio_runner import main

if __name__ == "__main__":
    main(target_weights={"SPY": 1 / 3, "TLT": 1 / 3, "GLD": 1 / 3}, config=load_config())
