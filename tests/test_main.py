from decimal import Decimal

from src.dragon.main import _select_stream_universe
from src.dragon.triangles import Triangle


def test_stream_universe_prefers_liquid_complete_triangles():
    triangles = [
        Triangle(("AAAUSDT", "AAABBB", "BBBUSDT"), ("USDT", "AAA", "BBB")),
        Triangle(("CCCUSDT", "CCCDDD", "DDDUSDT"), ("USDT", "CCC", "DDD")),
    ]
    volumes = {
        "AAAUSDT": Decimal("100"),
        "AAABBB": Decimal("100"),
        "BBBUSDT": Decimal("100"),
        "CCCUSDT": Decimal("1000"),
        "CCCDDD": Decimal("1000"),
        "DDDUSDT": Decimal("1000"),
    }
    chosen, symbols = _select_stream_universe(triangles, volumes, 3)
    assert chosen == [triangles[1]]
    assert symbols == ["CCCUSDT", "CCCDDD", "DDDUSDT"]


def test_stream_universe_never_splits_a_triangle():
    triangle = Triangle(("AAAUSDT", "AAABBB", "BBBUSDT"), ("USDT", "AAA", "BBB"))
    chosen, symbols = _select_stream_universe([triangle], {}, 2)
    assert chosen == [triangle]
    assert len(symbols) == 3


def test_stream_universe_zero_cap_means_full_universe():
    triangles = [
        Triangle(("AAAUSDT", "AAABBB", "BBBUSDT"), ("USDT", "AAA", "BBB")),
        Triangle(("CCCUSDT", "CCCDDD", "DDDUSDT"), ("USDT", "CCC", "DDD")),
    ]
    chosen, symbols = _select_stream_universe(triangles, {}, 0)
    assert chosen == triangles
    assert symbols == [
        "AAAUSDT", "AAABBB", "BBBUSDT",
        "CCCUSDT", "CCCDDD", "DDDUSDT",
    ]
