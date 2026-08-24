from src.book import OrderBook


def test_from_kalshi_and_derived_asks():
    book = OrderBook.from_kalshi({"orderbook": {
        "yes": [[44, 50], [45, 100]],       # unsorted on purpose
        "no": [[50, 40], [52, 60]],
    }})
    assert book.yes_bids == [(45, 100), (44, 50)]
    assert book.no_bids == [(52, 60), (50, 40)]
    # YES asks derive from NO bids: 100-52=48 (60), 100-50=50 (40)
    assert book.asks("yes") == [(48, 60), (50, 40)]
    assert book.best_ask("yes") == 48
    assert book.best_bid("yes") == 45
    assert book.asks("no") == [(55, 100), (56, 50)]


def test_walk_buy_through_levels():
    book = OrderBook(yes_bids=[], no_bids=[[52, 60], [50, 40]])
    fill = book.walk_buy("yes", 80)
    assert fill.contracts == 80
    assert fill.levels == [(48, 60), (50, 20)]
    assert fill.cost_cents == 48 * 60 + 50 * 20
    assert abs(fill.avg_price_cents - 48.5) < 1e-9
    assert fill.worst_price_cents == 50


def test_walk_partial_fill_when_thin():
    book = OrderBook(yes_bids=[], no_bids=[[52, 60]])
    fill = book.walk_buy("yes", 500)
    assert fill.contracts == 60


def test_walk_sell_uses_bids():
    book = OrderBook(yes_bids=[[45, 30], [44, 30]], no_bids=[])
    fill = book.walk_sell("yes", 40)
    assert fill.contracts == 40
    assert fill.levels == [(45, 30), (44, 10)]


def test_empty_book():
    book = OrderBook()
    assert book.best_ask("yes") is None
    assert book.walk_buy("yes", 10).contracts == 0
    assert book.depth_contracts("yes", "buy") == 0


def test_size_at_or_better():
    book = OrderBook(yes_bids=[[45, 30], [44, 30]], no_bids=[[52, 60], [50, 40]])
    assert book.size_at_or_better("yes", "buy", 48) == 60
    assert book.size_at_or_better("yes", "buy", 50) == 100
    assert book.size_at_or_better("yes", "sell", 45) == 30
