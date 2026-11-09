# VPS 24시간 모의매매 배포

이 구성은 BTC·ETH·SOL USDT-M 모의매매 세 개를 Docker로 실행한다. API 키와 실제 주문 기능은 사용하지 않는다. 세 실행기는 각각 1,000 USDT의 독립된 가상 계좌이므로 하나의 포트폴리오로 합산하지 않는다.

## 준비 사항

- Ubuntu 22.04 이상 VPS
- Docker Engine과 Docker Compose 플러그인
- Binance Futures 공개 API 이용이 허용되는 계정·지역·서버 IP
- 최소 1 vCPU, RAM 1GB, 디스크 10GB

서버를 구매하기 전에 해당 서버 위치에서 Binance Futures 공개 API가 정상 응답하는지 확인한다. 지역 제한을 우회하기 위한 서버로 사용하지 않는다.

## 설치

```sh
sudo mkdir -p /opt/coin-bot
sudo chown "$USER":"$USER" /opt/coin-bot
git clone https://github.com/202130435/test.git /opt/coin-bot
cd /opt/coin-bot
docker compose build
```

서버 사용자의 UID/GID가 1000이 아니면 `/opt/coin-bot/.env`에 다음을 저장한다.

```text
PAPER_UID=실제_UID
PAPER_GID=실제_GID
```

값은 `id -u`, `id -g`로 확인한다. 이 파일에는 API 키를 저장하지 않는다.

## 연결 사전 확인

BTC 공개 데이터를 한 번 읽고 DB 생성까지 확인한다.

```sh
docker compose run --rm btc \
  python futures_paper.py \
  --symbol BTC/USDT:USDT \
  --allow-short \
  --state /data/btc-v1.sqlite3 \
  --once
```

HTTP 451, 403 또는 접근 제한이 나오면 그 서버에서는 실행하지 않는다.

## 실행과 확인

```sh
docker compose up -d
docker compose ps
docker compose logs --tail=100 -f btc eth sol
```

세 서비스는 서버가 재부팅되어도 Docker의 `unless-stopped` 정책으로 다시 시작한다. 각 서비스는 새 15분봉이 마감됐을 때만 전략을 처리한다.

상태 확인:

```sh
docker compose exec btc python paper_data.py status --state /data/btc-v1.sqlite3
docker compose exec eth python paper_data.py status --state /data/eth-v1.sqlite3
docker compose exec sol python paper_data.py status --state /data/sol-v1.sqlite3
```

중단과 재시작:

```sh
docker compose stop
docker compose start
```

## 데이터 구조

Docker named volume `coin-paper_paper-data`에 다음 DB가 저장된다.

```text
btc-v1.sqlite3
eth-v1.sqlite3
sol-v1.sqlite3
```

각 DB에는 다음이 들어 있다.

- `state`: 현재 잔고·포지션·일일 위험 상태
- `events`: 모든 15분봉의 `wait`, `blocked`, `entry`, `hold`, `exit`
- `trades`: 모든 청산 거래와 수수료 포함 손익

`events`와 `trades`는 최근 100개 제한 없이 계속 누적된다.

## 실행 중 백업

프로젝트 디렉터리에 백업을 저장한다.

```sh
mkdir -p /opt/coin-bot/backups
docker compose --profile tools run --rm backup
```

SQLite backup API를 사용하므로 세 수집기를 중지하지 않아도 일관된 백업이 만들어진다. 결과에는 DB별 SHA-256과 크기를 기록한 `manifest.json`이 포함된다.

CSV·JSON 내보내기:

```sh
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose --profile tools run --rm backup \
  python paper_data.py export \
  --state /data/btc-v1.sqlite3 \
  --output-dir "/backups/btc-$stamp"
```

출력 디렉터리에는 안전한 SQLite 스냅샷, `state.json`, `events.csv`, `trades.csv`, `manifest.json`이 생성된다.

## systemd 자동 부팅과 매일 백업

저장소가 `/opt/coin-bot`에 있을 때 제공된 유닛을 설치한다.

```sh
sudo cp deploy/systemd/coin-paper.service /etc/systemd/system/
sudo cp deploy/systemd/coin-paper-backup.service /etc/systemd/system/
sudo cp deploy/systemd/coin-paper-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coin-paper.service
sudo systemctl enable --now coin-paper-backup.timer
```

확인:

```sh
systemctl status coin-paper.service
systemctl status coin-paper-backup.timer
systemctl list-timers coin-paper-backup.timer
```

timer는 하루에 한 번 세 DB를 백업하며, 서버가 꺼져 있던 시간에 예정된 백업도 다음 부팅 후 실행한다.

## 업데이트

```sh
cd /opt/coin-bot
git pull --ff-only origin main
docker compose build
sudo systemctl restart coin-paper.service
```

DB 볼륨은 `docker compose down`으로 삭제되지 않는다. `docker compose down -v`는 전체 모의매매 데이터를 삭제하므로 사용하지 않는다.

## 외부 접속

인터넷에 관리 포트를 직접 공개하지 않는다. Tailscale SSH 또는 SSH 키 인증을 사용해 휴대폰·노트북에서 서버에 접속한 뒤 위 상태 명령을 실행한다. 실거래 기능을 추가하기 전에는 별도의 테스트넷 API 키와 고정 IP·출금 권한 차단 정책이 필요하다.
