"""Isolated, non-destructive editing domain."""

from .contracts import (
    CoverSpec,
    DubbingSpec,
    EditRecipe,
    EditingError,
    MediaProcessor,
    RenderAsset,
    RenderResult,
    SegmentSpec,
    TranslationSpec,
    recipe_from_mapping,
)
from .schema import SCHEMA_VERSION, EditingSchemaError, ensure_editing_schema, validate_editing_schema
from .service import EditingService, default_editing_root

__all__ = [
    "CoverSpec",
    "DubbingSpec",
    "EditRecipe",
    "EditingError",
    "EditingSchemaError",
    "EditingService",
    "MediaProcessor",
    "RenderAsset",
    "RenderResult",
    "SCHEMA_VERSION",
    "SegmentSpec",
    "TranslationSpec",
    "default_editing_root",
    "ensure_editing_schema",
    "recipe_from_mapping",
    "validate_editing_schema",
]
