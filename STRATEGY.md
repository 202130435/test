# 선물 다중 시간대 전략

`futures_strategy.py`는 주문을 전송하지 않는 순수 신호·리스크 모듈이다. 모든 입력은 아직 움직이는 현재 봉을 제외한 **완성된 봉**이어야 한다. 기본 진입 봉은 15분 봉을 권장한다.

## 1. 큰 시장 사이클

일봉과 4시간봉을 각각 다음과 같이 분류한다. 이동평균은 단순이동평균(SMA)이다.

```text
상승 정배열 = 종가 > SMA50 > SMA200
             AND SMA50 상승 중
             AND SMA200 상승 중

하락 역배열 = 종가 < SMA50 < SMA200
             AND SMA50 하락 중
             AND SMA200 하락 중

나머지 = 중립
```

두 시간대가 모두 상승 정배열일 때만 최종 시장 상태를 `BULL`로 판정한다. 두 시간대가 모두 하락 역배열일 때만 `BEAR`로 판정한다. 일봉과 4시간봉이 서로 다르거나 이동평균이 얽혀 있으면 `NEUTRAL`이다.

이렇게 하면 일봉만 상승이고 4시간봉이 하락 전환한 구간에서 성급하게 롱에 진입하지 않는다. 최소 201개의 완성된 일봉과 201개의 완성된 4시간봉이 필요하다.

## 2. 마트로시카 진입 구조

큰 사이클 안의 작은 사이클은 기본적으로 15분 봉 SMA5와 SMA20 교차로 판단한다.

```text
작은 상승 변곡 = 이전 SMA5 <= 이전 SMA20
                 AND 현재 SMA5 > 현재 SMA20
                 AND 종가 > 현재 SMA5

작은 하락 변곡 = 이전 SMA5 >= 이전 SMA20
                 AND 현재 SMA5 < 현재 SMA20
                 AND 종가 < 현재 SMA5
```

포지션이 없을 때 판단표는 다음과 같다.

| 큰 시장 상태 | 작은 사이클 | 기본 행동 | 근거 |
| --- | --- | --- | --- |
| `BULL` | 상승 변곡 | 롱 진입 | 일봉·4시간 상승 흐름 안에서 단기 상승이 재개됨 |
| `BULL` | 그 외 | 관망 | 이미 오른 구간을 추격하지 않고 다음 변곡을 기다림 |
| `NEUTRAL` | 모든 상태 | 관망 | 큰 시간대가 합의되지 않음 |
| `BEAR` | 모든 상태 | 관망 | 기본 정책은 하락장에서 롱을 금지하고 현금 상태 유지 |
| `BEAR` | 하락 변곡 | 축소 숏 | `allow_short=True`를 명시한 경우에만 허용 |

숏을 허용해도 기본 위험 배수는 `0.25`다. 예를 들어 일반 롱에서 계좌의 0.5%를 위험에 노출한다면 숏은 0.125%만 노출한다. 이는 요구한 “비중 최소화 또는 관망”을 설정으로 선택할 수 있게 한 것이다.

고정된 한 번의 교차만 노리는 구조가 아니다. 상승 정배열이 유지되는 동안 작은 하락 변곡에서 포지션을 닫고, 다음 작은 상승 변곡에서 다시 진입한다. 큰 인형 안에서 작은 인형을 반복하는 방식으로 추세 내부의 여러 파동을 분리해 거래한다. 거래 횟수가 늘면 수수료와 슬리피지도 늘기 때문에 누적 수익을 보장하지는 않는다.

## 3. 손절과 무포지션 헤지

롱 진입 시 초기 손절가는 다음 세 기준을 조합한다.

```text
기술적 손절 = 최근 10개 15분 봉의 최저가보다 0.1% 아래
변동성 손절 = 진입가 - 2 × ATR(14)
최대 허용 거리 = 진입가보다 3% 아래

최종 손절 = 기술적·변동성 기준 중 더 넓은 값,
            단 진입가에서 최대 3%를 넘기지 않음
```

숏은 위 조건을 대칭으로 계산한다. 실제 주문 모듈에서는 이 가격을 거래소 서버에 조건부 청산 주문으로 등록해야 한다. 프로그램이 꺼져 있어도 손절이 남아 있어야 하기 때문이다.

롱 보유 중에는 다음 조건을 우선순위대로 평가한다.

1. 해당 봉의 저가가 보호 손절가에 닿으면 즉시 롱 청산으로 판정한다.
2. 일봉·4시간 상승 정배열이 깨지면 롱을 청산하고 무포지션으로 전환한다.
3. 15분 SMA5가 SMA20 아래로 하향 교차하면 단기 하락 사이클로 보고 롱을 청산한다.
4. 위 조건이 없으면 롱을 유지하면서 손절가를 SMA20 또는 `종가 - 2×ATR` 방향으로만 올린다. 손절가는 다시 낮추지 않는다.

숏은 반대 조건으로 동작한다. 이 전략에서 “헤지”의 기본 의미는 반대 포지션을 동시에 잡는 것이 아니라 위험 자산 노출을 0으로 만드는 것이다. 양방향 포지션은 수수료·증거금·복구 상태를 복잡하게 만들기 때문에 포함하지 않았다.

## 4. 포지션 크기와 일일 중단

기본 위험 한도는 거래당 계좌 평가금의 0.5%, 레버리지 상한은 2배다.

```text
허용 손실액 = 기준 평가금 × 0.005 × 위험 배수
단위당 손실 = |진입가 - 손절가| + 예상 왕복 수수료
              + 예상 왕복 슬리피지 + 펀딩 버퍼
위험 기준 수량 = 허용 손실액 / 단위당 손실
레버리지 기준 최대 수량 = 계좌 평가금 × 2 / 진입가

최종 수량 = min(위험 기준 수량, 레버리지 기준 최대 수량)
```

`futures_risk.py`는 이 계산에 예상 비용을 포함하고, 일일 3R·연속 손실 3회·하루 6회 거래에서 세션을 중단하는 개발 시작값을 제공한다. 이 수치는 강의에서 제시한 값이 아니며 백테스트로 조정해야 한다. 자세한 구분은 [COURSE_SPEC.md](COURSE_SPEC.md)를 따른다.

## 5. 코드 사용 예

```python
from futures_risk import DailyRiskState
from futures_system import plan_entry
from trade_checklist import ChecklistAssessment

assessment = ChecklistAssessment(
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

result = plan_entry(
    daily=closed_daily_candles,
    four_hour=closed_4h_candles,
    trigger_candles=closed_15m_candles,
    assessment=assessment,
    equity=account_equity,
    leverage=2,
    daily_state=DailyRiskState("2026-11-09", account_equity),
)

if result.ready:
    print(result.trade_plan)
```

`result.decision.reasons`에는 전략 근거, `result.blocked_reasons`에는 체크리스트 또는 위험 차단 사유가 들어 있다. 이를 거래 일지와 모바일 알림에 사용할 수 있다.

## 6. 아직 주문에 연결되지 않은 부분

현재 `bot.py`는 기존 현물 1분봉 SMA5/20 봇이다. 선물 쪽은 신호(`futures_strategy.py`), 시장 근거(`market_evidence.py`), 체크리스트(`trade_checklist.py`), 위험 및 주문 계획(`futures_risk.py`, `futures_system.py`)과 공개 데이터 기반 로컬 모의매매 실행기(`futures_paper.py`)까지 구현되어 있다. 실제 선물 주문 실행에는 다음 작업이 추가로 필요하다.

- USDT-M 선물 데이터와 계정 상태 조회
- 격리 마진·단방향 모드·레버리지 검증
- 실제 체결가를 기준으로 수량과 손절가 재계산
- 거래소 서버에 `reduceOnly` 손절·익절 주문 등록
- 부분 체결, 재시작, 수동 주문 및 미확정 주문 동기화
- 수수료·슬리피지·펀딩비를 포함한 백테스트
- 테스트넷 및 최소 금액 단계별 검증

이 작업이 끝나기 전에는 선물 실거래용으로 사용할 수 없다.
