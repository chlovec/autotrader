import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Account, EquitySnapshot, SignalAction, Trade


class RiskManager:
    """Sits between the strategy and execution. A signal only becomes an order if approved
    here. Limits and kill-switch state come straight off the Account row - both are
    dashboard-editable per account, not global config anymore."""

    def __init__(self, session: Session, account: Account):
        self.session = session
        self.account = account

    def kill_switch_engaged(self) -> tuple[bool, str]:
        return self.account.kill_switch_engaged, self.account.kill_switch_reason

    def daily_pnl(self) -> float:
        today_start = dt.datetime.combine(dt.date.today(), dt.time.min)
        scope = (EquitySnapshot.account_id == self.account.id,)
        first = self.session.execute(
            select(EquitySnapshot).where(EquitySnapshot.timestamp >= today_start, *scope).order_by(EquitySnapshot.timestamp)
        ).scalars().first()
        latest = self.session.execute(
            select(EquitySnapshot).where(*scope).order_by(EquitySnapshot.timestamp.desc())
        ).scalars().first()
        if not first or not latest:
            return 0.0
        return latest.equity - first.equity

    def current_exposure_usd(self, symbol: str) -> float:
        total = self.session.execute(
            select(func.coalesce(func.sum(Trade.qty * Trade.fill_price), 0.0))
            .where(Trade.symbol == symbol, Trade.account_id == self.account.id)
        ).scalar_one()
        return float(total)

    def daily_loss_limit_breached(self) -> bool:
        """Checked once per cycle by the runners (before generating any signals) so a
        breach halts everything for the day rather than being rejected signal-by-signal
        the way the position-size cap below is."""
        return self.daily_pnl() <= -self.account.max_daily_loss_usd

    def approve(self, symbol: str, action: SignalAction, order_value_usd: float) -> tuple[bool, str]:
        engaged, reason = self.kill_switch_engaged()
        if engaged:
            return False, f"kill switch engaged: {reason}"

        if action == SignalAction.hold:
            return False, "hold signal, nothing to do"

        if action == SignalAction.buy:
            projected = self.current_exposure_usd(symbol) + order_value_usd
            if projected > self.account.max_position_size_usd:
                return False, f"order would exceed max position size (${self.account.max_position_size_usd})"

        return True, "approved"
