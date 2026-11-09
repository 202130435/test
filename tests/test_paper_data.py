import csv
import json

from futures_paper import PaperState
from paper_data import backup_all, backup_database, export_database, read_status


def populated_database(path):
    state = PaperState(path, ["BTC/USDT:USDT"], 1_000)
    state.record_event(100, "wait", {"event": "wait", "price": 100})
    state.record_trade(
        {
            "opened_at": 90,
            "closed_at": 100,
            "side": "long",
            "entry_price": 100,
            "exit_price": 110,
            "quantity": 1,
            "gross_pnl": 10,
            "fees": 0.1,
            "net_pnl": 9.9,
            "reason": "target",
        }
    )
    state.data["balance"] = 1_009.9
    state.data["candle"] = 100
    state.save()
    return state


def test_status_and_backup_are_complete(tmp_path):
    source = tmp_path / "paper.sqlite3"
    state = populated_database(source)
    status = read_status(source)
    assert status["events"] == 1
    assert status["closed_trades"] == 1
    assert status["closed_net_pnl"] == 9.9

    target = tmp_path / "backup.sqlite3"
    result = backup_database(source, target)
    assert result["sha256"]
    assert read_status(target) == {**status, "database": str(target)}
    state.db.close()
    state.lock.close()


def test_export_contains_state_events_trades_and_manifest(tmp_path):
    source = tmp_path / "paper.sqlite3"
    state = populated_database(source)
    output = tmp_path / "export"
    manifest = export_database(source, output)
    assert set(manifest["files"]) == {
        "snapshot.sqlite3",
        "state.json",
        "events.csv",
        "trades.csv",
    }
    assert json.loads((output / "state.json").read_text())["balance"] == 1_009.9
    with (output / "trades.csv").open(newline="") as handle:
        trades = list(csv.DictReader(handle))
    assert trades[0]["net_pnl"] == "9.9"
    state.db.close()
    state.lock.close()


def test_backup_all_snapshots_every_symbol(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    states = [
        populated_database(data / "btc.sqlite3"),
        populated_database(data / "eth.sqlite3"),
    ]
    target, manifest = backup_all(data, tmp_path / "backups")
    assert len(manifest["backups"]) == 2
    assert (target / "btc.sqlite3").is_file()
    assert (target / "eth.sqlite3").is_file()
    for state in states:
        state.db.close()
        state.lock.close()
