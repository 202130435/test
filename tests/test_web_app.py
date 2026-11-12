import json

from futures_paper import PaperState
from web_app import build_dashboard, read_dashboard_database


def make_database(path, symbol="BTC/USDT:USDT"):
    state = PaperState(path, [symbol], 1_000)
    state.record_event(1_700_000_000_000, "wait", {"price": 35_000})
    state.record_trade(
        {
            "opened_at": 1_699_999_000_000,
            "closed_at": 1_700_000_000_000,
            "side": "long",
            "entry_price": 34_000,
            "exit_price": 35_000,
            "quantity": 0.01,
            "gross_pnl": 10,
            "fees": 0.5,
            "net_pnl": 9.5,
            "reason": "target",
        }
    )
    state.data["balance"] = 1_009.5
    state.data["candle"] = 1_700_000_000_000
    state.save()
    return state


def test_dashboard_reads_live_database_without_mutating_it(tmp_path):
    path = tmp_path / "btc-v1.sqlite3"
    state = make_database(path)
    before = path.read_bytes()

    result = read_dashboard_database(path)

    assert result["symbol"] == "BTC"
    assert result["closed_net_pnl"] == 9.5
    assert result["total_fees"] == 0.5
    assert result["recent_events"][0]["payload"]["price"] == 35_000
    assert result["recent_trades"][0]["reason"] == "target"
    assert path.read_bytes() == before
    state.db.close()
    state.lock.close()


def test_dashboard_isolates_a_broken_database(tmp_path):
    state = make_database(tmp_path / "btc-v1.sqlite3")
    (tmp_path / "broken.sqlite3").write_text("not sqlite", encoding="utf-8")

    result = build_dashboard(tmp_path)

    assert [account["symbol"] for account in result["accounts"]] == ["BTC"]
    assert result["errors"][0]["database"] == "broken.sqlite3"
    json.dumps(result)
    state.db.close()
    state.lock.close()
