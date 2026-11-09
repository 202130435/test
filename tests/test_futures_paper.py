from types import SimpleNamespace

import pytest

from futures_paper import (
    PaperState,
    automatic_assessment,
    current_equity,
    paper_step,
)
from futures_risk import RiskConfig
from futures_strategy import StrategyConfig
from trade_checklist import ChecklistAssessment


def candles(closes, start=0, volumes=None):
    volumes = volumes or [1] * len(closes)
    return [
        [start + index, close, close + 1, max(0.01, close - 1), close, volume]
        for index, (close, volume) in enumerate(zip(closes, volumes))
    ]


def rising():
    return candles([100 + index * 0.25 for index in range(220)])


def turn_up(start=0):
    return candles([100] * 21 + [110], start=start, volumes=[1] * 21 + [2])


def turn_down(start=0):
    return candles([100] * 21 + [90], start=start, volumes=[1] * 21 + [2])


class Exchange:
    def __init__(self):
        self.trigger = turn_up(1_700_000_000_000)
        self.has = {"fetchOpenInterest": True}

    def fetch_ohlcv(self, symbol, timeframe, limit):
        values = rising() if timeframe in {"1d", "4h"} else self.trigger
        forming = [values[-1][0] + 1, 0, 0, 0, values[-1][4], 0]
        return values + [forming]

    def fetch_open_interest(self, symbol):
        return {"openInterestAmount": 1234}


def args():
    return SimpleNamespace(
        symbol="BTC/USDT:USDT",
        leverage=2,
        fee=0.0005,
        slippage=0.0005,
        volume_multiplier=1.2,
    )


def complete_assessment(*unused):
    return ChecklistAssessment(
        signal_reviewed=True,
        location_reviewed=True,
        location_valid=True,
        timeframe_reviewed=True,
        timeframe_aligned=True,
        volume_reviewed=True,
        volume_confirmed=True,
        repeated_signal_risk_reviewed=True,
        structure_reviewed=True,
        structure_confirmed=True,
        oi_reviewed=True,
    )


def test_paper_entry_deduplication_and_exit(tmp_path, capsys):
    exchange = Exchange()
    state = PaperState(tmp_path / "paper.db", ["test"], 1_000)
    strategy = StrategyConfig()
    risk = RiskConfig()

    paper_step(exchange, state, args(), strategy, risk, complete_assessment)
    assert state.data["position"]["side"] == "long"
    assert state.data["balance"] < 1_000
    first_candle = state.data["candle"]

    paper_step(exchange, state, args(), strategy, risk, complete_assessment)
    assert state.data["candle"] == first_candle
    assert "already_processed" in capsys.readouterr().out

    exchange.trigger = turn_down(1_700_000_001_000)
    paper_step(exchange, state, args(), strategy, risk, complete_assessment)
    assert state.data["position"] is None
    assert len(state.data["trades"]) == 1
    assert state.data["daily"]["trade_count"] == 1
    assert state.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    assert state.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_equity_marks_long_and_short_positions():
    long_data = {
        "balance": 990,
        "position": {"side": "long", "entry_price": 100, "quantity": 2},
    }
    short_data = {
        "balance": 990,
        "position": {"side": "short", "entry_price": 100, "quantity": 2},
    }
    assert current_equity(long_data, 105) == pytest.approx(1_000)
    assert current_equity(short_data, 95) == pytest.approx(1_000)


def test_automatic_checklist_requires_oi_and_volume_confirmation():
    trigger = candles([100] * 22, volumes=[1] * 22)
    assessment = automatic_assessment(trigger, "long", False, 1.2)
    assert not assessment.oi_reviewed
    assert not assessment.volume_confirmed
