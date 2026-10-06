"""1번 수정 검증: 1초 이내 IEX_TRADE는 Yahoo/Nasdaq 폴링이 못 덮어쓴다."""
import sys, threading, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_feed import _write_live_price  # noqa: E402

store = {}
lock = threading.Lock()
store["TEST"] = {"price": 100.0, "source": "IEX_TRADE", "trade_ts": time.time() - 0.3}
_write_live_price(store, lock, "TEST", 98.0, 0.0, 0, "REGULAR")
assert store["TEST"]["price"] == 100.0, f"fresh IEX_TRADE가 덮어써짐: {store['TEST']}"
print("PASS: 1초 이내 IEX_TRADE 보호됨")

store["TEST"]["trade_ts"] = time.time() - 1.2
_write_live_price(store, lock, "TEST", 98.0, 0.0, 0, "REGULAR")
assert store["TEST"]["price"] == 98.0, f"만료된 IEX_TRADE인데 안 덮어써짐: {store['TEST']}"
print("PASS: 1초 경과 후 정상 갱신됨")
