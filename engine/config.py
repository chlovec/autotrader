import argparse
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    broker: str  # which BrokerClient implementation to use - see engine/brokers/make_broker()

    # Alpaca-specific. Only read/used when broker == "alpaca" - a different broker
    # would have its own fields here (IBKR has no API key at all; Questrade uses an
    # OAuth refresh token), not a shared "generic" shape, since brokers genuinely
    # don't share an auth model.
    #
    # alpaca_api_key/alpaca_secret_key are already resolved to whichever pair
    # matches alpaca_paper (see load_config()) - paper and live are separate
    # Alpaca accounts with separate keys, so AlpacaBroker just uses these two
    # fields without needing to know there were ever two pairs to choose from.
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    alpaca_paper: bool

    # IBKR-specific. Only read when broker == "ibkr". No API key - auth happens by
    # logging into a locally running TWS/Gateway instance yourself; this just
    # connects to its socket.
    ibkr_host: str
    ibkr_port: int
    ibkr_client_id: int

    # Questrade-specific. Only read when broker == "questrade". Auth is an OAuth
    # refresh token - see engine/brokers/questrade_broker.py for why this single
    # field isn't the whole story (Questrade rotates it on every use).
    questrade_refresh_token: str
    # Questrade has no push/webhook API, so its BrokerClient.stream() simulates one by
    # polling on this interval. Configurable rather than hardcoded so it can be tuned
    # without a code change if Questrade's rate limits ever get tight.
    questrade_poll_interval_seconds: float

    max_position_size_usd: float
    max_daily_loss_usd: float
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    alert_email_from: str
    alert_email_to: str


def _str_to_bool(value: str) -> bool:
    return value.lower() == "true"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Autotrader configuration - any flag not passed falls back to its "
        "environment variable / .env value.",
    )
    parser.add_argument("--broker", default=None)
    parser.add_argument("--alpaca-api-key", default=None)
    parser.add_argument("--alpaca-secret-key", default=None)
    parser.add_argument("--alpaca-base-url", default=None)
    parser.add_argument("--alpaca-paper", type=_str_to_bool, default=None)
    parser.add_argument("--ibkr-host", default=None)
    parser.add_argument("--ibkr-port", type=int, default=None)
    parser.add_argument("--ibkr-client-id", type=int, default=None)
    parser.add_argument("--questrade-refresh-token", default=None)
    parser.add_argument("--questrade-poll-interval-seconds", type=float, default=None)
    parser.add_argument("--max-position-size-usd", type=float, default=None)
    parser.add_argument("--max-daily-loss-usd", type=float, default=None)
    parser.add_argument("--smtp-host", default=None)
    parser.add_argument("--smtp-port", type=int, default=None)
    parser.add_argument("--smtp-username", default=None)
    parser.add_argument("--smtp-password", default=None)
    parser.add_argument("--alert-email-from", default=None)
    parser.add_argument("--alert-email-to", default=None)
    return parser


def load_config(argv: list[str] | None = None) -> Config:
    """Resolves configuration from CLI flags, falling back to the matching environment
    variable/.env value for any flag not passed. `argv=None` (the default) reads the
    process's real sys.argv - only the top-level entrypoint scripts (run.py,
    run_portfolio.py, run_research.py) should rely on that default; every other caller
    (backend, ad-hoc scripts, internal fallbacks) passes argv=[] explicitly so it never
    picks up an unrelated argv (uvicorn's, pytest's, ...) - parse_known_args ignores
    unrecognized tokens either way, but argv=[] makes the intent explicit rather than
    relying on that being harmless."""
    args, _unused = _build_parser().parse_known_args(argv)

    # if args.broker == "alpaca":
    alpaca_paper = args.alpaca_paper if args.alpaca_paper is not None else os.environ.get("ALPACA_PAPER", "true").lower() == "true"
    if alpaca_paper:
        alpaca_api_key = args.alpaca_api_key or os.environ.get("ALPACA_API_KEY", "")
        alpaca_secret_key = args.alpaca_secret_key or os.environ.get("ALPACA_SECRET_KEY", "")
        alpaca_base_url = args.alpaca_base_url or os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    else:
        alpaca_api_key = args.alpaca_api_key or os.environ.get("ALPACA_LIVE_API_KEY", "")
        alpaca_secret_key = args.alpaca_secret_key or os.environ.get("ALPACA_LIVE_SECRET_KEY", "")
        alpaca_base_url = args.alpaca_base_url or os.environ.get("ALPACA_LIVE_BASE_URL", "https://api.alpaca.markets")
        if not alpaca_api_key or not alpaca_secret_key:
            raise RuntimeError(
                "ALPACA_PAPER=false but ALPACA_LIVE_API_KEY/ALPACA_LIVE_SECRET_KEY are not set - "
                "refusing to start rather than silently trading on paper credentials instead."
            )

    return Config(
        broker=args.broker or os.environ.get("BROKER", "alpaca"),
        alpaca_api_key=alpaca_api_key,
        alpaca_secret_key=alpaca_secret_key,
        alpaca_base_url=alpaca_base_url,
        alpaca_paper=alpaca_paper,
        ibkr_host=args.ibkr_host or os.environ.get("IBKR_HOST", "127.0.0.1"),
        ibkr_port=args.ibkr_port if args.ibkr_port is not None else int(os.environ.get("IBKR_PORT", "7497")),
        ibkr_client_id=args.ibkr_client_id if args.ibkr_client_id is not None else int(os.environ.get("IBKR_CLIENT_ID", "1")),
        questrade_refresh_token=args.questrade_refresh_token or os.environ.get("QUESTRADE_REFRESH_TOKEN", ""),
        questrade_poll_interval_seconds=(
            args.questrade_poll_interval_seconds
            if args.questrade_poll_interval_seconds is not None
            else float(os.environ.get("QUESTRADE_POLL_INTERVAL_SECONDS", "5"))
        ),
        max_position_size_usd=(
            args.max_position_size_usd if args.max_position_size_usd is not None else float(os.environ.get("MAX_POSITION_SIZE_USD", "1000"))
        ),
        max_daily_loss_usd=(
            args.max_daily_loss_usd if args.max_daily_loss_usd is not None else float(os.environ.get("MAX_DAILY_LOSS_USD", "200"))
        ),
        smtp_host=args.smtp_host or os.environ.get("SMTP_HOST", ""),
        smtp_port=args.smtp_port if args.smtp_port is not None else int(os.environ.get("SMTP_PORT", "587")),
        smtp_username=args.smtp_username or os.environ.get("SMTP_USERNAME", ""),
        smtp_password=args.smtp_password or os.environ.get("SMTP_PASSWORD", ""),
        alert_email_from=args.alert_email_from or os.environ.get("ALERT_EMAIL_FROM", ""),
        alert_email_to=args.alert_email_to or os.environ.get("ALERT_EMAIL_TO", ""),
    )