import pytest

from engine.runner import main


def test_main_validates_config_before_starting_scheduler(monkeypatch):
    """main() must resolve config before scheduler.start() - otherwise a bad config
    (e.g. ALPACA_PAPER=false with no live keys) wouldn't surface until the next
    scheduled cron fire, and even then only as a logged APScheduler job error rather
    than a startup crash."""

    def boom(argv=None):
        raise RuntimeError("bad live config")

    monkeypatch.setattr("engine.runner.load_config", boom)

    with pytest.raises(RuntimeError, match="bad live config"):
        main(strategy=None)
