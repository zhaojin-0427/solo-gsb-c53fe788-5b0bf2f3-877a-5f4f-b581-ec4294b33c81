"""HTTP-level tests using FastAPI TestClient."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["PROVENANCE_DB"] = tmp.name
    # Import after env is set so the store picks up the temp DB.
    import importlib

    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    main.store.close()
    os.unlink(tmp.name)


BASE_REQUEST = {
    "idempotency_key": "key-join-1",
    "tables": [
        {
            "table": "orders",
            "primary_key": ["oid"],
            "columns": [
                {"name": "oid", "type": "int"},
                {"name": "cust", "type": "int"},
                {"name": "amt", "type": "int"},
            ],
            "rows": [
                {"oid": 1, "cust": 10, "amt": 5},
                {"oid": 2, "cust": 10, "amt": 7},
                {"oid": 3, "cust": 20, "amt": 9},
            ],
        },
        {
            "table": "customers",
            "primary_key": ["id"],
            "columns": [
                {"name": "id", "type": "int"},
                {"name": "name", "type": "string"},
            ],
            "rows": [{"id": 10, "name": "A"}, {"id": 20, "name": "B"}],
        },
    ],
    "plan": {
        "type": "join",
        "left": {"type": "scan", "table": "orders"},
        "right": {"type": "scan", "table": "customers"},
        "on": [{"left": "cust", "right": "id"}],
    },
}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.content == b'{"status":"ok"}'


def test_join_query_and_counterfactual(client):
    req = dict(BASE_REQUEST)
    req["explain"] = {"row": {"cust": 10, "oid": 1, "amt": 5, "id": 10, "name": "A"}}
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["row_count"] == 3
    cf = body["counterfactual"]
    # The joined output oid=1 has exactly one minimal witness, hence two
    # singleton minimal deletion sets.
    assert cf["minimal_deletion_count"] == 2
    deleted = {tuple(sorted(d)) for d in cf["minimal_deletion_sets"]}
    assert deleted == {("customers[10]",), ("orders[1]",)}
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False
        # deleting customer 10 removes both oid=1,2 rows; deleting order 1 only one
        if sc["delete"] == ["customers[10]"]:
            assert sc["result"]["row_count"] == 1
        else:
            assert sc["result"]["row_count"] == 2


def test_idempotent_replay_byte_identical(client):
    req = {**BASE_REQUEST, "idempotency_key": "key-replay-1"}
    r1 = client.post("/query", json=req)
    r2 = client.post("/query", json=req)
    assert r1.status_code == 200
    assert r1.content == r2.content
    assert r2.headers["x-idempotent-replay"] == "true"


def test_idempotency_conflict(client):
    modified = dict(BASE_REQUEST)
    modified["idempotency_key"] = "key-conflict-1"
    r1 = client.post("/query", json=modified)
    assert r1.status_code == 200
    modified = {
        **modified,
        "tables": [
            {
                **BASE_REQUEST["tables"][0],
                "rows": BASE_REQUEST["tables"][0]["rows"]
                + [{"oid": 4, "cust": 20, "amt": 1}],
            },
            BASE_REQUEST["tables"][1],
        ],
    }
    r2 = client.post("/query", json=modified)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_key_order_invariant(client):
    base = {**BASE_REQUEST, "idempotency_key": "key-shuffle-a"}
    r1 = client.post("/query", json=base)
    # same content, shuffled key order in a table row
    shuffled = {
        **BASE_REQUEST,
        "idempotency_key": "key-shuffle-b",
        "tables": [
            {
                **BASE_REQUEST["tables"][0],
                "rows": [
                    {"amt": 5, "cust": 10, "oid": 1},
                    {"oid": 2, "cust": 10, "amt": 7},
                    {"oid": 3, "cust": 20, "amt": 9},
                ],
            },
            BASE_REQUEST["tables"][1],
        ],
    }
    r2 = client.post("/query", json=shuffled)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == r2.content


def test_duplicate_primary_key(client):
    bad = {
        "idempotency_key": "key-dup-pk",
        "tables": [
            {
                "table": "t",
                "primary_key": ["id"],
                "columns": ["id"],
                "rows": [{"id": 1}, {"id": 1}],
            }
        ],
        "plan": {"type": "scan", "table": "t"},
    }
    r = client.post("/query", json=bad)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "DUPLICATE_PRIMARY_KEY"


def test_illegal_reference(client):
    bad = {
        "idempotency_key": "key-bad-ref",
        "tables": [
            {"table": "t", "primary_key": ["id"], "columns": ["id", "v"],
             "rows": [{"id": 1, "v": 1}]}
        ],
        "plan": {
            "type": "filter",
            "input": {"type": "scan", "table": "t"},
            "predicate": {"column": "nope", "op": "=", "value": 1},
        },
    }
    r = client.post("/query", json=bad)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ILLEGAL_REFERENCE"


def test_unknown_table(client):
    bad = {
        "idempotency_key": "key-bad-table",
        "tables": [
            {"table": "t", "primary_key": ["id"], "columns": ["id"],
             "rows": [{"id": 1}]}
        ],
        "plan": {"type": "scan", "table": "other"},
    }
    r = client.post("/query", json=bad)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNKNOWN_TABLE"


def test_sum_type_error(client):
    bad = {
        "idempotency_key": "key-sum-type",
        "tables": [
            {"table": "t", "primary_key": ["id"], "columns": ["id", "v"],
             "rows": [{"id": 1, "v": "x"}]}
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "t"},
            "aggregates": [{"fn": "sum", "column": "v", "as": "s"}],
        },
    }
    r = client.post("/query", json=bad)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "TYPE_ERROR"


def test_null_filter_3vl(client):
    req = {
        "idempotency_key": "key-null-3vl",
        "tables": [
            {
                "table": "t",
                "primary_key": ["id"],
                "columns": ["id", "v"],
                "rows": [{"id": 1, "v": 5}, {"id": 2, "v": None}, {"id": 3, "v": 9}],
            }
        ],
        "plan": {
            "type": "filter",
            "input": {"type": "scan", "table": "t"},
            "predicate": {"column": "v", "op": "<>", "value": 5},
        },
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200
    rows = r.json()["result"]["rows"]
    assert [x["row"]["id"] for x in rows] == [3]


def test_aggregate_counterfactual_group_deletion(client):
    req = {
        "idempotency_key": "key-agg-cf",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "cid", "amount"],
                "rows": [
                    {"oid": 1, "cid": 10, "amount": 5},
                    {"oid": 2, "cid": 10, "amount": 7},
                    {"oid": 3, "cid": 20, "amount": 9},
                ],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "group_by": ["cid"],
            "aggregates": [{"fn": "sum", "column": "amount", "as": "total"}],
        },
        "explain": {"row": {"cid": 10, "total": 12}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    cf = r.json()["counterfactual"]
    # sum=12 needs both members: one conjunctive witness, two singleton
    # minimal deletion sets — deleting either row changes the total.
    assert cf["target_provenance"]["dnf"] == "(s1 ∧ s2)"
    assert cf["minimal_deletion_sets"] == [["orders[1]"], ["orders[2]"]]
    for scenario in cf["scenarios"]:
        assert scenario["target_present"] is False
        assert scenario["result"]["row_count"] == 2
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    assert by_delete[("orders[1]",)]["result"]["rows"] == [
        {"cid": 10, "total": 7},
        {"cid": 20, "total": 9},
    ]
    assert by_delete[("orders[2]",)]["result"]["rows"] == [
        {"cid": 10, "total": 5},
        {"cid": 20, "total": 9},
    ]


def test_aggregate_count_sum_counterfactual(client):
    """The 50/30 case: count=2 and sum=80 must be one conjunctive witness."""
    req = {
        "idempotency_key": "key-agg-50-30",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "cid", "amount"],
                "rows": [
                    {"oid": 1, "cid": 10, "amount": 50},
                    {"oid": 2, "cid": 10, "amount": 30},
                    {"oid": 3, "cid": 20, "amount": 10},
                ],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "group_by": ["cid"],
            "aggregates": [
                {"fn": "count", "column": "*", "as": "n"},
                {"fn": "sum", "column": "amount", "as": "total"},
            ],
        },
        "explain": {"row": {"cid": 10, "n": 2, "total": 80}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["row_count"] == 2
    target = next(x for x in body["result"]["rows"] if x["row"]["cid"] == 10)
    assert target["witness_count"] == 1
    assert target["provenance"]["dnf"] == "(s1 ∧ s2)"
    assert target["minimal_witnesses"][0]["labels"] == ["orders[1]", "orders[2]"]
    # The other group keeps its own single-row witness.
    other = next(x for x in body["result"]["rows"] if x["row"]["cid"] == 20)
    assert other["provenance"]["dnf"] == "(s3)"

    cf = body["counterfactual"]
    assert cf["minimal_deletion_sets"] == [["orders[1]"], ["orders[2]"]]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False
    # Recomputation agrees: removing either row changes count and sum.
    assert by_delete[("orders[1]",)]["result"]["rows"] == [
        {"cid": 10, "n": 1, "total": 30},
        {"cid": 20, "n": 1, "total": 10},
    ]
    assert by_delete[("orders[2]",)]["result"]["rows"] == [
        {"cid": 10, "n": 1, "total": 50},
        {"cid": 20, "n": 1, "total": 10},
    ]


def test_aggregate_sum_equal_value_combos(client):
    """Equal-valued rows keep distinct minimal sum combinations."""
    req = {
        "idempotency_key": "key-agg-equal-combos",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "amount"],
                "rows": [
                    {"oid": 1, "amount": 30},
                    {"oid": 2, "amount": 30},
                    {"oid": 3, "amount": 60},
                    {"oid": 4, "amount": -60},
                ],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "aggregates": [{"fn": "sum", "column": "amount", "as": "total"}],
        },
        "explain": {"row": {"total": 60}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    row = body["result"]["rows"][0]
    # Minimal combinations summing to 60: {orders[1], orders[2]} (30+30) and
    # {orders[3]} (60). Both equal-valued 30-rows stay in their combination.
    assert row["provenance"]["dnf"] == "(s1 ∧ s2) ∨ (s3)"
    assert row["provenance"]["dnf_structured"] == [["s1", "s2"], ["s3"]]
    cf = body["counterfactual"]
    # Mixed positive/negative values make the sum non-monotone: deleting ANY
    # single row changes the total (removing 30 leaves 30, removing 60
    # leaves 0, removing -60 leaves 120), so every singleton is a minimal
    # deletion set — decided by recomputation, not by hitting sets.
    assert cf["minimal_deletion_sets"] == [
        ["orders[1]"],
        ["orders[2]"],
        ["orders[3]"],
        ["orders[4]"],
    ]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    assert by_delete[("orders[1]",)]["result"]["rows"] == [{"total": 30}]
    assert by_delete[("orders[2]",)]["result"]["rows"] == [{"total": 30}]
    assert by_delete[("orders[3]",)]["result"]["rows"] == [{"total": 0}]
    assert by_delete[("orders[4]",)]["result"]["rows"] == [{"total": 120}]
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False


def test_sum_mixed_signs_zero_total(client):
    """5 and -5 in one group, sum=0: the empty combination is not a witness,
    and deleting either single row (not both) removes the target."""
    req = {
        "idempotency_key": "key-agg-mixed-zero",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "cid", "amount"],
                "rows": [
                    {"oid": 1, "cid": 10, "amount": 5},
                    {"oid": 2, "cid": 10, "amount": -5},
                    {"oid": 3, "cid": 20, "amount": 7},
                ],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "group_by": ["cid"],
            "aggregates": [{"fn": "sum", "column": "amount", "as": "total"}],
        },
        "explain": {"row": {"cid": 10, "total": 0}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    target = next(x for x in body["result"]["rows"] if x["row"]["cid"] == 10)
    # The zero total is witnessed by the non-empty pair, not the empty set.
    assert target["provenance"]["dnf"] == "(s1 ∧ s2)"
    assert target["minimal_witnesses"][0]["labels"] == ["orders[1]", "orders[2]"]
    # The other group keeps its own single-row witness.
    other = next(x for x in body["result"]["rows"] if x["row"]["cid"] == 20)
    assert other["provenance"]["dnf"] == "(s3)"
    cf = body["counterfactual"]
    # Recomputation shows deleting either single row changes the total to
    # ±5, so the minimal deletion sets are the two singletons — not the
    # two-row set the (buggy) empty-witness hitting sets reported.
    assert cf["minimal_deletion_sets"] == [["orders[1]"], ["orders[2]"]]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    assert by_delete[("orders[1]",)]["result"]["rows"] == [
        {"cid": 10, "total": -5},
        {"cid": 20, "total": 7},
    ]
    assert by_delete[("orders[2]",)]["result"]["rows"] == [
        {"cid": 10, "total": 5},
        {"cid": 20, "total": 7},
    ]
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False


def test_sum_mixed_signs_nonmonotone_deletions(client):
    """5, 2, -2 (sum=5): deleting ANY single row changes the total, so the
    minimal deletion sets include the rows that appear in no minimal sum
    combination (the 2 and the -2)."""
    req = {
        "idempotency_key": "key-agg-mixed-nonmono",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "amount"],
                "rows": [
                    {"oid": 1, "amount": 5},
                    {"oid": 2, "amount": 2},
                    {"oid": 3, "amount": -2},
                ],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "aggregates": [{"fn": "sum", "column": "amount", "as": "total"}],
        },
        "explain": {"row": {"total": 5}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    row = body["result"]["rows"][0]
    # Only {orders[1]} sums to 5 minimally: the witness stays a singleton.
    assert row["provenance"]["dnf"] == "(s1)"
    cf = body["counterfactual"]
    # But the sum is non-monotone: removing the 2 or the -2 changes the
    # total to 3 or 7, so those singletons are minimal deletion sets too.
    assert cf["minimal_deletion_sets"] == [
        ["orders[1]"],
        ["orders[2]"],
        ["orders[3]"],
    ]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    assert by_delete[("orders[1]",)]["result"]["rows"] == [{"total": 0}]
    assert by_delete[("orders[2]",)]["result"]["rows"] == [{"total": 3}]
    assert by_delete[("orders[3]",)]["result"]["rows"] == [{"total": 7}]
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False


def test_aggregate_min_max_null_counterfactual(client):
    req = {
        "idempotency_key": "key-agg-min-null",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "amount"],
                "rows": [
                    {"oid": 1, "amount": 50},
                    {"oid": 2, "amount": 30},
                    {"oid": 3, "amount": None},
                ],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "aggregates": [{"fn": "min", "column": "amount", "as": "lo"}],
        },
        "explain": {"row": {"lo": 30}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    row = body["result"]["rows"][0]
    # min=30 is produced by orders[2] alone; the NULL row pins nothing and
    # never enters the symbol table.
    assert row["provenance"]["dnf"] == "(s1)"
    assert row["minimal_witnesses"][0]["labels"] == ["orders[2]"]
    assert body["symbol_table"] == [{"source": "orders[2]", "symbol": "s1"}]
    cf = body["counterfactual"]
    assert cf["minimal_deletion_sets"] == [["orders[2]"]]
    sc = cf["scenarios"][0]
    assert sc["target_present"] is False
    assert sc["result"]["rows"] == [{"lo": 50}]


def test_counterfactual_recompute_empty_aggregate(client):
    req = {
        "idempotency_key": "key-cf-empty-agg",
        "tables": [
            {"table": "t", "primary_key": ["id"], "columns": ["id", "v"],
             "rows": [{"id": 1, "v": 5}]}
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "t"},
            "aggregates": [{"fn": "count", "column": "*", "as": "n"}],
        },
        "explain": {"row": {"n": 1}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200
    sc = r.json()["counterfactual"]["scenarios"][0]
    assert sc["target_present"] is False
    assert sc["recompute_error"]["code"] == "EMPTY_AGGREGATE"


def test_sum_mixed_signs_nonmonotone_reappearance_pair(client):
    """5, 2, -2 (sum=5): deleting {2,-2} brings the total back to 5, so the
    target REAPPEARS. The pair must be evaluated (it is not pruned as a
    superset of any non-solution) yet must not be reported — only the three
    singletons remain minimal deletion sets."""
    req = {
        "idempotency_key": "key-agg-mixed-reappear",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "amount"],
                "rows": [
                    {"oid": 1, "amount": 5},
                    {"oid": 2, "amount": 2},
                    {"oid": 3, "amount": -2},
                ],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "aggregates": [{"fn": "sum", "column": "amount", "as": "total"}],
        },
        "explain": {"row": {"total": 5}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    cf = r.json()["counterfactual"]
    assert cf["minimal_deletion_sets"] == [
        ["orders[1]"],
        ["orders[2]"],
        ["orders[3]"],
    ]
    # Only minimal (singleton) deletions get scenarios; the reappearing pair
    # is not among them.
    assert all(len(s["delete"]) == 1 for s in cf["scenarios"])


def test_sum_mixed_signs_with_ten_zero_rows_reduced_pool(client):
    """The reported bug: one group with amounts 5, 2, -2 plus ten 0 rows,
    target total=5. The pool is 13 rows — over the 12-row cap — but the ten
    zero rows are provably irrelevant to any inclusion-minimal deletion set,
    so the pool is reduced to 3 and all three singleton deletion sets are
    returned in label order, with recomputed totals 0, 3, 7."""
    rows = [
        {"oid": 1, "cid": 10, "amount": 5},
        {"oid": 2, "cid": 10, "amount": 2},
        {"oid": 3, "cid": 10, "amount": -2},
    ]
    rows += [{"oid": 100 + i, "cid": 10, "amount": 0} for i in range(10)]
    req = {
        "idempotency_key": "key-agg-mixed-ten-zeros",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "cid", "amount"],
                "rows": rows,
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "group_by": ["cid"],
            "aggregates": [{"fn": "sum", "column": "amount", "as": "total"}],
        },
        "explain": {"row": {"cid": 10, "total": 5}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    cf = r.json()["counterfactual"]
    assert cf["minimal_deletion_count"] == 3
    assert cf["minimal_deletion_sets"] == [
        ["orders[1]"],
        ["orders[2]"],
        ["orders[3]"],
    ]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    # scenarios follow the same label order
    assert [tuple(sc["delete"]) for sc in cf["scenarios"]] == [
        ("orders[1]",),
        ("orders[2]",),
        ("orders[3]",),
    ]
    assert by_delete[("orders[1]",)]["result"]["rows"] == [{"cid": 10, "total": 0}]
    assert by_delete[("orders[2]",)]["result"]["rows"] == [{"cid": 10, "total": 3}]
    assert by_delete[("orders[3]",)]["result"]["rows"] == [{"cid": 10, "total": 7}]
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False
    # The zero rows never enter the counterfactual scenarios.
    deleted_labels = {d[0] for sc in cf["scenarios"] for d in sc["delete"]}
    assert not any(l.startswith("orders[1") for l in deleted_labels)

    # Idempotent replay of the same request is byte-identical.
    r2 = client.post("/query", json=req)
    assert r2.status_code == 200
    assert r2.content == r.content
    assert r2.headers["x-idempotent-replay"] == "true"


def test_sum_mixed_signs_budget_exceeded_structured_error(client):
    """A 13-row mixed-sign group with no reducible rows (every row also
    feeds count(*)) exceeds the pool cap: the API must fail with a
    structured error, never with an approximate hitting-set response."""
    rows = [
        {"oid": 1, "amount": 5},
        {"oid": 2, "amount": -5},
    ]
    rows += [{"oid": 10 + i, "amount": 0} for i in range(11)]
    req = {
        "idempotency_key": "key-agg-mixed-budget",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "amount"],
                "rows": rows,
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "aggregates": [
                {"fn": "count", "column": "*", "as": "n"},
                {"fn": "sum", "column": "amount", "as": "total"},
            ],
        },
        "explain": {"row": {"n": 13, "total": 0}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "COUNTERFACTUAL_BUDGET_EXCEEDED"
    assert err["details"]["pool_size"] == 13
    assert err["details"]["reduced_pool_size"] == 13
    assert err["details"]["max_pool_size"] == 12
    # No approximate success payload accompanies the error.
    assert "counterfactual" not in r.json()

    # Budget errors are pinned to the idempotency key and replay verbatim.
    r2 = client.post("/query", json=req)
    assert r2.status_code == 422
    assert r2.content == r.content
    assert r2.headers["x-idempotent-replay"] == "true"


def test_sum_mixed_signs_count_null_rows_reduced(client):
    """With count(col), NULL members are ignored by the count and the sum, so
    they reduce out even though count(*) would pin them: 5, 2, -2 plus ten
    NULL rows must still yield the three singletons."""
    rows = [
        {"oid": 1, "amount": 5},
        {"oid": 2, "amount": 2},
        {"oid": 3, "amount": -2},
    ]
    rows += [{"oid": 100 + i, "amount": None} for i in range(10)]
    req = {
        "idempotency_key": "key-agg-mixed-count-null",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "amount"],
                "rows": rows,
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "aggregates": [
                {"fn": "count", "column": "amount", "as": "n"},
                {"fn": "sum", "column": "amount", "as": "total"},
            ],
        },
        "explain": {"row": {"n": 3, "total": 5}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    cf = r.json()["counterfactual"]
    assert cf["minimal_deletion_sets"] == [
        ["orders[1]"],
        ["orders[2]"],
        ["orders[3]"],
    ]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    assert by_delete[("orders[1]",)]["result"]["rows"] == [{"n": 2, "total": 0}]
    assert by_delete[("orders[2]",)]["result"]["rows"] == [{"n": 2, "total": 3}]
    assert by_delete[("orders[3]",)]["result"]["rows"] == [{"n": 2, "total": 7}]


def test_sum_mixed_signs_with_min_still_reduces_zeros(client):
    """A simultaneous min aggregate does not pin zero rows: in a mixed-sign
    group the minimum is always negative (the maximum positive), so zero
    rows attain neither extremum and still reduce out. 5, -2 plus eleven
    zeros, target (total=3, lo=-2): the pool reduces to the two non-zero
    rows."""
    rows = [
        {"oid": 1, "amount": 5},
        {"oid": 2, "amount": -2},
    ]
    rows += [{"oid": 100 + i, "amount": 0} for i in range(11)]
    req = {
        "idempotency_key": "key-agg-mixed-minmax-hold",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "amount"],
                "rows": rows,
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "aggregates": [
                {"fn": "sum", "column": "amount", "as": "total"},
                {"fn": "min", "column": "amount", "as": "lo"},
            ],
        },
        "explain": {"row": {"total": 3, "lo": -2}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    cf = r.json()["counterfactual"]
    # lo=-2 pins only orders[2]; the eleven zero rows attain neither -2 nor
    # a maximum, so they reduce out; orders[1] (5) and orders[2] (-2) are
    # both singleton kills for the mixed sum.
    assert cf["minimal_deletion_sets"] == [["orders[1]"], ["orders[2]"]]


def test_error_response_is_canonical_and_cached(client):
    req = {
        "idempotency_key": "key-err-cached",
        "tables": [
            {"table": "t", "primary_key": ["id"], "columns": ["id"],
             "rows": [{"id": 1}, {"id": 1}]}
        ],
        "plan": {"type": "scan", "table": "t"},
    }
    r1 = client.post("/query", json=req)
    r2 = client.post("/query", json=req)
    assert r1.status_code == 400
    assert r1.content == r2.content
    assert r2.headers["x-idempotent-replay"] == "true"
