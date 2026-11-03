from fraudradar.generator import TransactionGenerator
from fraudradar.schemas import Transaction


def test_payment_details_survive_the_broker():
    tx = TransactionGenerator(seed=2).legit(1000)
    assert tx.kind is not None and tx.src_balance is not None and tx.dst_balance is not None
    assert Transaction.from_bytes(tx.to_bytes()) == tx


def test_messages_without_payment_details_still_parse():
    old = b'{"tx_id":"t","ts":1,"src":"A","dst":"B","amount":5.0,"src_city":0,"dst_city":1,"device":"D","src_age_days":9.0}'
    tx = Transaction.from_bytes(old)
    assert tx.kind is None and tx.src_balance is None and tx.dst_merchant is False


def test_legit_senders_can_afford_what_they_send():
    gen = TransactionGenerator(seed=1)
    for i in range(3000):
        tx = gen.legit(i)
        assert tx.src_balance >= tx.amount > 0
        assert (tx.kind == "PAYMENT") == tx.dst_merchant
        assert tx.dst_balance == 0.0 or not tx.dst_merchant


def test_every_ring_account_ends_by_sweeping_itself_empty():
    txs = TransactionGenerator(seed=1).ring(0, size=14)
    by_account: dict[str, list[Transaction]] = {}
    for t in txs:
        by_account.setdefault(t.src, []).append(t)
    assert len(by_account) == 14
    for payments in by_account.values():
        last = payments[-1]
        assert last.amount == last.src_balance and last.kind == "TRANSFER"
        assert all(p.amount < p.src_balance for p in payments[:-1])
    assert txs[0].dst_balance == 0.0  # the mule starts empty
    pooled = 0.0
    for t in txs:  # and holds, before each payment, everything sent to it earlier
        assert t.dst_balance == pooled
        pooled = round(pooled + t.amount, 2)
