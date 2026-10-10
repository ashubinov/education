"""Оценка отдачи (RTP) и частоты выигрышей слота методом Монте-Карло: python scripts/slots_rtp.py [число_вращений]
Цель настройки — RTP около 94–96 %, выигрыш примерно в каждом четвёртом вращении."""
import collections
import os
import pathlib
import random
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.environ.setdefault("LQ_DB", os.path.join(tempfile.mkdtemp(), "rtp.db"))
sys.path.insert(0, str(ROOT))
from server import slots  # noqa: E402

n = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
bet = 10
rng = random.Random(1)
paid, hits = 0, 0
tiers, parts = collections.Counter(), collections.Counter()
biggest = 0
for _ in range(n):
    grid, ev = slots.play(bet, rng)
    p = ev["payout"]
    paid += p
    hits += p > 0
    tiers[ev["tier"]] += 1
    parts["lines"] += sum(w["pay"] for w in ev["lines"])
    parts["scatter"] += ev["scatter"]["pay"] if ev["scatter"] else 0
    parts["bonus"] += ev["bonus"]["prize"] if ev["bonus"] else 0
    biggest = max(biggest, p)
print(f"вращений: {n:,}, RTP: {paid / (n * bet):.4f}, частота выигрыша: {hits / n:.3f}, максимум: {biggest / bet:.0f}× ставки")
print("доля RTP: линии %.3f, scatter %.3f, bonus %.3f" % tuple(parts[k] / (n * bet) for k in ("lines", "scatter", "bonus")))
print("категории:", {k: f"{v / n:.4f}" for k, v in tiers.items()})
