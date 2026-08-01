import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import EquitySnapshot, KillSwitch, SignalAction, Trade
from engine.config import Config


class RiskManager:
    """Sits between the strategy and execution. A signal only becomes an order if approved here."""

    def __init__(self, config: Config, session: Session):
        self.config = config
        self.session = session

    def kill_switch_engaged(self) -> tuple[bool, str]:
        switch = self.session.get(KillSwitch, 1)
        return (switch.engaged, switch.reason) if switch else (False, "")

    def daily_pnl(self) -> float:
        today_start = dt.datetime.combine(dt.date.today(), dt.time.min)
        first = self.session.execute(
            select(EquitySnapshot).where(EquitySnapshot.timestamp >= today_start).order_by(EquitySnapshot.timestamp)
        ).scalars().first()
        latest = self.session.execute(
            select(EquitySnapshot).order_by(EquitySnapshot.timestamp.desc())
        ).scalars().first()
        if not first or not latest:
            return 0.0
        return latest.equity - first.equity

    def current_exposure_usd(self, symbol: str) -> float:
        total = self.session.execute(
            select(func.coalesce(func.sum(Trade.qty * Trade.fill_price), 0.0)).where(Trade.symbol == symbol)
        ).scalar_one()
        return float(total)

    def approve(self, symbol: str, action: SignalAction, order_value_usd: float) -> tuple[bool, str]:
        engaged, reason = self.kill_switch_engaged()
        if engaged:
            return False, f"kill switch engaged: {reason}"

        if action == SignalAction.hold:
            return False, "hold signal, nothing to do"

        if self.daily_pnl() <= -self.config.max_daily_loss_usd:
            return False, "daily loss limit reached"

        if action == SignalAction.buy:
            projected = self.current_exposure_usd(symbol) + order_value_usd
            if projected > self.config.max_position_size_usd:
                return False, f"order would exceed max position size (${self.config.max_position_size_usd})"

        return True, "approved"
