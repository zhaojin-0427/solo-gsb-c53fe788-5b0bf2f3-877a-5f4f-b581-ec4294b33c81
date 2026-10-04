"""Unit tests for minimal hitting sets and witness construction."""
import pytest

from app.evaluator import Evaluator, Table
from app.provenance import (
    CounterfactualBudgetExceeded,
    minimal_deletion_sets_by_recompute,
    minimal_hitting_sets,
)


def L(table, *pk):
    return (table, pk)


def test_hitting_sets_basic():
    witnesses = {frozenset([L("t", 1), L("t", 2)]), frozenset([L("t", 2), L("t", 3)])}
    hs = minimal_hitting_sets(witnesses)
    assert frozenset([L("t", 2)]) in hs
    assert frozenset([L("t", 1), L("t", 3)]) in hs
    assert len(hs) == 2


def test_hitting_sets_nested():
    witnesses = {
        frozenset([L("t", 1), L("t", 2)]),
        frozenset([L("t", 2), L("t", 3)]),
        frozenset([L("t", 3), L("t", 4)]),
    }
    hs = minimal_hitting_sets(witnesses)
    # {2,3}, {1,3}, {2,4}
    assert len(hs) == 3
    for h in hs:
        for w in witnesses:
            assert h & w
        assert not any(o < h for o in hs)


def test_single_witness():
    w = frozenset([L("a", "x"), L("b", "y")])
    hs = minimal_hitting_sets(witnesses={w})
    assert set(hs) == {frozenset([L("a", "x")]), frozenset([L("b", "y")])}


def test_deletion_sets_by_recompute():
    pool = {L("t", 1), L("t", 2)}
    # Every non-empty set kills: the singletons are the minimal solutions.
    sols = minimal_deletion_sets_by_recompute(pool, lambda d: len(d) > 0)
    assert sols == [frozenset([L("t", 1)]), frozenset([L("t", 2)])]
    # Only pairs kill: the single pair is the unique minimal solution.
    sols = minimal_deletion_sets_by_recompute(pool, lambda d: len(d) >= 2)
    assert sols == [frozenset([L("t", 1), L("t", 2)])]
    # Non-upward-closed kills (only the exact singleton works): supersets of
    # a solution are skipped, and the solution is still reported.
    sols = minimal_deletion_sets_by_recompute(
        pool, lambda d: d == frozenset([L("t", 1)])
    )
    assert sols == [frozenset([L("t", 1)])]
    # Pools beyond the enumeration bound refuse the search with a
    # structured budget error rather than returning an approximate answer.
    big = {L("t", i) for i in range(13)}
    with pytest.raises(CounterfactualBudgetExceeded):
        minimal_deletion_sets_by_recompute(big, lambda d: True)

    # A non-solution's supersets are still evaluated: when only the pair
    # {1,2} kills (neither singleton does), the search finds it — a failing
    # singleton never suppresses its supersets.
    pool3 = {L("t", 1), L("t", 2), L("t", 3)}
    sols = minimal_deletion_sets_by_recompute(
        pool3, lambda d: d == frozenset([L("t", 1), L("t", 2)])
    )
    assert sols == [frozenset([L("t", 1), L("t", 2)])]

    # The recomputation budget itself raises instead of truncating.
    with pytest.raises(CounterfactualBudgetExceeded):
        minimal_deletion_sets_by_recompute(
            {L("t", i) for i in range(10)},
            lambda d: False,
            max_pool_size=12,
            max_evaluations=8,
        )


def make_evaluator(spec):
    return Evaluator({t["table"]: Table(t) for t in spec})


def join_query():
    return {
        "type": "join",
        "left": {"type": "scan", "table": "orders"},
        "right": {"type": "scan", "table": "customers"},
        "on": [{"left": "cust", "right": "id"}],
    }


def test_join_provenance():
    tables = [
        {
            "table": "orders",
            "primary_key": ["oid"],
            "columns": ["oid", "cust", "amt"],
            "rows": [
                {"oid": 1, "cust": 10, "amt": 5},
                {"oid": 2, "cust": 10, "amt": 7},
                {"oid": 3, "cust": 20, "amt": 9},
            ],
        },
        {
            "table": "customers",
            "primary_key": ["id"],
            "columns": ["id", "name"],
            "rows": [{"id": 10, "name": "A"}, {"id": 20, "name": "B"}],
        },
    ]
    ev = make_evaluator(tables)
    rows = ev.eval(join_query())
    # find joined row oid=1
    cols = ev.schema(join_query())
    r1 = next(r for r in rows if r["data"][cols.index("oid")] == 1)
    assert r1["witnesses"] == {frozenset([L("orders", 1), L("customers", 10)])}
    assert len(rows) == 3


def test_aggregate_group_witnesses():
    tables = [
        {
            "table": "orders",
            "primary_key": ["oid"],
            "columns": ["oid", "cust", "amt"],
            "rows": [
                {"oid": 1, "cust": 10, "amt": 5},
                {"oid": 2, "cust": 10, "amt": 7},
                {"oid": 3, "cust": 20, "amt": 9},
            ],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {"type": "scan", "table": "orders"},
        "group_by": ["cust"],
        "aggregates": [
            {"fn": "count", "column": "*", "as": "n"},
            {"fn": "sum", "column": "amt", "as": "total"},
            {"fn": "min", "column": "amt", "as": "lo"},
            {"fn": "max", "column": "amt", "as": "hi"},
        ],
    }
    ev = make_evaluator(tables)
    rows = ev.eval(plan)
    cols = ev.schema(plan)
    g10 = next(r for r in rows if r["data"][cols.index("cust")] == 10)
    # count(*)=2 covers both members; sum=12 has the single minimal combo
    # {1,2}; min=5 pins row 1 and max=7 pins row 2. The conjunction of all
    # components requires both rows.
    assert g10["witnesses"] == {frozenset([L("orders", 1), L("orders", 2)])}
    data = dict(zip(cols, g10["data"]))
    assert data == {"cust": 10, "n": 2, "total": 12, "lo": 5, "hi": 7}

    # Deleting either member changes the aggregate values, so each singleton
    # is a minimal deletion set — matching recomputation.
    hs = minimal_hitting_sets(g10["witnesses"])
    assert hs == [frozenset([L("orders", 1)]), frozenset([L("orders", 2)])]


def test_aggregate_min_max_witnesses():
    tables = [
        {
            "table": "orders",
            "primary_key": ["oid"],
            "columns": ["oid", "amt"],
            "rows": [
                {"oid": 1, "amt": 50},
                {"oid": 2, "amt": 30},
                {"oid": 3, "amt": 30},
            ],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {"type": "scan", "table": "orders"},
        "aggregates": [{"fn": "min", "column": "amt", "as": "lo"}],
    }
    ev = make_evaluator(tables)
    rows = ev.eval(plan)
    # min=30: each equal-valued row is a minimal combo on its own.
    assert rows[0]["witnesses"] == {
        frozenset([L("orders", 2)]),
        frozenset([L("orders", 3)]),
    }
    hs = minimal_hitting_sets(rows[0]["witnesses"])
    assert hs == [frozenset([L("orders", 2), L("orders", 3)])]


def test_aggregate_sum_minimal_combos():
    from app.evaluator import minimal_sum_combos

    tables = [
        {
            "table": "orders",
            "primary_key": ["oid"],
            "columns": ["oid", "amt"],
            "rows": [
                {"oid": 1, "amt": 50},
                {"oid": 2, "amt": 30},
                {"oid": 3, "amt": 0},
            ],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {"type": "scan", "table": "orders"},
        "aggregates": [{"fn": "sum", "column": "amt", "as": "total"}],
    }
    ev = make_evaluator(tables)
    rows = ev.eval(plan)
    # The zero-valued row never participates in a minimal sum combination.
    assert rows[0]["data"] == (80,)
    assert rows[0]["witnesses"] == {frozenset([L("orders", 1), L("orders", 2)])}

    # Equal-valued rows keep distinct combinations: 30a+30b and 60 both hit 60.
    items = [
        ({"id": "a"}, 30),
        ({"id": "b"}, 30),
        ({"id": "c"}, 60),
    ]
    combos = minimal_sum_combos(items, 60)
    assert {tuple(m["id"] for m in c) for c in combos} == {("a", "b"), ("c",)}
    # Negative values: only the full set sums to the target.
    items = [({"id": "a"}, 50), ({"id": "b"}, 30), ({"id": "c"}, -10)]
    combos = minimal_sum_combos(items, 70)
    assert {tuple(m["id"] for m in c) for c in combos} == {("a", "b", "c")}

    # Zero target: the empty selection is never a combination — the minimal
    # witness is the non-empty cancelling pair.
    items = [({"id": "a"}, 5), ({"id": "b"}, -5)]
    combos = minimal_sum_combos(items, 0)
    assert {tuple(m["id"] for m in c) for c in combos} == {("a", "b")}


def test_aggregate_sum_zero_total_witness():
    """A zero total from mixed signs is witnessed by the non-empty pair."""
    tables = [
        {
            "table": "t",
            "primary_key": ["id"],
            "columns": ["id", "v"],
            "rows": [
                {"id": 1, "v": 5},
                {"id": 2, "v": -5},
            ],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {"type": "scan", "table": "t"},
        "aggregates": [{"fn": "sum", "column": "v", "as": "total"}],
    }
    ev = make_evaluator(tables)
    rows = ev.eval(plan)
    assert rows[0]["data"] == (0,)
    # Not the trivial family from the empty combination: both rows pinned.
    assert rows[0]["witnesses"] == {frozenset([L("t", 1), L("t", 2)])}
    hs = minimal_hitting_sets(rows[0]["witnesses"])
    assert hs == [frozenset([L("t", 1)]), frozenset([L("t", 2)])]


def test_aggregate_null_components():
    tables = [
        {
            "table": "t",
            "primary_key": ["id"],
            "columns": ["id", "v"],
            "rows": [
                {"id": 1, "v": 50},
                {"id": 2, "v": None},
            ],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {"type": "scan", "table": "t"},
        "aggregates": [
            {"fn": "count", "column": "v", "as": "n"},
            {"fn": "sum", "column": "v", "as": "total"},
        ],
    }
    ev = make_evaluator(tables)
    rows = ev.eval(plan)
    # count(v)=1 and sum=50 ignore the NULL row: only row 1 is pinned.
    assert rows[0]["data"] == (1, 50)
    assert rows[0]["witnesses"] == {frozenset([L("t", 1)])}

    # All-NULL sum/min/max: value NULL, only group existence is pinned.
    plan_null = {
        "type": "aggregate",
        "input": {
            "type": "filter",
            "input": {"type": "scan", "table": "t"},
            "predicate": {"column": "v", "op": "is_null"},
        },
        "aggregates": [
            {"fn": "count", "column": "v", "as": "n"},
            {"fn": "sum", "column": "v", "as": "total"},
        ],
    }
    rows = ev.eval(plan_null)
    assert rows[0]["data"] == (0, None)
    assert rows[0]["witnesses"] == {frozenset([L("t", 2)])}


def test_empty_aggregate_input():
    from app.errors import ApiError

    tables = [
        {
            "table": "t",
            "primary_key": ["id"],
            "columns": ["id", "v"],
            "rows": [{"id": 1, "v": 1}],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {
            "type": "filter",
            "input": {"type": "scan", "table": "t"},
            "predicate": {"column": "v", "op": "=", "value": 99},
        },
        "aggregates": [{"fn": "count", "column": "*", "as": "n"}],
    }
    ev = make_evaluator(tables)
    try:
        ev.eval(plan)
        assert False, "expected ApiError"
    except ApiError as e:
        assert e.code == "EMPTY_AGGREGATE"
