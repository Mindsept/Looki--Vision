"""Intrinsèques d'une caméra pinhole rectifiée, dans une résolution de référence.

Ce contrat ne contient ni extrinsèques ni coefficients de distorsion. Les images
doivent être rectifiées et conserver le champ de vue de la calibration. Un
redimensionnement de l'image entière est autorisé ; un recadrage nécessite une
nouvelle calibration décrivant le cadrage réellement transmis. Un crop qui garde
le même ratio ne peut pas être détecté à partir des dimensions seules.
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Integral, Real
from pathlib import Path
from typing import Any

import numpy as np

CALIBRATION_FIELDS = frozenset({"fx", "fy", "cx", "cy", "width", "height"})


def _positive_dimension(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
        raise ValueError(f"Camera calibration {name} must be a positive integer")
    return int(value)


@dataclass(frozen=True)
class CameraCalibration:
    """Focales et point principal en pixels ; largeur/hauteur avant resize."""

    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int

    def __post_init__(self) -> None:
        for name in ("fx", "fy", "cx", "cy"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
                raise ValueError(f"Camera calibration {name} must be a finite number")
            if name in {"fx", "fy"} and value <= 0:
                raise ValueError(f"Camera calibration {name} must be positive")
            object.__setattr__(self, name, float(value))
        for name in ("width", "height"):
            object.__setattr__(self, name, _positive_dimension(getattr(self, name), name))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CameraCalibration":
        if not isinstance(value, Mapping) or set(value) != CALIBRATION_FIELDS:
            raise ValueError(
                "Camera calibration must contain exactly width, height, fx, fy, cx, cy "
                "for a rectified pinhole image; distortion/extrinsics are unsupported"
            )
        return cls(**value)

    @classmethod
    def from_json(cls, path: Path) -> "CameraCalibration":
        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"Duplicate camera calibration field: {key}")
                result[key] = value
            return result

        payload = json.loads(Path(path).read_text(), object_pairs_hook=unique_fields)
        return cls.from_dict(payload)

    def matrix_for_size(self, width: int, height: int) -> np.ndarray:
        """K après resize intégral ; seul l'arrondi d'au plus un pixel est toléré.

        fx/cx suivent le facteur horizontal, fy/cy le vertical, y compris leur
        minuscule différence due aux dimensions entières. Aucun offset de crop
        n'est deviné. L'API UniDepth adaptera ensuite cette K à son resize interne.
        """
        width = _positive_dimension(width, "target width")
        height = _positive_dimension(height, "target height")
        if abs(width * self.height - height * self.width) > max(self.width, self.height):
            raise ValueError(
                "Camera calibration aspect ratio mismatch; only full-image resize is supported"
            )
        sx, sy = width / self.width, height / self.height
        with np.errstate(over="ignore", under="ignore", invalid="ignore"):
            matrix = np.array(
                [
                    [self.fx * sx, 0.0, self.cx * sx],
                    [0.0, self.fy * sy, self.cy * sy],
                    [0.0, 0.0, 1.0],
                ],
                dtype=np.float32,
            )
        if not np.isfinite(matrix).all() or matrix[0, 0] <= 0 or matrix[1, 1] <= 0:
            raise ValueError(
                "Camera calibration cannot be represented as finite positive float32 intrinsics"
            )
        return matrix
