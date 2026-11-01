from fraudradar.features import FLAG_TTL_MS, FeatureStore
from fraudradar.schemas import Transaction


def tx(src="A1", dst="M1", amount=50.0, ts=1_000_000, device="D1", age=400.0) -> Transaction:
    return Transaction(
        tx_id=f"{src}-{ts}", ts=ts, src=src, dst=dst, amount=amount,
        src_city=0, dst_city=1, device=device, src_age_days=age,
    )


def test_features_are_read_only_and_exclude_the_current_transaction():
    store = FeatureStore()
    t = tx()
    before = store.features(t)
    assert store.features(t) == before  # reading twice changes nothing
    assert before.velocity == 0  # the transaction has not happened yet
    store.update(t)
    assert store.features(tx(ts=t.ts + 1)).velocity == 1


def test_amount_z_compares_with_the_accounts_own_history():
    store = FeatureStore()
    for i in range(10):
        store.update(tx(amount=50.0 + i, ts=1_000_000 + i * 1000))
    typical = store.features(tx(amount=55.0, ts=1_020_000)).amount_z
    huge = store.features(tx(amount=5000.0, ts=1_020_000)).amount_z
    assert huge > typical + 3


def test_shared_device_counts_other_accounts_only():
    store = FeatureStore()
    store.update(tx(src="A1", device="D9"))
    store.update(tx(src="A2", device="D9", ts=1_000_100))
    assert store.features(tx(src="A1", device="D9", ts=1_000_200)).shared_device == 1
    assert store.features(tx(src="A3", device="D9", ts=1_000_200)).shared_device == 2


def test_young_fan_in_counts_distinct_young_senders_inside_the_window():
    store = FeatureStore()
    for i, sender in enumerate(["R1", "R2", "R3", "R1"]):
        store.update(tx(src=sender, dst="MULE", age=1.0, ts=1_000_000 + i * 100))
    store.update(tx(src="OLD", dst="MULE", age=900.0, ts=1_000_500))  # old senders do not count
    assert store.features(tx(src="R9", dst="MULE", age=1.0, ts=1_001_000)).young_fan_in == 3
    # outside the 60s window nothing remains
    assert store.features(tx(src="R9", dst="MULE", age=1.0, ts=1_070_000)).young_fan_in == 0


def test_flag_links_device_mates_and_expires():
    store = FeatureStore()
    store.update(tx(src="BAD", device="D7", ts=1_000_000))
    store.flag("BAD", 1_000_000)
    probe = tx(src="OTHER", device="D7", ts=1_000_500)
    assert store.features(probe).flagged_linked == 1
    late = tx(src="OTHER", device="D7", ts=1_000_000 + FLAG_TTL_MS + 1)
    assert store.features(late).flagged_linked == 0
