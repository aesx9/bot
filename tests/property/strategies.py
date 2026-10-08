from __future__ import annotations

from decimal import Decimal

from hypothesis import strategies as st

SYMBOLS = ("A", "B", "C", "D")

sizes = st.decimals(min_value=Decimal("-1000"), max_value=Decimal("1000"), places=4,
                    allow_nan=False, allow_infinity=False)
nonzero = sizes.filter(lambda d: d != 0)
prices = st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("100000"), places=4,
                     allow_nan=False, allow_infinity=False)
positions = st.dictionaries(st.sampled_from(SYMBOLS), nonzero, max_size=len(SYMBOLS))
price_maps = st.fixed_dictionaries({s: prices for s in SYMBOLS})

# Pasos reales de Kraken: contractValueTradePrecision entre -3 y 4
STEPS = tuple(Decimal(10) ** -p for p in range(-3, 5))
