# 바이낸스 현물 매매 봇

Python 3.12 / CCXT. 기본은 실제 시세를 이용한 로컬 모의매매입니다. 바이낸스 테스트넷 주문과는 다릅니다.

## 설치 및 실행

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/python bot.py --once
.venv/bin/python bot.py
```

### Windows PowerShell

Python 3.12를 설치하고 프로젝트 파일을 PC에 내려받은 뒤, `bot.py`가 있는 폴더에서 PowerShell을 여세요. GitHub 저장소에서 Code → Download ZIP을 선택해 내려받거나 Git으로 복제할 수 있습니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe bot.py --once
```

계속 모의매매하려면:

```powershell
.\.venv\Scripts\python.exe bot.py
```

가상환경 활성화가 필요 없어 PowerShell 실행 정책을 변경하지 않아도 됩니다. `py`를 찾지 못하면 Python 설치 및 Python Launcher 설치 여부를 확인하세요. `--once`가 출력 없이 정상 종료할 경우 시세 조회는 완료했지만 매매 신호가 없었던 것입니다. `--live`를 지정하지 않으면 실제 주문을 보내지 않으며 API 키도 필요하지 않습니다. 종료는 Ctrl+C입니다. Windows와 Linux/macOS에서 파일 잠금은 `portalocker`로 처리합니다.

현재 클라우드에서는 `.venv/bin/python` 대신 `/workspace/coin-bot-venv/bin/python`을 사용할 수 있습니다.

기본 전략: BTC/USDT, 1분 봉의 5/20 단순이동평균 교차. 완성된 봉에서 상승 교차 시 시장가 매수, 하락 교차 시 봇이 보유한 수량만 매도합니다. 기본 가상 자본은 100 USDT, 매수 금액은 10 USDT, 모의 수수료는 0.1%입니다. `--symbol`, `--timeframe`, `--fast`, `--slow`, `--capital`, `--quote-amount`, `--fee`로 조정합니다. 매매 신호가 없으면 주문하지 않습니다.

상태는 SQLite에 저장하며 같은 데이터베이스를 쓰는 동시 실행은 차단합니다. 모드·전략을 변경할 때는 별도 `--state` 경로를 사용하세요. 종료는 Ctrl+C입니다. 종료해도 보유 코인은 자동으로 매도하지 않습니다.

## 실거래

실거래 주문 경로는 구현했지만 실제 계정에서 검증하지 않았습니다. 손절, 최대 손실 제한, 자동 주문 복구, 슬리피지 제한은 아직 없습니다. 모의매매와 주문 테스트를 충분히 검증한 후 사용하세요. 전략 수익성은 검증되지 않았습니다.

전용 계정/API 키 사용을 권장합니다. 출금 권한 없이 현물 거래 권한만 부여하세요. `BINANCE_API_KEY`와 `BINANCE_API_SECRET`을 안전한 실행 환경에서 공급해야 합니다. 키를 코드·Git·채팅에 넣지 마세요. Binance HMAC 서명에는 프로세스가 사용할 수 있는 실제 secret이 필요하며 HTTPS 프록시 치환용 placeholder는 사용할 수 없습니다.

직접 실행하려면 다음 두 옵션을 함께 지정해야 합니다:

```sh
.venv/bin/python bot.py --live --confirm-live BINANCE_SPOT_REAL_ORDERS --state live.sqlite3 --quote-amount 10 --capital 100
```

`--capital`은 봇 내부 예산이며 계좌 전체 잔고가 아닙니다. 봇은 단일 포지션만 유지합니다. 시장가 체결 금액은 가격 변동으로 설정한 매수 금액을 초과할 수 있습니다. 최소 주문 수량/금액을 충족하지 못하면 정지합니다. 수수료와 정밀도로 발생한 잔여 수량은 수동 확인이 필요할 수 있습니다.

주문 전 intent를 영구 저장합니다. 응답 유실, 부분 체결 또는 불명확한 응답이면 실행 및 재시작을 중단하며 자동 재주문하지 않습니다. 바이낸스에서 `spotbot-<봉 timestamp>-<buy/sell>` 주문 ID를 조회하여 체결·잔고를 확인하고 DB를 백업한 뒤 상태를 수동 조정해야 합니다. 미해결 상태를 지우거나 새 DB로 무작정 재시작하면 중복 주문이 생길 수 있습니다. 여러 DB로 같은 계정을 동시에 운영하지 마세요.

## 네트워크

`api.binance.com` HTTPS 접근이 필요합니다. 클라우드 프록시와 CA 설정을 그대로 사용하고 TLS 검증을 유지합니다. 지역 제한이나 네트워크 오류를 우회하지 않습니다. 오류가 나면 봇은 정지합니다.

## 타임아웃과 공개 API 장애 대응

[참고 글](https://velog.io/@whird625/바이낸스-에러-해결)의 타임아웃과 다중 호스트 접근을 현물 봇에 맞게 적용했습니다.

요청 타임아웃은 기본 3,500ms이며 `--timeout-ms`로 변경합니다. 이는 요청별 제한이며 전체 실행 시간을 3.5초로 제한하는 것은 아닙니다. 공개 시장정보·봉 조회가 타임아웃 또는 일시적 서버/연결 오류로 실패하면 `api.binance.com`, `api1.binance.com`, `api2.binance.com`, `api3.binance.com` 순서로 최대 한 번씩 시도합니다. 이 호스트들도 클라우드 네트워크 허용 목록에 있어야 합니다.

451 지역 제한 및 403/418/429 접근 제한·요청 제한에는 폴백하지 않고 정지합니다. 실거래 잔고 조회와 주문 전송은 기본 호스트를 유지하고 자동 재시도하지 않습니다. `data-api.binance.vision`은 주문을 처리하는 API가 아니므로 주문 대상으로 추가하지 않았습니다. 리전 변경은 이 프로젝트의 코드 설정으로 할 수 없으며, 현재 클라우드에서 반환된 451의 정확한 지역 원인은 확인되지 않았습니다.
