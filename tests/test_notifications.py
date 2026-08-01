from unittest.mock import patch

from engine.config import Config
from engine.notifications import CompositeNotifier, EmailNotifier, MacNotifier, _applescript_string, make_notifier


def _config(smtp_host: str = "", alert_email_to: str = "") -> Config:
    return Config(
        broker="alpaca",
        alpaca_api_key="",
        alpaca_secret_key="",
        alpaca_base_url="",
        alpaca_paper=True,
        max_position_size_usd=1000.0,
        max_daily_loss_usd=200.0,
        smtp_host=smtp_host,
        smtp_port=587,
        smtp_username="user",
        smtp_password="pass",
        alert_email_from="from@example.com",
        alert_email_to=alert_email_to,
    )


def test_composite_notifier_calls_all():
    calls = []

    class Fake:
        def notify(self, level, subject, message):
            calls.append((level, subject, message))

    CompositeNotifier([Fake(), Fake()]).notify("error", "subj", "msg")
    assert len(calls) == 2


def test_make_notifier_without_email_config_is_mac_only():
    notifier = make_notifier(_config())
    assert len(notifier.notifiers) == 1
    assert isinstance(notifier.notifiers[0], MacNotifier)


def test_make_notifier_with_email_config_adds_email():
    notifier = make_notifier(_config(smtp_host="smtp.example.com", alert_email_to="me@example.com"))
    assert len(notifier.notifiers) == 2
    assert any(isinstance(n, EmailNotifier) for n in notifier.notifiers)


def test_applescript_string_escapes_quotes_and_backslashes():
    assert _applescript_string('say "hi"') == '"say \\"hi\\""'
    assert _applescript_string("back\\slash") == '"back\\\\slash"'


def test_mac_notifier_swallows_errors():
    with patch("engine.notifications.subprocess.run", side_effect=Exception("boom")):
        MacNotifier().notify("error", "subj", "msg")  # must not raise


def test_email_notifier_swallows_errors():
    with patch("engine.notifications.smtplib.SMTP", side_effect=Exception("boom")):
        EmailNotifier("smtp.example.com", 587, "user", "pass", "from@x.com", "to@x.com").notify("error", "subj", "msg")  # must not raise
