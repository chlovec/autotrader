import argparse
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    """Global settings - not tied to any one account. Broker selection and credentials
    moved onto individual accounts (see AccountCredentials/load_account_credentials below)
    once trading multiple accounts became possible; a single global "the broker" no longer
    means anything.

    max_position_size_usd/max_daily_loss_usd/max_total_exposure_usd here are seed defaults
    only, used once when an id from ACCOUNT_IDS is first seen (engine/accounts.py's
    sync_accounts_from_env) - after that the per-account values on db.models.Account are
    what RiskManager/the rebalancer actually enforce, and are owned by the dashboard from
    then on.
    """

    max_position_size_usd: float
    max_daily_loss_usd: float
    max_total_exposure_usd: float
    questrade_poll_interval_seconds: float
    # Research (engine/research.py's fetch_universe_news) always uses Alpaca's News API,
    # independent of which broker(s) any account actually trades through - it's a global
    # screen, not scoped to one account. A free Alpaca paper key pair works fine here even
    # if no account trades through Alpaca at all.
    alpaca_news_api_key: str
    alpaca_news_secret_key: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    alert_email_from: str
    alert_email_to: str


@dataclass(frozen=True)
class AccountCredentials:
    """Everything a specific account's BrokerClient needs to connect - broker selection
    plus that broker's auth fields, read from ACCOUNT_<id>_* env vars (see
    load_account_credentials). Same three broker-specific shapes as before multi-account
    support (Alpaca api-key/secret, IBKR's socket connection, Questrade's OAuth refresh
    token) - they didn't get more similar just because there can now be several of them.
    """

    broker: str
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = ""
    alpaca_paper: bool = True
    ibkr_host: str = ""
    ibkr_port: int = 0
    ibkr_client_id: int = 0
    questrade_refresh_token: str = ""
    questrade_poll_interval_seconds: float = 5.0


def _str_to_bool(value: str) -> bool:
    return value.lower() == "true"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Autotrader configuration - any flag not passed falls back to its "
        "environment variable / .env value.",
    )
    parser.add_argument("--max-position-size-usd", type=float, default=None)
    parser.add_argument("--max-daily-loss-usd", type=float, default=None)
    parser.add_argument("--max-total-exposure-usd", type=float, default=None)
    parser.add_argument("--questrade-poll-interval-seconds", type=float, default=None)
    parser.add_argument("--alpaca-news-api-key", default=None)
    parser.add_argument("--alpaca-news-secret-key", default=None)
    parser.add_argument("--smtp-host", default=None)
    parser.add_argument("--smtp-port", type=int, default=None)
    parser.add_argument("--smtp-username", default=None)
    parser.add_argument("--smtp-password", default=None)
    parser.add_argument("--alert-email-from", default=None)
    parser.add_argument("--alert-email-to", default=None)
    return parser


def load_config(argv: list[str] | None = None) -> Config:
    """Resolves the global settings from CLI flags, falling back to the matching
    environment variable/.env value for any flag not passed. `argv=None` (the default)
    reads the process's real sys.argv - only the top-level entrypoint scripts
    (run_engine.py, run_research.py) should rely on that default; every other caller
    (backend, ad-hoc scripts, internal fallbacks) passes argv=[] explicitly so it never
    picks up an unrelated argv (uvicorn's, pytest's, ...) - parse_known_args ignores
    unrecognized tokens either way, but argv=[] makes the intent explicit rather than
    relying on that being harmless."""
    args, _unused = _build_parser().parse_known_args(argv)

    return Config(
        max_position_size_usd=(
            args.max_position_size_usd if args.max_position_size_usd is not None else float(os.environ.get("MAX_POSITION_SIZE_USD", "1000"))
        ),
        max_daily_loss_usd=(
            args.max_daily_loss_usd if args.max_daily_loss_usd is not None else float(os.environ.get("MAX_DAILY_LOSS_USD", "200"))
        ),
        max_total_exposure_usd=(
            args.max_total_exposure_usd
            if args.max_total_exposure_usd is not None
            else float(os.environ.get("MAX_TOTAL_EXPOSURE_USD", "0"))
        ),
        questrade_poll_interval_seconds=(
            args.questrade_poll_interval_seconds
            if args.questrade_poll_interval_seconds is not None
            else float(os.environ.get("QUESTRADE_POLL_INTERVAL_SECONDS", "5"))
        ),
        alpaca_news_api_key=args.alpaca_news_api_key or os.environ.get("ALPACA_NEWS_API_KEY", ""),
        alpaca_news_secret_key=args.alpaca_news_secret_key or os.environ.get("ALPACA_NEWS_SECRET_KEY", ""),
        smtp_host=args.smtp_host or os.environ.get("SMTP_HOST", ""),
        smtp_port=args.smtp_port if args.smtp_port is not None else int(os.environ.get("SMTP_PORT", "587")),
        smtp_username=args.smtp_username or os.environ.get("SMTP_USERNAME", ""),
        smtp_password=args.smtp_password or os.environ.get("SMTP_PASSWORD", ""),
        alert_email_from=args.alert_email_from or os.environ.get("ALERT_EMAIL_FROM", ""),
        alert_email_to=args.alert_email_to or os.environ.get("ALERT_EMAIL_TO", ""),
    )


def load_account_ids() -> list[str]:
    """Parses the comma-separated ACCOUNT_IDS env var - the set of accounts this
    deployment knows about. Each id must have a matching ACCOUNT_<id>_* block (see
    load_account_credentials)."""
    raw = os.environ.get("ACCOUNT_IDS", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def load_account_credentials(account_id: str) -> AccountCredentials:
    """Reads the ACCOUNT_<account_id>_* env vars for one account. Mirrors the old
    top-level BROKER/ALPACA_*/IBKR_*/QUESTRADE_* vars, just namespaced per account so
    multiple accounts (even multiple accounts on the same broker) can coexist in one
    .env file."""
    prefix = f"ACCOUNT_{account_id}_"

    def env(suffix: str, default: str = "") -> str:
        return os.environ.get(prefix + suffix, default)

    broker = env("BROKER")

    alpaca_paper = env("ALPACA_PAPER", "true").lower() == "true"
    if alpaca_paper:
        alpaca_api_key = env("ALPACA_API_KEY")
        alpaca_secret_key = env("ALPACA_SECRET_KEY")
        alpaca_base_url = env("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    else:
        alpaca_api_key = env("ALPACA_LIVE_API_KEY")
        alpaca_secret_key = env("ALPACA_LIVE_SECRET_KEY")
        alpaca_base_url = env("ALPACA_LIVE_BASE_URL", "https://api.alpaca.markets")
        if broker == "alpaca" and (not alpaca_api_key or not alpaca_secret_key):
            raise RuntimeError(
                f"{prefix}ALPACA_PAPER=false but {prefix}ALPACA_LIVE_API_KEY/{prefix}ALPACA_LIVE_SECRET_KEY are not "
                "set - refusing to start rather than silently trading on paper credentials instead."
            )

    return AccountCredentials(
        broker=broker,
        alpaca_api_key=alpaca_api_key,
        alpaca_secret_key=alpaca_secret_key,
        alpaca_base_url=alpaca_base_url,
        alpaca_paper=alpaca_paper,
        ibkr_host=env("IBKR_HOST", "127.0.0.1"),
        ibkr_port=int(env("IBKR_PORT", "7497")),
        ibkr_client_id=int(env("IBKR_CLIENT_ID", "1")),
        questrade_refresh_token=env("QUESTRADE_REFRESH_TOKEN"),
        questrade_poll_interval_seconds=float(env("QUESTRADE_POLL_INTERVAL_SECONDS", os.environ.get("QUESTRADE_POLL_INTERVAL_SECONDS", "5"))),
    )