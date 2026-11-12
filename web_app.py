"""Read-only web dashboard for futures paper-trading databases."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse


DATA_DIRECTORY = Path(os.environ.get("PAPER_DATA_DIR", "/data"))
RECENT_LIMIT = 20

app = FastAPI(
    title="Coin Paper Dashboard",
    description="Read-only status for the BTC, ETH, and SOL paper traders.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
)


def _database_connection(path):
    connection = sqlite3.connect(
        f"file:{Path(path).resolve()}?mode=ro",
        uri=True,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _symbol_from_identity(identity, fallback):
    if isinstance(identity, list) and identity and isinstance(identity[0], str):
        return identity[0].split("/")[0]
    return fallback.upper()


def read_dashboard_database(path, recent_limit=RECENT_LIMIT):
    """Read one database without acquiring the trader's exclusive lock."""
    if not 1 <= recent_limit <= 100:
        raise ValueError("recent_limit must be between 1 and 100")

    source = Path(path)
    with _database_connection(source) as database:
        row = database.execute("SELECT data FROM state WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Database has no state row")
        state = json.loads(row["data"])

        events = []
        for item in database.execute(
            """
            SELECT candle,event,payload,created_at
            FROM events ORDER BY candle DESC LIMIT ?
            """,
            (recent_limit,),
        ):
            try:
                payload = json.loads(item["payload"])
            except json.JSONDecodeError:
                payload = {}
            events.append(
                {
                    "candle": item["candle"],
                    "event": item["event"],
                    "payload": payload,
                    "created_at": item["created_at"],
                }
            )

        trades = [
            dict(item)
            for item in database.execute(
                """
                SELECT opened_at,closed_at,side,entry_price,exit_price,quantity,
                       gross_pnl,fees,net_pnl,reason
                FROM trades ORDER BY closed_at DESC LIMIT ?
                """,
                (recent_limit,),
            )
        ]
        totals = database.execute(
            """
            SELECT COUNT(*) AS count,
                   COALESCE(SUM(net_pnl),0) AS net_pnl,
                   COALESCE(SUM(fees),0) AS fees
            FROM trades
            """
        ).fetchone()

    symbol = _symbol_from_identity(state.get("identity"), source.stem)
    return {
        "id": source.stem,
        "symbol": symbol,
        "balance": state.get("balance", 0),
        "position": state.get("position"),
        "last_candle": state.get("candle", 0),
        "daily": state.get("daily"),
        "closed_trades": totals["count"],
        "closed_net_pnl": totals["net_pnl"],
        "total_fees": totals["fees"],
        "recent_events": events,
        "recent_trades": trades,
    }


def discover_databases(data_directory=DATA_DIRECTORY):
    directory = Path(data_directory)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.sqlite3"))


def build_dashboard(data_directory=DATA_DIRECTORY, recent_limit=RECENT_LIMIT):
    accounts = []
    errors = []
    for path in discover_databases(data_directory):
        try:
            accounts.append(read_dashboard_database(path, recent_limit))
        except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as error:
            errors.append({"database": path.name, "error": str(error)})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "accounts": accounts,
        "errors": errors,
    }


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'"
    )
    return response


@app.get("/health")
def health():
    databases = discover_databases()
    return {"status": "ok", "databases": len(databases)}


@app.get("/api/dashboard")
def dashboard_api():
    return JSONResponse(build_dashboard())


DASHBOARD_HTML = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>CoinPilot Lab</title>
  <style>
    :root { color-scheme: dark; --bg:#080b12; --panel:#111827; --line:#253047;
      --text:#f3f6fb; --muted:#91a0b8; --green:#2dd4a7; --red:#fb7185; --blue:#60a5fa; }
    * { box-sizing:border-box; }
    body { margin:0; background:radial-gradient(circle at top,#14213a 0,#080b12 38%);
      color:var(--text); font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }
    main { width:min(1180px,calc(100% - 32px)); margin:auto; padding:36px 0 64px; }
    header { display:flex; justify-content:space-between; align-items:end; gap:20px; margin-bottom:24px; }
    h1 { margin:0; font-size:clamp(26px,5vw,42px); letter-spacing:-.04em; }
    h2 { margin:0 0 14px; font-size:18px; }
    p { margin:4px 0 0; color:var(--muted); }
    .pill { border:1px solid var(--line); border-radius:999px; padding:7px 11px; color:var(--muted); }
    .grid { display:grid; grid-template-columns:repeat(3,1fr); gap:16px; }
    .card,.section { background:rgba(17,24,39,.9); border:1px solid var(--line); border-radius:18px;
      box-shadow:0 14px 45px rgba(0,0,0,.24); }
    .card { padding:20px; }
    .symbol { display:flex; justify-content:space-between; align-items:center; font-weight:750; font-size:20px; }
    .position { font-size:12px; padding:4px 8px; border-radius:99px; background:#1f2937; }
    .metric { margin-top:18px; }
    .metric span { color:var(--muted); display:block; font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
    .metric strong { display:block; font-size:26px; margin-top:2px; }
    .submetrics { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:14px; }
    .submetrics div { border-top:1px solid var(--line); padding-top:11px; }
    .submetrics b { display:block; font-size:16px; }
    .positive { color:var(--green); } .negative { color:var(--red); }
    .section { padding:20px; margin-top:16px; overflow:hidden; }
    table { width:100%; border-collapse:collapse; min-width:690px; }
    th,td { padding:11px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }
    th { color:var(--muted); font-size:12px; font-weight:600; }
    .table-wrap { overflow:auto; }
    .empty { padding:34px; text-align:center; color:var(--muted); border:1px dashed var(--line); border-radius:14px; }
    .error { color:#fecdd3; background:#4c0519; padding:12px; border-radius:10px; margin-top:10px; }
    @media (max-width:800px) { .grid { grid-template-columns:1fr; } header { align-items:start; flex-direction:column; }
      main { width:min(100% - 22px,1180px); padding-top:22px; } }
  </style>
</head>
<body><main>
  <header><div><h1>CoinPilot Lab</h1><p>BTC · ETH · SOL 모의매매 현황</p></div><div class="pill" id="updated">불러오는 중</div></header>
  <div class="grid" id="accounts"></div>
  <section class="section"><h2>최근 청산 거래</h2><div class="table-wrap" id="trades"></div></section>
  <section class="section"><h2>최근 전략 판단</h2><div class="table-wrap" id="events"></div></section>
  <div id="errors"></div>
</main>
<script>
const money = value => Number(value || 0).toLocaleString('ko-KR',{minimumFractionDigits:2,maximumFractionDigits:2});
const when = value => value ? new Date(Number(value)).toLocaleString('ko-KR') : '-';
const pnlClass = value => Number(value) >= 0 ? 'positive' : 'negative';
const esc = value => String(value ?? '-').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function render(data) {
  document.querySelector('#updated').textContent = '갱신 ' + new Date(data.generated_at).toLocaleTimeString('ko-KR');
  const accounts = document.querySelector('#accounts');
  accounts.innerHTML = data.accounts.length ? data.accounts.map(a => {
    const side = a.position ? a.position.side.toUpperCase() : '대기';
    return `<article class="card"><div class="symbol">${esc(a.symbol)}<span class="position">${esc(side)}</span></div>
      <div class="metric"><span>가상 잔고</span><strong>${money(a.balance)} USDT</strong></div>
      <div class="submetrics"><div><span>누적 실현 손익</span><b class="${pnlClass(a.closed_net_pnl)}">${money(a.closed_net_pnl)}</b></div>
      <div><span>청산 거래</span><b>${a.closed_trades}회</b></div><div><span>누적 수수료</span><b>${money(a.total_fees)}</b></div>
      <div><span>마지막 봉</span><b>${when(a.last_candle)}</b></div></div></article>`;
  }).join('') : '<div class="empty">아직 표시할 모의매매 DB가 없습니다.</div>';
  const trades = data.accounts.flatMap(a => a.recent_trades.map(t=>({...t,symbol:a.symbol})))
    .sort((a,b)=>b.closed_at-a.closed_at).slice(0,30);
  document.querySelector('#trades').innerHTML = trades.length ? `<table><thead><tr><th>시간</th><th>종목</th><th>방향</th><th>진입</th><th>청산</th><th>순손익</th><th>사유</th></tr></thead><tbody>${trades.map(t=>`<tr><td>${when(t.closed_at)}</td><td>${esc(t.symbol)}</td><td>${esc(t.side)}</td><td>${money(t.entry_price)}</td><td>${money(t.exit_price)}</td><td class="${pnlClass(t.net_pnl)}">${money(t.net_pnl)}</td><td>${esc(t.reason)}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">아직 청산된 거래가 없습니다.</div>';
  const events = data.accounts.flatMap(a => a.recent_events.map(e=>({...e,symbol:a.symbol})))
    .sort((a,b)=>b.candle-a.candle).slice(0,30);
  document.querySelector('#events').innerHTML = events.length ? `<table><thead><tr><th>시간</th><th>종목</th><th>판단</th><th>가격</th><th>사유</th></tr></thead><tbody>${events.map(e=>`<tr><td>${when(e.candle)}</td><td>${esc(e.symbol)}</td><td>${esc(e.event)}</td><td>${money(e.payload.price)}</td><td>${esc((e.payload.reasons || e.payload.reason || []).toString())}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">아직 기록된 전략 판단이 없습니다.</div>';
  document.querySelector('#errors').innerHTML = data.errors.map(e=>`<div class="error">${esc(e.database)}: ${esc(e.error)}</div>`).join('');
}
async function refresh() {
  try { const response = await fetch('/api/dashboard'); if (!response.ok) throw new Error('HTTP '+response.status); render(await response.json()); }
  catch (error) { document.querySelector('#errors').innerHTML = `<div class="error">대시보드를 불러오지 못했습니다: ${esc(error.message)}</div>`; }
}
refresh(); setInterval(refresh,30000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard_page():
    return HTMLResponse(DASHBOARD_HTML)
