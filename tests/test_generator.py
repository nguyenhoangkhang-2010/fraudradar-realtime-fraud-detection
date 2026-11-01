from fraudradar.features import YOUNG_DAYS
from fraudradar.generator import TransactionGenerator


def test_generator_is_deterministic_for_a_seed():
    a = TransactionGenerator(seed=3).legit(1000)
    b = TransactionGenerator(seed=3).legit(1000)
    assert a == b


def test_legit_traffic_is_unlabeled_and_mostly_established():
    gen = TransactionGenerator(seed=1)
    txs = [gen.legit(i) for i in range(2000)]
    assert all(t.truth is None for t in txs)
    young = sum(t.src_age_days < YOUNG_DAYS for t in txs) / len(txs)
    assert 0.0 < young < 0.10  # some honest new accounts exist, but they are rare


def test_ring_shape():
    gen = TransactionGenerator(seed=1)
    start = 5_000_000
    txs = gen.ring(start, size=14, duration_ms=6000)
    assert {t.truth for t in txs} == {"ring-001"}
    assert len({t.src for t in txs}) == 14
    assert len({t.dst for t in txs}) == 1  # one mule
    assert len({t.device for t in txs}) < 14  # devices are shared
    assert all(t.src_age_days < YOUNG_DAYS for t in txs)
    assert all(start <= t.ts < start + 6000 for t in txs)
    assert [t.ts for t in txs] == sorted(t.ts for t in txs)


def test_second_ring_gets_a_new_id():
    gen = TransactionGenerator(seed=1)
    gen.ring(0)
    assert gen.ring(0)[0].truth == "ring-002"
