"""The catalog-only semantic layer for SLayer model files.

Thin on purpose, like the Ossie layer beside it: the reading is the semantic
source's job and reaches this layer through the engine's semantic-source seam,
so what is left here is provenance, the declared gaps, and two refusals.

**Why the refusals.** SLayer does have a query runtime, but it runs through
SLayer's own datasource connection, which carries credentials dex does not read
and spends outside dex's cost guard and query firewall. Under dex, SLayer never
touches the warehouse, so a governed query over a SLayer layer is the relations
the catalog names, read with `explore query`.
"""

from __future__ import annotations

from typing import Any

from ...errors import ProjectError
from .backend import EXECUTION_DEX, BackendDescriptor, SemanticBackendError
from .catalog import SemanticCatalog

# Fields of the neutral catalog SLayer has nowhere to put, per element kind, so
# a caller can tell a structural absence from a field the author left blank.
#
# Two whole kinds are absent. SLayer declares no entities: its joins and keys
# reach dex through the declaration channel instead. And it declares no
# measures in the catalog's sense: a saved `ModelMeasure` is a formula with no
# aggregation of its own, so it is listed as a metric, and a column becomes an
# aggregation input only inside a query.
SLAYER_CATALOG_GAPS: dict[str, list[str]] = {
    "semantic_models": ["label", "model_ref", "primary_entity"],
    "metrics": ["input_measures", "queryable_granularities"],
    "dimensions": ["queryable_granularities"],
    "entities": ["name", "type", "label", "description", "roles"],
    "measures": [
        "name",
        "agg",
        "expr",
        "agg_time_dimension",
        "label",
        "description",
        "semantic_model",
        "column",
    ],
}

_NO_RUNTIME = (
    "dex does not run SLayer queries: SLayer executes through its own datasource "
    "connection, which carries credentials dex does not read and spends outside "
    "dex's cost guard and query firewall, so under dex SLayer never touches the "
    "warehouse"
)


class LocalSlayerLayer:
    """The SLayer model files committed to this repository, as a catalog."""

    name = "local"
    vendor = "slayer"
    deployment = "local"
    execution = EXECUTION_DEX
    catalog_gaps = SLAYER_CATALOG_GAPS
    descriptor = BackendDescriptor(
        name=name,
        vendor=vendor,
        deployment=deployment,
        execution=execution,
        catalog_gaps=SLAYER_CATALOG_GAPS,
    )

    def __init__(self, source: Any) -> None:
        self._source = source

    @classmethod
    def from_engine(cls, engine: Any) -> LocalSlayerLayer:
        """Build through the engine's semantic-source seam, never a project."""

        if engine.repo_root is None:
            raise SemanticBackendError(
                "reading SLayer models needs a repository to read them from: "
                "they are git-reviewable files, so build the engine with "
                "DexEngine.from_repo(repo_root)"
            )
        return cls(engine.semantic_catalog_source())

    def list_definitions(self) -> SemanticCatalog:
        try:
            view = self._source.semantic_catalog()
        except ProjectError as exc:
            raise SemanticBackendError(str(exc)) from exc
        return SemanticCatalog.from_view(view, self)

    def declared_relationships(self) -> list[Any]:
        """SLayer's joins, with every ordered column pair."""

        return self._source.declared_definitions().declared_relationships

    def declared_keys(self) -> tuple[list[Any], list[Any]]:
        """SLayer's declared keys. SLayer is never the transformation project,
        so this is the only route its keys have to grain detection."""

        defs = self._source.declared_definitions()
        return defs.declared_keys, defs.declared_composite_keys

    def query(self, _q: Any) -> Any:
        raise SemanticBackendError(
            f"{_NO_RUNTIME}. Read the layer with `explore semantic list`, then "
            "query the relations its semantic models name with `explore query`, "
            "which runs under the firewall and the cost guard"
        )

    def values(self, _dimension: str, _metrics: list[str]) -> Any:
        raise SemanticBackendError(
            f"{_NO_RUNTIME}. A dimension backed by a bare column names its "
            "relation and column in the catalog; profile that relation instead"
        )

    def filter_refs(self, _clauses: list[str]) -> list[str] | None:
        """No filter to read, because there is no query to filter."""

        return None
