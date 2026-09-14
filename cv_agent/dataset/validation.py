"""cv_agent.dataset.validation — Validation gates for root checksums and annotation geometry (ADR-0009)."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from cv_agent.dataset.contracts import (
    AnnotationManifest,
    ClassDefinition,
    DatasetManifest,
)
from cv_agent.dataset.errors import InvalidGeometryError, PayloadIntegrityError
from cv_agent.dataset.types import ContentHash, validate_content_hash

# SHA-256 digest of empty byte string (for empty dataset collection)
EMPTY_PAYLOAD_ROOT_CHECKSUM = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)


def compute_payload_root_checksum(hashes: Iterable[ContentHash | str]) -> str:
    """Compute deterministic, canonical SHA-256 root checksum for a payload collection (Gate B2).

    Algorithm:
    1. Collect all distinct ContentHash strings.
    2. Sort hashes in standard ASCII lexicographical order.
    3. Serialize as newline-terminated ASCII string (empty string if 0 items).
    4. Return lowercase 64-hex SHA-256 digest.
    """
    cleaned = {str(validate_content_hash(str(h))) for h in hashes}
    if not cleaned:
        return EMPTY_PAYLOAD_ROOT_CHECKSUM

    sorted_hashes = sorted(cleaned)
    payload_str = "\n".join(sorted_hashes) + "\n"
    return hashlib.sha256(payload_str.encode("ascii")).hexdigest()


def verify_payload_root_checksum(manifest: DatasetManifest) -> None:
    """Verify that a DatasetManifest's declared payload_root_checksum matches its split membership."""
    all_hashes = manifest.split_membership.all_hashes()
    computed = compute_payload_root_checksum(all_hashes)
    if computed != manifest.payload_root_checksum:
        raise PayloadIntegrityError(
            expected_root=manifest.payload_root_checksum,
            computed_root=computed,
        )


def validate_annotation_geometry(
    annotation_manifest: AnnotationManifest,
    class_definitions: Sequence[ClassDefinition],
    image_extents: Mapping[ContentHash | str, tuple[int, int]] | None = None,
) -> None:
    """Validate geometric validity of all annotation primitives in an AnnotationManifest (AC #8 / D-047).

    Mandatory invariants:
    - Finite coordinates: NaN and Inf are strictly rejected.
    - Bounding boxes: x_min >= 0, y_min >= 0, x_min < x_max, y_min < y_max.
    - Extent containment: coordinates cannot exceed declared image resolution (width, height).
    - Class membership: referenced class names must resolve to declared ClassDefinition names.
    - Polygons: >= 3 distinct vertices, closed loop, strictly positive area (Shoelace formula > 0),
      non-negative coordinates, within extent bounds.

    Raises:
        InvalidGeometryError: if any annotation primitive fails verification.
    """
    valid_class_names = {c.name for c in class_definitions}
    extents = image_extents or {}

    for ch, primitives in annotation_manifest.payload_annotations.items():
        ch_str = str(ch)
        extent = extents.get(ch) or extents.get(ch_str)
        width, height = extent if extent is not None else (None, None)

        for idx, prim in enumerate(primitives):
            # 1. Class validity check
            cls_name = (
                prim.get("class_name") or prim.get("label") or prim.get("class_id")
            )
            if cls_name is None:
                raise InvalidGeometryError(
                    f"Item '{ch_str}' primitive #{idx} is missing 'class_name'",
                    details={
                        "content_hash": ch_str,
                        "primitive_index": idx,
                        "primitive": dict(prim),
                    },
                )
            if str(cls_name) not in valid_class_names:
                raise InvalidGeometryError(
                    f"Item '{ch_str}' primitive #{idx} references undeclared class '{cls_name}'. "
                    f"Declared classes: {sorted(valid_class_names)}",
                    details={
                        "content_hash": ch_str,
                        "primitive_index": idx,
                        "class_name": str(cls_name),
                    },
                )

            # 2. Bounding box geometry check
            bbox: Any = prim.get("bbox")
            if bbox is not None:
                # Support tuple/list [x_min, y_min, x_max, y_max] or dict
                if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                    x_min, y_min, x_max, y_max = bbox
                elif isinstance(bbox, (dict, Mapping)):
                    try:
                        x_min = bbox["x_min"]
                        y_min = bbox["y_min"]
                        x_max = bbox["x_max"]
                        y_max = bbox["y_max"]
                    except KeyError as err:
                        raise InvalidGeometryError(
                            f"Item '{ch_str}' primitive #{idx} bbox dictionary missing key '{err}'",
                            details={"content_hash": ch_str, "primitive_index": idx},
                        ) from None
                else:
                    raise InvalidGeometryError(
                        f"Item '{ch_str}' primitive #{idx} invalid bbox structure: {bbox!r}",
                        details={"content_hash": ch_str, "primitive_index": idx},
                    )

                # Validate numeric types and bounds
                try:
                    x_min, y_min, x_max, y_max = (
                        float(x_min),
                        float(y_min),
                        float(x_max),
                        float(y_max),
                    )
                except (ValueError, TypeError):
                    raise InvalidGeometryError(
                        f"Item '{ch_str}' primitive #{idx} bbox coordinates must be numeric",
                        details={"content_hash": ch_str, "bbox": bbox},
                    ) from None

                # Disallow NaN and +/-Inf
                if not (
                    math.isfinite(x_min)
                    and math.isfinite(y_min)
                    and math.isfinite(x_max)
                    and math.isfinite(y_max)
                ):
                    raise InvalidGeometryError(
                        f"Item '{ch_str}' primitive #{idx} bbox coordinates must be finite (got NaN or Inf)",
                        details={
                            "content_hash": ch_str,
                            "bbox": (x_min, y_min, x_max, y_max),
                        },
                    )

                if x_min < 0 or y_min < 0:
                    raise InvalidGeometryError(
                        f"Item '{ch_str}' primitive #{idx} bbox has negative coordinates: ({x_min}, {y_min})",
                        details={
                            "content_hash": ch_str,
                            "bbox": (x_min, y_min, x_max, y_max),
                        },
                    )

                if x_min >= x_max:
                    raise InvalidGeometryError(
                        f"Item '{ch_str}' primitive #{idx} bbox x_min ({x_min}) >= x_max ({x_max})",
                        details={
                            "content_hash": ch_str,
                            "bbox": (x_min, y_min, x_max, y_max),
                        },
                    )

                if y_min >= y_max:
                    raise InvalidGeometryError(
                        f"Item '{ch_str}' primitive #{idx} bbox y_min ({y_min}) >= y_max ({y_max})",
                        details={
                            "content_hash": ch_str,
                            "bbox": (x_min, y_min, x_max, y_max),
                        },
                    )

                if width is not None and x_max > width:
                    raise InvalidGeometryError(
                        f"Item '{ch_str}' primitive #{idx} bbox x_max ({x_max}) exceeds image width ({width})",
                        details={
                            "content_hash": ch_str,
                            "bbox": (x_min, y_min, x_max, y_max),
                            "width": width,
                        },
                    )

                if height is not None and y_max > height:
                    raise InvalidGeometryError(
                        f"Item '{ch_str}' primitive #{idx} bbox y_max ({y_max}) exceeds image height ({height})",
                        details={
                            "content_hash": ch_str,
                            "bbox": (x_min, y_min, x_max, y_max),
                            "height": height,
                        },
                    )

            # 3. Polygon geometry check
            polygon = prim.get("polygon")
            if polygon is not None:
                if not isinstance(polygon, (list, tuple)) or len(polygon) < 3:
                    raise InvalidGeometryError(
                        f"Item '{ch_str}' primitive #{idx} polygon must have >= 3 vertices",
                        details={"content_hash": ch_str, "polygon": polygon},
                    )

                # Reject explicitly non-closed polygons
                if prim.get("closed") is False or prim.get("is_closed") is False:
                    raise InvalidGeometryError(
                        f"Item '{ch_str}' primitive #{idx} polygon is explicitly marked as non-closed",
                        details={"content_hash": ch_str, "primitive": dict(prim)},
                    )

                parsed_pts: list[tuple[float, float]] = []
                for pt_idx, pt in enumerate(polygon):
                    if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                        raise InvalidGeometryError(
                            f"Item '{ch_str}' primitive #{idx} polygon vertex #{pt_idx} must be (x, y) pair",
                            details={"content_hash": ch_str, "vertex": pt},
                        )
                    try:
                        px, py = float(pt[0]), float(pt[1])
                    except (ValueError, TypeError):
                        raise InvalidGeometryError(
                            f"Item '{ch_str}' primitive #{idx} polygon vertex #{pt_idx} coordinates must be numeric",
                            details={"content_hash": ch_str, "vertex": pt},
                        ) from None

                    # Reject NaN and +/-Inf coordinates
                    if not (math.isfinite(px) and math.isfinite(py)):
                        raise InvalidGeometryError(
                            f"Item '{ch_str}' primitive #{idx} polygon vertex #{pt_idx} coordinates must be finite (got NaN or Inf)",
                            details={"content_hash": ch_str, "vertex": (px, py)},
                        )

                    if px < 0 or py < 0:
                        raise InvalidGeometryError(
                            f"Item '{ch_str}' primitive #{idx} polygon vertex #{pt_idx} has negative coords: ({px}, {py})",
                            details={"content_hash": ch_str, "vertex": (px, py)},
                        )
                    if width is not None and px > width:
                        raise InvalidGeometryError(
                            f"Item '{ch_str}' primitive #{idx} polygon vertex #{pt_idx} x ({px}) > width ({width})",
                            details={
                                "content_hash": ch_str,
                                "vertex": (px, py),
                                "width": width,
                            },
                        )
                    if height is not None and py > height:
                        raise InvalidGeometryError(
                            f"Item '{ch_str}' primitive #{idx} polygon vertex #{pt_idx} y ({py}) > height ({height})",
                            details={
                                "content_hash": ch_str,
                                "vertex": (px, py),
                                "height": height,
                            },
                        )
                    parsed_pts.append((px, py))

                # Normalize polygon ring: if first and last point match, exclude duplicate trailing point for ring vertices
                if parsed_pts[0] == parsed_pts[-1]:
                    ring = parsed_pts[:-1]
                else:
                    ring = parsed_pts

                if len(ring) < 3:
                    raise InvalidGeometryError(
                        f"Item '{ch_str}' primitive #{idx} polygon has fewer than 3 distinct vertices",
                        details={"content_hash": ch_str, "vertices": parsed_pts},
                    )

                # Compute Shoelace area to reject degenerate or zero-area polygons
                n = len(ring)
                area_twice = 0.0
                for i in range(n):
                    x1, y1 = ring[i]
                    x2, y2 = ring[(i + 1) % n]
                    area_twice += (x1 * y2) - (x2 * y1)
                area = abs(area_twice) / 2.0

                if area <= 1e-7:
                    raise InvalidGeometryError(
                        f"Item '{ch_str}' primitive #{idx} polygon has zero or degenerate area ({area})",
                        details={
                            "content_hash": ch_str,
                            "area": area,
                            "vertices": ring,
                        },
                    )
