from engine.config import load_config
from engine.multi_runner import main

if __name__ == "__main__":
    # Which accounts trade and with which strategy is now a .env/dashboard concern (see
    # ACCOUNT_IDS, ACCOUNT_<id>_* in .env.example) rather than a CLI flag - main() loops
    # over every active account itself.
    main(config=load_config())
