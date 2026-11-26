"""Inspect, export, and safely back up futures paper-trading databases."""

import argparse
from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def readonly_connection(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.execute('PRAGMA query_only=ON')
    connection.execute('PRAGMA busy_timeout=5000')
    return connection


def _require_database(path):
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Paper database not found: {source}")
    return source


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_status(path):
    source = _require_database(path)
    with closing(readonly_connection(source)) as database:
        database.execute('BEGIN')
        row = database.execute("SELECT data FROM state WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Paper database has no state row")
        state = json.loads(row[0])
        event_count = database.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        trade_count, net_pnl = database.execute(
            "SELECT COUNT(*), COALESCE(SUM(net_pnl),0) FROM trades"
        ).fetchone()
    return {
        "database": str(source),
        "identity": state["identity"],
        "balance": state["balance"],
        "position": state["position"],
        "last_candle": state["candle"],
        "daily": state["daily"],
        "events": event_count,
        "closed_trades": trade_count,
        "closed_net_pnl": net_pnl,
    }


def backup_database(source_path, destination_path):
    source = _require_database(source_path)
    destination = Path(destination_path)
    if destination.exists():
        raise FileExistsError(f"Backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(readonly_connection(source)) as source_database:
        with closing(sqlite3.connect(destination)) as destination_database:
            source_database.backup(destination_database, pages=100, sleep=0.05)
    return {
        "source": str(source),
        "backup": str(destination),
        "sha256": file_sha256(destination),
        "bytes": destination.stat().st_size,
    }


def _write_query_csv(database, query, destination):
    cursor = database.execute(query)
    with Path(destination).open("x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(column[0] for column in cursor.description)
        writer.writerows(cursor)


def export_database(source_path, output_directory):
    source = _require_database(source_path)
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=False)
    snapshot = output / "snapshot.sqlite3"
    backup_database(source, snapshot)

    with closing(readonly_connection(snapshot)) as database:
        state_row = database.execute("SELECT data FROM state WHERE id=1").fetchone()
        if state_row is None:
            raise ValueError("Paper database has no state row")
        (output / "state.json").write_text(
            json.dumps(json.loads(state_row[0]), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _write_query_csv(
            database,
            "SELECT candle,event,payload,created_at FROM events ORDER BY candle",
            output / "events.csv",
        )
        _write_query_csv(
            database,
            """
            SELECT opened_at,closed_at,side,entry_price,exit_price,quantity,
                   gross_pnl,fees,net_pnl,reason
            FROM trades ORDER BY opened_at
            """,
            output / "trades.csv",
        )
        funding_exists = database.execute("SELECT 1 FROM sqlite_master WHERE name='funding_payments'").fetchone()
        query = ('SELECT opened_at,timestamp,rate,mark_price,pnl FROM funding_payments ORDER BY timestamp'
                 if funding_exists else 'SELECT NULL AS opened_at,NULL AS timestamp,NULL AS rate,NULL AS mark_price,NULL AS pnl WHERE 0')
        _write_query_csv(database, query, output / 'funding.csv')

    files = [snapshot, output / "state.json", output / "events.csv", output / "trades.csv", output / 'funding.csv']
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "files": {
            item.name: {"sha256": file_sha256(item), "bytes": item.stat().st_size}
            for item in files
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def backup_all(data_directory, output_directory):
    data = Path(data_directory)
    sources = sorted(data.glob("*.sqlite3"))
    if not sources:
        raise FileNotFoundError(f"No paper databases found in {data}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(output_directory) / stamp
    target.mkdir(parents=True, exist_ok=False)
    results = [
        backup_database(source, target / source.name)
        for source in sources
    ]
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "backups": results,
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    status_parser = commands.add_parser("status")
    status_parser.add_argument("--state", required=True)
    status_parser.add_argument("--max-age-seconds", type=float)

    backup_parser = commands.add_parser("backup")
    backup_parser.add_argument("--state", required=True)
    backup_parser.add_argument("--output", required=True)

    export_parser = commands.add_parser("export")
    export_parser.add_argument("--state", required=True)
    export_parser.add_argument("--output-dir", required=True)

    all_parser = commands.add_parser("backup-all")
    all_parser.add_argument("--data-dir", required=True)
    all_parser.add_argument("--output-dir", required=True)

    args = parser.parse_args()
    if args.command == "status":
        result = read_status(args.state)
        if args.max_age_seconds is not None:
            if args.max_age_seconds <= 0:
                parser.error('max-age-seconds must be positive')
            result['stale'] = (time.time() - result['last_candle'] / 1000 > args.max_age_seconds)
            if result['stale']:
                print(json.dumps(result, ensure_ascii=False))
                raise SystemExit(1)
    elif args.command == "backup":
        result = backup_database(args.state, args.output)
    elif args.command == "export":
        result = export_database(args.state, args.output_dir)
    else:
        target, manifest = backup_all(args.data_dir, args.output_dir)
        result = {"directory": str(target), **manifest}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
