import logging
import smtplib
import subprocess
from email.message import EmailMessage
from typing import Protocol

from sqlalchemy.orm import Session

from db.models import SystemEvent
from engine.config import Config

logger = logging.getLogger("autotrader.notifications")


class Notifier(Protocol):
    def notify(self, level: str, subject: str, message: str) -> None: ...


def _applescript_string(text: str) -> str:
    """AppleScript string literals are double-quoted, not single-quoted like Python's
    repr() - using repr() here silently produces invalid syntax osascript rejects."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class MacNotifier:
    """Native macOS notification via osascript. Zero setup, but only useful while this
    machine is on - there's no remote delivery."""

    def notify(self, level: str, subject: str, message: str) -> None:
        title = f"Autotrader [{level.upper()}]"
        script = (
            f"display notification {_applescript_string(message)} "
            f"with title {_applescript_string(title)} subtitle {_applescript_string(subject)}"
        )
        try:
            subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=5)
        except Exception:
            logger.exception("failed to send macOS notification")


class EmailNotifier:
    def __init__(self, host: str, port: int, username: str, password: str, from_addr: str, to_addr: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.from_addr = from_addr
        self.to_addr = to_addr

    def notify(self, level: str, subject: str, message: str) -> None:
        email = EmailMessage()
        email["Subject"] = f"[Autotrader:{level.upper()}] {subject}"
        email["From"] = self.from_addr
        email["To"] = self.to_addr
        email.set_content(message)
        try:
            if self.port == 465:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=10) as smtp:
                    smtp.login(self.username, self.password)
                    smtp.send_message(email)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
                    smtp.starttls()
                    smtp.login(self.username, self.password)
                    smtp.send_message(email)
        except Exception:
            logger.exception("failed to send email alert")


class CompositeNotifier:
    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    def notify(self, level: str, subject: str, message: str) -> None:
        for notifier in self.notifiers:
            notifier.notify(level, subject, message)


def make_notifier(config: Config) -> Notifier:
    notifiers: list[Notifier] = [MacNotifier()]
    if config.smtp_host and config.alert_email_to:
        notifiers.append(
            EmailNotifier(
                config.smtp_host, config.smtp_port, config.smtp_username, config.smtp_password, config.alert_email_from, config.alert_email_to
            )
        )
    return CompositeNotifier(notifiers)


def log_and_notify(session: Session, notifier: Notifier, level: str, source: str, message: str, account_id: str | None = None) -> None:
    session.add(SystemEvent(level=level, source=source, message=message, account_id=account_id))
    session.commit()
    notifier.notify(level, source, message)
