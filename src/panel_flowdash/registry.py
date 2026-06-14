"""Component registry: the register decorator and metadata model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PanelAppMetadata:
    """Metadata attached to a component or page by the @register decorator."""

    page: bool = True
    component: bool = False
    sidebar: bool = False
    title: str | None = None
    icon: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    default_size: dict[str, Any] | None = None
    min_size: dict[str, Any] | None = None
    max_size: dict[str, Any] | None = None
    singleton: bool = False
    provides: list[str] = field(default_factory=list)
    requires: list[Any] = field(default_factory=list)
    config_schema: dict[str, Any] | None = None

    @classmethod
    def from_app(cls, app: Any) -> PanelAppMetadata:
        """Extract metadata from an app object."""
        metadata = getattr(app, "__panel_app_metadata__", None)
        if metadata is None:
            metadata = _APP_METADATA_BY_ID.get(id(app))
        if metadata is None:
            return cls()
        if isinstance(metadata, cls):
            return metadata
        if isinstance(metadata, dict):
            return cls(**metadata)
        raise TypeError("Unsupported panel app metadata type.")


_APP_METADATA_BY_ID: dict[int, PanelAppMetadata] = {}


def register(
    *,
    page: bool = True,
    component: bool = False,
    sidebar: bool = False,
    title: str | None = None,
    icon: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    default_size: dict[str, Any] | None = None,
    min_size: dict[str, Any] | None = None,
    max_size: dict[str, Any] | None = None,
    singleton: bool = False,
    provides: list[str] | None = None,
    requires: list[Any] | None = None,
    config_schema: dict[str, Any] | None = None,
):
    """Metadata-only decorator for app exports.

    Annotates an app object/callable without altering runtime behavior.
    """
    metadata = PanelAppMetadata(
        page=page,
        component=component,
        sidebar=sidebar,
        title=title,
        icon=icon,
        description=description,
        tags=list(tags or []),
        default_size=default_size,
        min_size=min_size,
        max_size=max_size,
        singleton=singleton,
        provides=list(provides or []),
        requires=list(requires or []),
        config_schema=config_schema,
    )

    def _decorator(app):
        _APP_METADATA_BY_ID[id(app)] = metadata
        try:
            setattr(app, "__panel_app_metadata__", metadata)
        except Exception:
            pass
        return app

    return _decorator


panel_app = register


@dataclass(frozen=True)
class RegistryEntry:
    """A registered component/page with its metadata."""

    app_id: str
    section: str
    name: str
    page_path: str
    module_name: str
    app: Any
    metadata: PanelAppMetadata

    @property
    def title(self) -> str:
        """Human-readable title."""
        return self.metadata.title or self.name.replace("_", " ")
