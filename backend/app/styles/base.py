"""Style registry and parameter validation."""

from __future__ import annotations

import importlib
import pkgutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, ValidationError as PydanticValidationError
from pydantic import create_model

from app.core.errors import ValidationAppError

_REGISTRY: dict[str, "ClipStyle"] = {}


@dataclass(frozen=True)
class StyleParameter:
    """Declarative parameter description — drives the UI and validation."""

    name: str
    type: str  # "number" | "string" | "boolean" | "enum"
    default: Any = None
    choices: list[str] = field(default_factory=list)
    description: str = ""

    def public(self) -> dict:
        out = {
            "name": self.name,
            "type": self.type,
            "default": self.default,
            "description": self.description,
        }
        if self.type == "enum":
            out["choices"] = list(self.choices)
        return out


class ClipStyle:
    """Base class. Subclasses set metadata and implement apply()."""

    id: str = "abstract"
    name: str = "Abstract"
    description: str = ""
    parameters: list[StyleParameter] = []

    def apply(
        self,
        source: Path,
        output: Path,
        *,
        start_in_source: float,
        duration: float,
        params: dict[str, Any],
    ) -> None:
        """Transform `source` [start_in_source, +duration) into `output`.

        Must raise on failure (the orchestrator converts any exception into a
        failed job with the message).
        """
        raise NotImplementedError


def register_style(style: ClipStyle) -> ClipStyle:
    if not style.id or style.id == "abstract":
        raise ValueError("style must define a unique non-empty id")
    if style.id in _REGISTRY:
        raise ValueError(f"duplicate style id: {style.id}")
    _REGISTRY[style.id] = style
    return style


def get_style(style_id: str) -> ClipStyle | None:
    return _REGISTRY.get(style_id)


def all_styles() -> list[ClipStyle]:
    return list(_REGISTRY.values())


def _params_model(style: ClipStyle):
    fields: dict[str, tuple] = {}
    for p in style.parameters:
        if p.type == "number":
            fields[p.name] = (float, p.default)
        elif p.type == "string":
            fields[p.name] = (str, p.default)
        elif p.type == "boolean":
            fields[p.name] = (bool, p.default)
        elif p.type == "enum":
            literal = Literal[tuple(p.choices)]  # type: ignore[valid-type]
            fields[p.name] = (literal, p.default)
        else:  # pragma: no cover - guarded by tests
            raise ValueError(f"unknown parameter type {p.type!r}")
    return create_model(
        f"{style.id}_params",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def validate_params(style: ClipStyle, raw: dict | None) -> dict:
    """Coerce + default a params dict against the style's declarations."""
    if not style.parameters:
        if raw:
            raise ValidationAppError(
                f"Style {style.id!r} accepts no parameters.",
                field="style_params",
                details=[{"param": k, "message": "unknown parameter"} for k in raw],
            )
        return {}
    model = _params_model(style)
    try:
        obj = model.model_validate(raw or {})
    except PydanticValidationError as exc:
        details = [
            {"param": ".".join(str(x) for x in e["loc"]) or "?", "message": e["msg"]}
            for e in exc.errors()
        ]
        raise ValidationAppError(
            f"Invalid parameters for style {style.id!r}.",
            field="style_params",
            details=details,
        ) from exc
    return obj.model_dump()


def public_style(style: ClipStyle) -> dict:
    return {
        "id": style.id,
        "name": style.name,
        "description": style.description,
        "parameters": [p.public() for p in style.parameters],
    }


def autodiscover() -> list[str]:
    """Import every sibling module and auto-register its `STYLE`.

    A new clip style = ONE new file in this package defining a module-level
    `STYLE = MyStyle()` constant. Autodiscovery does the rest: no API,
    database, or UI changes anywhere. Must be called from the package
    __init__ (``__package__`` refers to the app.styles package there).
    """
    package = sys.modules[__package__]
    loaded = []
    for mod in pkgutil.iter_modules(package.__path__):
        if mod.name in ("base", "__init__"):
            continue
        module = importlib.import_module(f"{__package__}.{mod.name}")
        style = getattr(module, "STYLE", None)
        if isinstance(style, ClipStyle):
            if style.id in _REGISTRY:
                raise ValueError(
                    f"duplicate style id {style.id!r} from module {mod.name!r}"
                )
            _REGISTRY[style.id] = style
        loaded.append(mod.name)
    return loaded
