from types import SimpleNamespace
import pytest
from bot import State, signal, order_amount, step

class Exchange:
    def __init__(self, fail=False):
        self.urls = {'api': {'public': 'https://api.binance.com/api/v3'}}
        self.orders = []
        self.fail = fail
    def market(self, symbol):
        return {'base':'BTC', 'quote':'USDT', 'limits':{'amount':{'min':0.001}, 'cost':{'min':5}}}
    def amount_to_precision(self, symbol, amount):
        return str(int(amount * 1000) / 1000)
    def fetch_ohlcv(self, *args, **kwargs):
        return [[i, 0, 0, 0, price, 0] for i, price in enumerate([10, 9, 8, 20, 999], 1)]
    def fetch_balance(self):
        return {'free':{'USDT':100, 'BTC':10}}
    def create_order(self, *args):
        self.orders.append(args)
        if self.fail:
            raise TimeoutError('uncertain submission')
        return {'status':'closed', 'filled':0.5, 'cost':10, 'fees':[]}

def args(live=False):
    return SimpleNamespace(symbol='BTC/USDT', timeframe='1m', fast=2, slow=3, quote_amount=10, fee=0.001, live=live)

def test_crossovers():
    candles=lambda values:[[0,0,0,0,v] for v in values]
    assert signal(candles([10,9,8,20]),2,3)=='buy'
    assert signal(candles([8,9,10,1]),2,3)=='sell'
    assert signal(candles([1,2,3,4]),2,3) is None

def test_minimum_and_rounding():
    e=Exchange()
    assert order_amount(e,'BTC/USDT',10,20)==0.5
    with pytest.raises(ValueError): order_amount(e,'BTC/USDT',1,20)

def test_paper_and_restart_deduplication(tmp_path):
    path=tmp_path/'state.db'
    state=State(path,['paper'],100)
    e=Exchange()
    step(e,state,args())
    assert state.data['position']==0.5
    assert state.data['cash']==pytest.approx(89.99)
    assert e.orders==[]
    state.db.close()
    state.lock.close()
    state=State(path,['paper'],100)
    step(e,state,args())
    assert state.data['position']==0.5
    assert state.data['candle']==4

def test_uncertain_order_blocks_restart(tmp_path):
    path=tmp_path/'state.db'
    state=State(path,['live'],100)
    e=Exchange(fail=True)
    with pytest.raises(TimeoutError): step(e,state,args(True))
    assert len(e.orders)==1
    state.db.close()
    state.lock.close()
    with pytest.raises(RuntimeError,match='Unresolved'): State(path,['live'],100)

def test_successful_live_bookkeeping(tmp_path):
    state=State(tmp_path/'state.db',['live'],100)
    e=Exchange()
    step(e,state,args(True))
    assert len(e.orders)==1
    assert state.data['pending'] is None
    assert state.data['position']==0.5
    assert state.data['cash']==90

def test_state_isolation(tmp_path):
    path=tmp_path/'state.db'
    state=State(path,['paper'],100)
    state.db.close()
    state.lock.close()
    with pytest.raises(ValueError): State(path,['live'],100)


def test_single_process_lock(tmp_path):
    path=tmp_path/'state.db'
    state=State(path,['paper'],100)
    with pytest.raises(RuntimeError,match='Another bot'): State(path,['paper'],100)
