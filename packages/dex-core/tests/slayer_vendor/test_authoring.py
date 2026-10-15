"""Authoring SLayer models through plan/apply (#599).

What `semantic slayer define|update|plan` promises: whole configured model files
in, the set they would leave validated with SLayer's own model, a reviewable
plan stored with no warehouse connection and no SLayer storage opened, every
broken set refused naming the file and the element, a write surface of exactly
`semantic.slayer.models`, and `transform apply` writing the authored bytes or
nothing at all.

Needs the real `[slayer]` extra, so it skips without it; the `slayer-sqlglot` CI
job is where it runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from exmergo_dex_core.slayer import dependency
from exmergo_dex_core.slayer.dependency import SLAYER_VERSION

if dependency.installed_version() != SLAYER_VERSION:
    pytest.skip(
        f"the [slayer] extra (SLayer {SLAYER_VERSION}) is not installed",
        allow_module_level=True,
    )

from exmergo_dex_core.adapters.project import EditableProject
from exmergo_dex_core.cli import main
from exmergo_dex_core.edits import EditOp, SemanticEditTarget, content_hash
from exmergo_dex_core.edits_conformance import SemanticEditTargetContract
from exmergo_dex_core.semantic_source import SemanticSourceContext
from exmergo_dex_core.slayer.loader import FORMAT_VERSION
from exmergo_dex_core.slayer.project import SlayerSemanticLayer
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

BASE = "semantics/slayer"
ORDERS = "models/wh/orders.yaml"
CUSTOMERS = "models/wh/customers.yaml"


def model(name: str, *, data_source: str = "wh", **fields: Any) -> dict[str, Any]:
    return {
        "version": FORMAT_VERSION,
        "name": name,
        "sql_table": f"demo.main.{name}",
        "data_source": data_source,
        **fields,
    }


def orders(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "default_time_dimension": "ordered_at",
        "columns": [
            {"name": "order_id", "type": "INT", "primary_key": True},
            {"name": "customer_id", "type": "INT"},
            {"name": "amount", "type": "DOUBLE"},
            {"name": "ordered_at", "type": "TIMESTAMP"},
        ],
        "measures": [{"name": "revenue", "formula": "sum(amount)"}],
        "joins": [
            {
                "target_model": "customers",
                "join_pairs": [["customer_id", "id"]],
                "cardinality": "many_to_one",
            }
        ],
    }
    fields.update(overrides)
    return model("orders", **fields)


def customers(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "columns": [
            {"name": "id", "type": "INT", "primary_key": True},
            {"name": "region"},
        ]
    }
    fields.update(overrides)
    return model("customers", **fields)


def dump(entry: dict[str, Any], comment: str = "") -> str:
    return comment + yaml.safe_dump(entry, sort_keys=False)


def put(root: Path, name: str, text: str) -> Path:
    path = root / BASE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def read(root: Path, name: str) -> str:
    return (root / BASE / name).read_bytes().decode("utf-8")


def configure(root: Path, models: list[str], vendor: str = "slayer") -> None:
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nsemantic:\n  vendor: {vendor}\n  slayer:\n"
        f"    base_dir: {BASE}\n    models:\n"
        + "".join(f"      - {name}\n" for name in models),
        encoding="utf-8",
    )


def repo(root: Path, models: list[str], **files: str) -> Path:
    """Configure ``models`` and write ``files`` (keyed by attribute-safe name)."""

    configure(root, models)
    for name, text in files.items():
        put(root, name, text)
    return root


def payload(root: Path, edits: dict[str, str]) -> str:
    path = root / "edits.json"
    path.write_text(
        json.dumps({"edits": [{"path": p, "content": c} for p, c in edits.items()]}),
        encoding="utf-8",
    )
    return str(path)


def run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


def plan(capsys, root: Path, mode: str, edits: dict[str, str]) -> tuple[int, dict]:
    return run(
        capsys,
        root,
        "semantic",
        "slayer",
        mode,
        "author slayer models",
        "--edits-file",
        payload(root, edits),
    )


def errors(envelope: dict) -> str:
    return " ".join(envelope["errors"])


@pytest.fixture
def no_storage_or_warehouse(monkeypatch):
    """Fail the test if SLayer storage is opened or a warehouse is reached."""

    from slayer.storage import yaml_storage

    def refuse(*_args, **_kwargs):
        raise AssertionError("SLayer storage was opened")

    monkeypatch.setattr(yaml_storage.YAMLStorage, "__init__", refuse)

    def no_connection(*_args, **_kwargs):
        raise AssertionError("a warehouse connection was opened")

    monkeypatch.setattr("exmergo_dex_core.connect.open_adapter", no_connection)


# --- the shipped contract --------------------------------------------------------


class TestSlayerSemanticEditing(SemanticEditTargetContract):
    """The write guarantees, without claiming a transformation-project tier."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def _layer(self) -> SlayerSemanticLayer:
        put(self.root, ORDERS, dump(orders()))
        put(self.root, CUSTOMERS, dump(customers()))
        return SlayerSemanticLayer.from_context(
            SemanticSourceContext(
                repo_root=str(self.root),
                connector="duckdb",
                options={"base_dir": BASE, "models": [ORDERS, CUSTOMERS]},
            )
        )

    def make_semantic_edit_target(self):
        return self._layer()

    def an_edit_against_a_changed_semantic_target(self):
        target = self._layer()
        current = target.semantic_edit_view().files[ORDERS]
        edit = PlanEdit(
            path=ORDERS,
            kind=EditKind.SEMANTIC_DOCUMENT,
            op=EditOp.UPSERT,
            old_content_hash=current.sha256,
            new_content="# proposed\n" + current.content,
        )
        put(self.root, ORDERS, "# rewritten by SLayer\n" + current.content)
        return target, [edit], lambda: read(self.root, ORDERS)

    def a_clean_semantic_edit(self, target):
        current = read(self.root, CUSTOMERS)
        edit = PlanEdit(
            path=CUSTOMERS,
            kind=EditKind.SEMANTIC_DOCUMENT,
            op=EditOp.UPSERT,
            old_content_hash=content_hash(current),
            new_content="# proposed clean\n" + current,
        )
        return edit, lambda: read(self.root, CUSTOMERS)


def test_slayer_claims_the_semantic_edit_capability_not_the_project_tier(tmp_path):
    layer = SlayerSemanticLayer(tmp_path, BASE, [ORDERS])

    assert isinstance(layer, SemanticEditTarget)
    assert not isinstance(layer, EditableProject)
    assert layer.semantic_editing_surface() == [ORDERS]


# --- define, update, plan --------------------------------------------------------


def test_define_plans_a_new_model_and_apply_writes_the_authored_bytes(
    tmp_path, capsys, no_storage_or_warehouse
):
    root = repo(tmp_path, [ORDERS, CUSTOMERS], **{CUSTOMERS: dump(customers())})
    # A comment, a key order no dumper would choose, and CRLF line endings: all
    # of it has to land exactly as authored.
    authored = "# owned by dex\r\n" + dump(orders()).replace("\n", "\r\n")

    rc, planned = plan(capsys, root, "define", {ORDERS: authored})

    assert rc == 0, planned["errors"]
    assert planned["data"]["defined"] == ["orders"]
    assert planned["data"]["updated"] == []
    assert planned["diffs"] and planned["diffs"][0]["path"] == ORDERS
    assert not (root / BASE / ORDERS).exists()
    assert any("byte-for-byte" in w for w in planned["warnings"])
    assert any("dex owns" in n for n in planned["data"]["notes"])

    rc, applied = run(capsys, root, "transform", "apply")

    assert rc == 0, applied["errors"]
    assert applied["data"]["written"] == [ORDERS]
    assert (root / BASE / ORDERS).read_bytes() == authored.encode("utf-8")


def test_update_classifies_an_existing_model_and_leaves_the_others_alone(
    tmp_path, capsys, no_storage_or_warehouse
):
    before_customers = dump(customers())
    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: before_customers},
    )
    revised = dump(
        orders(measures=[{"name": "revenue", "formula": "sum(amount) / 100"}])
    )

    rc, planned = plan(capsys, root, "update", {ORDERS: revised})
    assert rc == 0, planned["errors"]
    assert planned["data"]["updated"] == ["orders"]
    assert planned["data"]["defined"] == []

    rc, applied = run(capsys, root, "transform", "apply")
    assert rc == 0, applied["errors"]
    assert read(root, ORDERS) == revised
    assert read(root, CUSTOMERS) == before_customers


def test_define_refuses_an_existing_model_and_update_a_new_one(tmp_path, capsys):
    root = repo(tmp_path, [ORDERS, CUSTOMERS], **{CUSTOMERS: dump(customers())})

    rc, refused_update = plan(capsys, root, "update", {ORDERS: dump(orders())})
    assert rc != 0
    assert "semantic slayer define" in errors(refused_update)

    put(root, ORDERS, dump(orders()))
    rc, refused_define = plan(
        capsys, root, "define", {CUSTOMERS: dump(customers(), "# again\n")}
    )
    assert rc != 0
    assert "semantic slayer update" in errors(refused_define)


def test_a_subcommand_naming_another_vendor_is_refused(tmp_path, capsys):
    configure(tmp_path, [ORDERS], vendor="ossie")
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\nsemantic:\n  vendor: ossie\n  ossie:\n"
        "    files: [layer.ossie.yaml]\n",
        encoding="utf-8",
    )

    rc, refused = plan(capsys, tmp_path, "plan", {ORDERS: dump(orders())})

    assert rc != 0
    assert "use `semantic ossie`" in errors(refused)


# --- broken sets are refused, naming the file and the element ----------------------


@pytest.mark.parametrize(
    ("edits", "named"),
    [
        pytest.param(
            {
                ORDERS: dump(
                    orders(
                        joins=[
                            {
                                "target_model": "nowhere",
                                "join_pairs": [["customer_id", "id"]],
                            }
                        ]
                    )
                )
            },
            [ORDERS, "target_model", "nowhere"],
            id="dangling-join-target",
        ),
        pytest.param(
            {
                ORDERS: dump(
                    orders(
                        joins=[
                            {
                                "target_model": "customers",
                                "join_pairs": [["customer_id", "missing_col"]],
                            }
                        ]
                    )
                )
            },
            [ORDERS, "join_pairs", "missing_col"],
            id="join-pair-missing-column",
        ),
        pytest.param(
            {"models/wh/orders.yaml": dump({**orders(), "name": "orders_v2"})},
            [ORDERS, "orders_v2", "models/<data_source>/<name>.yaml"],
            id="path-name-mismatch",
        ),
        pytest.param(
            {ORDERS: dump({**orders(), "version": FORMAT_VERSION - 1})},
            [ORDERS, "version", "stale", str(FORMAT_VERSION)],
            id="stale-version",
        ),
        pytest.param(
            {ORDERS: dump(orders(default_time_dimension="placed_at"))},
            [ORDERS, "default_time_dimension", "placed_at"],
            id="time-dimension-not-a-column",
        ),
        pytest.param(
            {
                ORDERS: dump(
                    orders(measures=[{"name": "revenue", "formula": "sum(amount"}])
                )
            },
            [ORDERS, "measures['revenue'].formula"],
            id="formula-does-not-parse",
        ),
        pytest.param(
            {
                ORDERS: dump(
                    orders(measures=[{"name": "aov", "formula": "gross / count(*)"}])
                )
            },
            [ORDERS, "measures['aov'].formula", "gross"],
            id="formula-names-no-saved-measure",
        ),
    ],
)
def test_a_broken_set_is_refused_naming_file_and_element(
    tmp_path, capsys, edits, named
):
    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )

    rc, refused = plan(capsys, root, "plan", edits)

    assert rc != 0
    message = errors(refused)
    assert "no plan was stored" in message
    for fragment in named:
        assert fragment in message, (fragment, message)
    assert not list((root / ".dex").glob("plans/*"))


def test_a_duplicate_model_name_refuses_both_files(tmp_path, capsys):
    other = "models/other/orders.yaml"
    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS, other],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )

    rc, refused = plan(
        capsys,
        root,
        "define",
        {other: dump(orders(data_source="other", joins=[]))},
    )

    assert rc != 0
    message = errors(refused)
    assert "duplicate_name" in message
    assert ORDERS in message and other in message


@pytest.mark.parametrize(
    ("path", "says"),
    [
        ("datasources/wh.yaml", "connection credentials"),
        ("priority.yaml", "priority.yaml"),
        ("embeddings.db", "search index"),
        ("models/wh/unlisted.yaml", "not configured"),
        ("../escape.yaml", "outside semantic.slayer.base_dir"),
    ],
)
def test_a_path_outside_the_configured_models_is_refused(tmp_path, capsys, path, says):
    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )

    rc, refused = plan(capsys, root, "plan", {path: "type: duckdb\n"})

    assert rc != 0
    assert says in errors(refused)
    assert not (root / BASE / path).exists()


@pytest.mark.parametrize(
    "secret_at",
    [f"{BASE}/datasources/wh.yaml", "outside-storage/wh.yaml"],
    ids=["datasource-in-storage", "outside-storage"],
)
def test_a_configured_model_symlinked_away_from_models_is_refused(
    tmp_path, capsys, secret_at
):
    root = repo(tmp_path, [ORDERS, CUSTOMERS], **{CUSTOMERS: dump(customers())})
    secret = tmp_path / secret_at
    secret.parent.mkdir(parents=True)
    secret.write_text("password: hunter2\n", encoding="utf-8")
    link = root / BASE / ORDERS
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(secret)

    rc, refused = plan(capsys, root, "plan", {ORDERS: dump(orders())})

    assert rc != 0
    assert "models/ directory" in errors(refused) or "outside" in errors(refused)
    assert "hunter2" not in json.dumps(refused)
    assert secret.read_text(encoding="utf-8") == "password: hunter2\n"


# --- apply -----------------------------------------------------------------------


def test_a_file_changed_after_planning_refuses_the_whole_multi_file_plan(
    tmp_path, capsys
):
    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )
    rc, planned = plan(
        capsys,
        root,
        "update",
        {
            ORDERS: dump(orders(), "# authored orders\n"),
            CUSTOMERS: dump(customers(), "# authored customers\n"),
        },
    )
    assert rc == 0, planned["errors"]
    # What SLayer's storage does on save: rewrite the file in its own layout.
    rewritten = dump(customers(), "# rewritten by slayer serve\n")
    put(root, CUSTOMERS, rewritten)
    orders_before = read(root, ORDERS)

    rc, applied = run(capsys, root, "transform", "apply")

    assert applied["status"] == "needs_confirmation", applied
    assert [c["path"] for c in applied["data"]["conflicts"]] == [CUSTOMERS]
    hint = json.dumps(applied)
    assert "SLayer's own storage" in hint and "semantic slayer plan" in hint
    assert read(root, ORDERS) == orders_before
    assert read(root, CUSTOMERS) == rewritten


def test_formulas_are_skipped_by_name_without_the_sql_extra(
    tmp_path, capsys, monkeypatch
):
    from exmergo_dex_core.slayer import loader

    monkeypatch.setattr(loader, "find_spec", lambda name: None)
    root = repo(
        tmp_path,
        [ORDERS, CUSTOMERS],
        **{ORDERS: dump(orders()), CUSTOMERS: dump(customers())},
    )
    unparseable = orders(measures=[{"name": "revenue", "formula": "sum(amount"}])

    rc, planned = plan(capsys, root, "update", {ORDERS: dump(unparseable)})

    assert rc == 0, planned["errors"]
    assert any(
        "formulas were not parsed" in note and "[sql]" in note
        for note in planned["data"]["notes"]
    )
