"""Reading SLayer model files, and judging whether each one is a model.

The one module in dex that imports SLayer. Everything else reaches the validated
models through :func:`load_models`, which is reached only through a source that
:func:`.dependency.ensure_available` let construct, so a base install never
imports it.

**SLayer's own model is the validator, and SLayer's storage is never opened.**
Each configured file is read with ``yaml.safe_load`` and validated with SLayer's
``SlayerModel``, so what dex accepts is what the pinned release accepts, with no
second reading of a format that moves between releases. ``YAMLStorage`` is not
used: opening it runs layout migrations that move and rename files under the
storage root, and a read that rewrites the repository is not a read.

**A stale format is refused before SLayer sees it.** ``SlayerModel`` migrates an
older ``version`` while parsing and passes a newer one through on a best-effort
basis. Either would let dex report a file as current when SLayer's next save
would rewrite it, or read fields the pinned release does not know, so the
version is checked first and anything but the pinned release's current version
is an error naming both numbers.

**Problems are collected, not raised.** A file with two problems reports two,
and a caller on the declaration channel may not raise at all, so every finding
is a :class:`Diagnostic` naming the file and the element it sits at. A model
with an error is absent from :attr:`LoadResult.models` and present in
:attr:`LoadResult.diagnostics`, which is what keeps "this could not be read"
from looking like "this declares nothing".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import ValidationError
from slayer.core.models import SlayerModel, join_key_error
from slayer.storage.migrations import CURRENT_VERSIONS

from .dependency import SLAYER_VERSION

__all__ = [
    "FORMAT_VERSION",
    "Diagnostic",
    "LoadResult",
    "LoadedModel",
    "load_models",
]

#: The model format version the pinned SLayer release writes. A file carrying
#: any other version is refused rather than migrated or read best-effort.
FORMAT_VERSION: int = CURRENT_VERSIONS["SlayerModel"]

# Severity vocabulary, matching the Ossie reader's: `error` means dex will not
# read the model, `warning` means it read it and something in it is questionable.
ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Diagnostic:
    """One thing wrong with a model file.

    ``element`` is where in the model the problem sits, named by the element's
    own name where it has one (``columns['order id'].name``), ``(root)`` for the
    model as a whole, and ``(config)`` for the coordinate that named the file.
    ``rule`` says which check spoke, so a caller can act on a class of finding
    without matching on prose.
    """

    file: str
    element: str
    rule: str
    severity: str
    message: str

    def render(self) -> str:
        """One line, for a note list or an exception message."""

        return f"{self.file}: {self.element}: [{self.rule}] {self.message}"


@dataclass(frozen=True)
class LoadedModel:
    """One model that passed every check, with the file it came from."""

    file: str
    model: SlayerModel


@dataclass
class LoadResult:
    """Every configured model dex could read, and everything wrong with the rest."""

    models: list[LoadedModel] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == ERROR]

    def notes(self) -> list[str]:
        """Every diagnostic as a line, for a caller's note list."""

        return [d.render() for d in self.diagnostics]


def load_models(
    repo_root: Path | str, base_dir: str, models: Sequence[str]
) -> LoadResult:
    """Read, validate, and return every configured model file.

    Never raises for a bad file. ``models`` are named relative to ``base_dir``,
    and each diagnostic names the file that way too, because that is the
    spelling the configuration line uses.
    """

    root = Path(repo_root).resolve()
    storage = (root / base_dir).resolve()
    result = LoadResult()
    parsed: list[LoadedModel] = []
    for name in models:
        path, refusal = _resolve(root, storage, name)
        if refusal is not None:
            result.diagnostics.append(refusal)
            continue
        loaded, found = _read(path, name)
        result.diagnostics.extend(found)
        if loaded is not None:
            parsed.append(loaded)

    # A file that failed before its name could be read still names its model in
    # its path, so a join to it is reported as unreadable rather than unknown.
    configured = {PurePosixPath(name).stem for name in models}
    result.models = _across_models(parsed, configured, result.diagnostics)
    for loaded in result.models:
        result.diagnostics.extend(_warnings(loaded))
    return result


def _resolve(
    root: Path, storage: Path, name: str
) -> tuple[Path | None, Diagnostic | None]:
    """The configured name as a file inside the storage root, or a refusal.

    Confinement is checked on the *resolved* path, so a symlink pointing out of
    the repository, or out of the storage root into a datasource file, is caught
    along with a ``..`` that walks out of it.
    """

    def refusal(rule: str, message: str) -> tuple[None, Diagnostic]:
        return None, Diagnostic(
            file=name, element="(config)", rule=rule, severity=ERROR, message=message
        )

    if PurePosixPath(name).is_absolute():
        return refusal(
            "confinement",
            f"a SLayer model is named relative to semantic.slayer.base_dir, and "
            f"'{name}' is an absolute path",
        )
    resolved = (storage / name).resolve()
    for boundary, what in ((root, "the repository"), (storage, "the storage root")):
        try:
            resolved.relative_to(boundary)
        except ValueError:
            return refusal(
                "confinement",
                f"'{name}' resolves outside {what} ({resolved}); dex reads SLayer "
                "models only from semantic.slayer.base_dir inside the repository",
            )
    if not resolved.is_file():
        return refusal("missing", f"no such SLayer model file: '{name}'")
    return resolved, None


def _read(path: Path, name: str) -> tuple[LoadedModel | None, list[Diagnostic]]:
    """Parse and validate one file, collecting every problem with it."""

    def refusal(element: str, rule: str, message: str) -> list[Diagnostic]:
        return [
            Diagnostic(
                file=name, element=element, rule=rule, severity=ERROR, message=message
            )
        ]

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return None, refusal("(root)", "unreadable", f"could not read the file: {exc}")
    except yaml.YAMLError as exc:
        # `yaml.YAMLError` descends from `Exception` rather than `ValueError`, so
        # it is caught by name.
        return None, refusal("(root)", "malformed", f"not valid YAML: {exc}")
    if not isinstance(raw, dict):
        return None, refusal(
            "(root)",
            "root",
            "a SLayer model is a mapping with `version` and `name` at its root, "
            f"and this parsed as {type(raw).__name__}",
        )

    stale = _version_problem(raw.get("version"))
    if stale is not None:
        return None, refusal("version", "format_version", stale)

    try:
        model = SlayerModel.model_validate(raw)
    except ValidationError as exc:
        return None, [
            Diagnostic(
                file=name,
                element=_element(raw, error["loc"]),
                rule="invalid",
                severity=ERROR,
                message=_message(error),
            )
            for error in exc.errors()
        ]
    except (ValueError, TypeError) as exc:
        # A SLayer validator that raises its own error outside pydantic's
        # wrapping still means the model is not one the pinned release accepts.
        return None, refusal("(root)", "invalid", str(exc))

    mismatch = _path_problem(name, model)
    if mismatch is not None:
        return None, refusal("(root)", "path", mismatch)
    return LoadedModel(file=name, model=model), []


def _version_problem(version: Any) -> str | None:
    """Why ``version`` is not the pinned format, or ``None`` when it is.

    A file with no ``version`` is stale rather than current: SLayer's storage
    stamps a stored model without one as version 1 and migrates it from there,
    so reading it as current would disagree with the tool that owns the format.
    """

    expected = (
        f"SLayer {SLAYER_VERSION} reads and writes model format version "
        f"{FORMAT_VERSION}"
    )
    if version is None:
        return (
            f"the model carries no `version`, which SLayer reads as format "
            f"version 1. {expected}; set `version: {FORMAT_VERSION}` once the "
            "model is current, rather than letting SLayer migrate it on its next "
            "save"
        )
    if isinstance(version, bool) or not isinstance(version, int):
        return f"`version` is {version!r}, not a format version number. {expected}"
    if version < FORMAT_VERSION:
        return (
            f"the model is written in format version {version}, which is stale. "
            f"{expected}, and SLayer would migrate this file on its next save. "
            "dex reads only the current format rather than migrating it in "
            "memory and reporting it as current"
        )
    if version > FORMAT_VERSION:
        return (
            f"the model is written in format version {version}, newer than this "
            f"dex reads. {expected}; a newer format may carry fields the pinned "
            "release does not know, so it is refused rather than read "
            "best-effort"
        )
    return None


def _path_problem(name: str, model: SlayerModel) -> str | None:
    """Whether the file sits where SLayer would keep this model.

    SLayer stores a model at ``models/<data_source>/<name>.yaml``, so a file
    whose path and content disagree is one SLayer would read under a different
    name, or not at all.
    """

    parts = PurePosixPath(name).parts
    if len(parts) != 3:
        return None
    data_source, stem = parts[1], PurePosixPath(parts[2]).stem
    if model.name != stem or model.data_source != data_source:
        return (
            f"the file is at models/{data_source}/{stem}, and the model inside "
            f"declares name '{model.name}' and data_source "
            f"'{model.data_source}'. SLayer keeps a model at "
            "models/<data_source>/<name>.yaml, so the two have to agree"
        )
    return None


def _across_models(
    parsed: list[LoadedModel], configured: set[str], diagnostics: list[Diagnostic]
) -> list[LoadedModel]:
    """The models that survive the checks that need the whole set.

    A duplicated name retracts **every** file declaring it: SLayer resolves a
    join and a dotted reference by model name, so either copy would silently
    mean the other. A join to a model the set does not hold retracts the model
    declaring it, and that runs to a fixed point, because a model joining to a
    retracted one now joins to nothing.
    """

    names: dict[str, list[str]] = {}
    for loaded in parsed:
        names.setdefault(loaded.model.name, []).append(loaded.file)
    for model_name, files in names.items():
        if len(files) < 2:
            continue
        for file in files:
            others = ", ".join(f for f in files if f != file)
            diagnostics.append(
                Diagnostic(
                    file=file,
                    element="name",
                    rule="duplicate_name",
                    severity=ERROR,
                    message=(
                        f"model '{model_name}' is also declared in {others}. SLayer "
                        "resolves joins and references by model name, so neither "
                        "copy is read"
                    ),
                )
            )
    surviving = [m for m in parsed if len(names[m.model.name]) == 1]
    configured = configured | set(names)

    while True:
        by_name = {m.model.name: m.model for m in surviving}
        broken: dict[str, list[Diagnostic]] = {}
        for loaded in surviving:
            found = _join_problems(loaded, by_name, configured)
            if found:
                broken[loaded.file] = found
        if not broken:
            return surviving
        for found in broken.values():
            diagnostics.extend(found)
        surviving = [m for m in surviving if m.file not in broken]


def _join_problems(
    loaded: LoadedModel, by_name: dict[str, SlayerModel], configured: set[str]
) -> list[Diagnostic]:
    """Every join of one model that does not resolve within the set.

    The source side of each pair is already checked by ``SlayerModel`` itself.
    The target side needs the other model, so SLayer checks it only when its
    storage loads the peer, which dex never opens; the same rule is applied here
    through SLayer's own ``join_key_error``.
    """

    model = loaded.model
    found: list[Diagnostic] = []
    for index, join in enumerate(model.joins):
        element = f"joins[{join.name or index}]"
        target = by_name.get(join.target_model)
        if target is None:
            reason = (
                "which is configured but could not be read (see its own diagnostic)"
                if join.target_model in configured
                else "which no configured model file declares"
            )
            found.append(
                Diagnostic(
                    file=loaded.file,
                    element=f"{element}.target_model",
                    rule="unknown_join_target",
                    severity=ERROR,
                    message=(
                        f"model '{model.name}' joins to '{join.target_model}', "
                        f"{reason}. A join dex cannot resolve would reach the "
                        "relationship channel as a declared edge to nothing"
                    ),
                )
            )
            continue
        for _, key in join.join_pairs:
            error = join_key_error(
                model=model.name,
                target=target.name,
                key=key,
                side=target.name,
                columns=target.columns,
            )
            if error is not None and not target.awaits_columns:
                found.append(
                    Diagnostic(
                        file=loaded.file,
                        element=f"{element}.join_pairs",
                        rule="join_key",
                        severity=ERROR,
                        message=str(error),
                    )
                )
    return found


def _warnings(loaded: LoadedModel) -> list[Diagnostic]:
    """What dex reads past and says so."""

    model = loaded.model
    declared = model.default_time_dimension
    if declared and model.get_column(declared) is None:
        return [
            Diagnostic(
                file=loaded.file,
                element="default_time_dimension",
                rule="unknown_column",
                severity=WARNING,
                message=(
                    f"model '{model.name}' names default_time_dimension "
                    f"'{declared}', which is not one of its columns, so dex "
                    "reports no time dimension for it"
                ),
            )
        ]
    return []


def _element(raw: dict[str, Any], loc: Sequence[Any]) -> str:
    """A pydantic error location, with list indices named by the element's name.

    ``columns -> 3 -> name`` reads as nothing to someone holding the file;
    ``columns['order id'].name`` says which column to open.
    """

    if not loc:
        return "(root)"
    rendered = ""
    node: Any = raw
    for part in loc:
        if isinstance(part, int):
            item = node[part] if isinstance(node, list) and part < len(node) else None
            label = item.get("name") if isinstance(item, dict) else None
            rendered += f"[{label!r}]" if isinstance(label, str) else f"[{part}]"
            node = item
        else:
            rendered += f".{part}" if rendered else str(part)
            node = node.get(part) if isinstance(node, dict) else None
    return rendered


def _message(error: Any) -> str:
    """Pydantic's message without its "Value error, " prefix."""

    message = str(error.get("msg", ""))
    return message.removeprefix("Value error, ")
