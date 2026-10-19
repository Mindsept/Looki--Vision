"""UniDepthV2 : profondeur métrique et intrinsèques estimées depuis RGB.

L'implémentation officielle est une dépendance figée, pas une copie dans app.
Son API ``infer`` normalise l'entrée, retire son padding et remet la carte à la
taille originale. La sortie ``depth`` est z caméra en mètres ; ``radius`` n'est
pas utilisé. Aucune focale inventée ni correction d'échelle n'est appliquée.

Sources : https://github.com/lpiccinelli-eth/UniDepth et
https://huggingface.co/lpiccinelli/unidepth-v2-vits14 . Licence CC BY-NC 4.0.
"""

import json
import math
import threading
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
from PIL import Image

from app.vision.calibration import CameraCalibration
from app.vision.depth_anything import DepthPrediction
from app.vision.timing import elapsed_ms, synchronize_device

DEFAULT_UNIDEPTH_MODEL_ID = "lpiccinelli/unidepth-v2-vits14"
UNIDEPTH_MODEL_REVISION = "038c238f06c87b6c2f5b3749fd51fbf442b1f218"
UNIDEPTH_SOURCE_REVISION = "8d8cfe4c7ee15297099983607febf0d4f32eb3d6"
# Rejection ceiling only: not a learned model range and never used to rescale.
DEPTH_SANITY_MAX_M = 10_000.0


class UniDepthMetricProvider:
    """Charger uniquement des safetensors ; inférence locale sans fallback mock.

    Les horodatages distinguent la conversion RGB, l'API officielle ``infer`` et
    l'extraction CPU. ``depth_inference_ms`` inclut le pré/post-traitement INTERNE
    de cette API : il ne doit pas être présenté comme son seul réseau neuronal.
    Les objets injectables permettent des tests sans torch, poids ou réseau.
    """

    def __init__(
        self,
        model_id: str = DEFAULT_UNIDEPTH_MODEL_ID,
        device: str = "cpu",
        *,
        revision: str = UNIDEPTH_MODEL_REVISION,
        resolution_level: int | None = None,
        camera_calibration: CameraCalibration | None = None,
        model: Any = None,
        torch_module: Any = None,
    ) -> None:
        if resolution_level is not None and (
            isinstance(resolution_level, bool)
            or not isinstance(resolution_level, int)
            or not 0 <= resolution_level < 10
        ):
            raise ValueError("UniDepth resolution_level must be an integer in [0, 9] or None")
        if not revision:
            raise ValueError("UniDepth weights require an explicit pinned revision")
        if camera_calibration is not None and not isinstance(camera_calibration, CameraCalibration):
            raise ValueError("camera_calibration must be a CameraCalibration instance or None")
        if torch_module is None:
            try:
                import torch
            except ImportError as exc:
                raise RuntimeError("Install the vision extra to use unidepth_v2") from exc
            torch_module = torch
        if model is None:
            try:
                from huggingface_hub import snapshot_download
                from safetensors.torch import load_model
                from unidepth.models import UniDepthV2
            except ImportError as exc:
                raise RuntimeError(
                    "Install the vision extra with uv sync to use the pinned UniDepth dependency"
                ) from exc
            snapshot = Path(
                snapshot_download(
                    model_id,
                    revision=revision,
                    allow_patterns=["config.json", "model.safetensors"],
                )
            )
            config = json.loads((snapshot / "config.json").read_text())
            # Official full checkpoints use null/None to disable encoder loading.
            # Upstream treats "" as a download and every other non-None value
            # (including False) as a torch.load argument. Accept only explicit null.
            encoder = config.get("model", {}).get("pixel_encoder", {})
            if "pretrained" not in encoder or encoder["pretrained"] is not None:
                raise ValueError(
                    "UniDepth full checkpoint must disable separate pretrained backbone"
                )
            model = UniDepthV2(config=config)
            load_model(model, str(snapshot / "model.safetensors"), strict=True)
        self.model_id = model_id
        self.model_revision = revision
        self.max_depth_m = DEPTH_SANITY_MAX_M
        self.device = device
        self.resolution_level = resolution_level
        self.camera_calibration = camera_calibration
        self._torch = torch_module
        self._model = model.to(device).float().eval()
        if resolution_level is not None:
            self._model.resolution_level = resolution_level
        self._lock = threading.Lock()

    def predict_depth(
        self,
        image: Image.Image,
        *,
        camera_calibration: CameraCalibration | None = None,
    ) -> DepthPrediction:
        """L'override par image prime sur la calibration fixe ; None garde ce défaut."""
        calibration = (
            camera_calibration if camera_calibration is not None else self.camera_calibration
        )
        if calibration is not None and not isinstance(calibration, CameraCalibration):
            raise ValueError("camera_calibration must be a CameraCalibration instance or None")
        with self._lock, self._torch.inference_mode():
            synchronize_device(self._torch, self.device)
            step = perf_counter()
            pixels = np.asarray(image.convert("RGB")).copy()
            rgb = self._torch.from_numpy(pixels).permute(2, 0, 1).to(self.device)
            input_intrinsics = (
                calibration.matrix_for_size(image.width, image.height) if calibration else None
            )
            camera = (
                self._torch.from_numpy(input_intrinsics.copy()).to(self.device)
                if input_intrinsics is not None
                else None
            )
            synchronize_device(self._torch, self.device)
            preprocess_ms = elapsed_ms(step)
            step = perf_counter()
            # Keep uint8 RGB: the official API applies ImageNet normalization.
            output = (
                self._model.infer(rgb, camera=camera)
                if camera is not None
                else self._model.infer(rgb)
            )
            synchronize_device(self._torch, self.device)
            inference_ms = elapsed_ms(step)
            step = perf_counter()
            depth = output["depth"].detach().float().cpu().numpy()
            intrinsics = output["intrinsics"].detach().float().cpu().numpy()
            if depth.shape != (1, 1, image.height, image.width):
                raise ValueError("UniDepth must return one depth map at the original image size")
            if (
                intrinsics.shape != (1, 3, 3)
                or not np.isfinite(intrinsics).all()
                or intrinsics[0, 0, 0] <= 0
                or intrinsics[0, 1, 1] <= 0
                or not np.allclose(intrinsics[0, 2], [0, 0, 1], atol=1e-5)
            ):
                raise ValueError("UniDepth returned invalid estimated camera intrinsics")
            # Upstream returns its estimated K even when provided rays guide the
            # depth decoder. Report the K actually supplied to infer separately.
            matrix = intrinsics[0] if input_intrinsics is None else input_intrinsics
            fov = math.degrees(2 * math.atan(image.width / (2 * float(matrix[0, 0]))))
            metadata = {
                "depth_backend": "unidepth_v2",
                "depth_model_id": self.model_id,
                "depth_model_revision": self.model_revision,
                "depth_source_revision": UNIDEPTH_SOURCE_REVISION,
                "depth_intrinsics": matrix.tolist(),
                "depth_intrinsics_source": "estimated" if calibration is None else "provided",
                "depth_model_estimated_intrinsics": intrinsics[0].tolist(),
                "depth_calibration_reference_size": (
                    [calibration.width, calibration.height] if calibration else None
                ),
                "depth_intrinsics_image_size": [image.width, image.height],
                "depth_horizontal_fov_deg": fov,
                "depth_resolution_level": self.resolution_level,
                "depth_resolution_policy": (
                    "official infer: 200k..600k pixels by default"
                    if self.resolution_level is None
                    else f"official infer: resolution_level={self.resolution_level}"
                ),
                "depth_dtype": "float32",
                "depth_semantics": "camera z in meters; not Euclidean range",
                "depth_max_semantics": "10000m rejection ceiling; not trained range or scale factor",
                "depth_timing_scope": (
                    "depth_inference_ms measures official infer, including its internal "
                    "normalization, resizing, network and output reconstruction"
                ),
            }
            postprocess_ms = elapsed_ms(step)
        return DepthPrediction(
            depth_m=depth[0, 0],
            timing_ms={
                "depth_preprocess_ms": preprocess_ms,
                "depth_inference_ms": inference_ms,
                "depth_postprocess_ms": postprocess_ms,
                "depth_total_ms": preprocess_ms + inference_ms + postprocess_ms,
            },
            metadata=metadata,
        )
