"""FaceTracker — MediaPipe Face Landmarker wrapper for the head pointer.

Runs the Face Landmarker on the same ``camera_frame`` messages the L515 sidecar
(or the iPad) already streams, and emits a single ``HeadSample`` per frame:

  * forward   — head-forward unit vector (camera space) from the 4x4 facial
                transformation matrix.
  * nose_nxy  — nose-tip normalized image position (nx, ny) for the depth
                parallax correction (the L515 depth is looked up at this point).

The pose math + cursor mapping live in ``sensors/head_pointer.py`` (pure /
testable); this module owns only the camera-facing inference, and degrades
gracefully when mediapipe / cv2 / the model file are unavailable (logs a warning,
never crashes — same contract as GestureProcessor).

The model file ``sensors/face_landmarker.task`` is NOT vendored. Download once:

    https://storage.googleapis.com/mediapipe-models/face_landmarker/\
face_landmarker/float16/1/face_landmarker.task

and drop it next to ``hand_landmarker.task`` in ``sensors/``.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Optional

from sensors.head_pointer import head_forward

log = logging.getLogger("face_tracker")

try:
    import mediapipe as mp
    import numpy as np
    from mediapipe.tasks.python import BaseOptions as _BaseOptions
    from mediapipe.tasks.python.vision import (
        FaceLandmarker as _FaceLandmarker,
        FaceLandmarkerOptions as _FaceLandmarkerOptions,
        RunningMode as _RunningMode,
    )
    _MP_AVAILABLE = True
except (ImportError, AttributeError) as _mp_import_err:  # pragma: no cover
    _MP_AVAILABLE = False
    log.warning(
        "mediapipe not installed — FaceTracker disabled. "
        "pip install mediapipe  (%s)", _mp_import_err
    )

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:  # pragma: no cover
    _CV2_AVAILABLE = False
    log.warning("opencv-python not installed — FaceTracker disabled.")

# MediaPipe Face Mesh nose-tip landmark. Index 1 is the nasal tip — the point
# the L515 sees most cleanly and the natural ray origin.
_NOSE_TIP = 1


@dataclass
class HeadSample:
    """One processed camera frame's head pose. ``ok`` is False when no usable
    face/pose was found (caller should reset the pointer)."""
    ok: bool
    forward: Optional[tuple] = None     # (fx, fy, fz) camera-space unit vector
    nose_nxy: Optional[tuple] = None    # (nx, ny) normalized image position


class FaceTracker:
    """Lazy MediaPipe Face Landmarker over ``camera_frame`` messages."""

    _MODEL_PATH: str = "sensors/face_landmarker.task"

    def __init__(self) -> None:
        self._face = None
        self._available = _MP_AVAILABLE and _CV2_AVAILABLE
        self._frame_count = 0
        # Most-recent normalized landmarks (for the viewer overlay) + matrix.
        self.latest_landmarks: Optional[list[tuple[float, float]]] = None
        self.latest_matrix = None

    # ── FaceLandmarker (lazy) ─────────────────────────────────────────────
    def _get_face(self):
        if self._face is not None or not self._available:
            return self._face
        import os
        if not os.path.exists(self._MODEL_PATH):
            log.warning(
                "FaceTracker: %s not found — head pointer disabled. "
                "Download the Face Landmarker model (see module docstring).",
                self._MODEL_PATH,
            )
            self._available = False
            return None
        options = _FaceLandmarkerOptions(
            base_options=_BaseOptions(model_asset_path=self._MODEL_PATH),
            running_mode=_RunningMode.VIDEO,
            num_faces=1,
            output_facial_transformation_matrixes=True,
            output_face_blendshapes=False,
        )
        self._face = _FaceLandmarker.create_from_options(options)
        log.info("FaceTracker: FaceLandmarker loaded")
        return self._face

    def on_camera_frame(self, msg: dict) -> Optional[HeadSample]:
        """Decode a ``camera_frame`` message and return a HeadSample, or None
        when the tracker is unavailable / the frame can't be decoded."""
        if not self._available:
            return None
        image_b64 = msg.get("image_b64", "")
        if not image_b64:
            return None
        try:
            img_bytes = base64.b64decode(image_b64)
            img_arr = cv2.imdecode(
                np.frombuffer(img_bytes, dtype="u1"), cv2.IMREAD_COLOR
            )
            if img_arr is None:
                return None
        except Exception as exc:
            log.debug("FaceTracker: frame decode error: %s", exc)
            return None
        self._frame_count += 1
        return self._process_frame(img_arr)

    def _process_frame(self, bgr_frame) -> Optional[HeadSample]:
        face = self._get_face()
        if face is None:
            return None
        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        ts_ms = int(time.monotonic() * 1000)
        result = face.detect_for_video(mp_image, ts_ms)

        if not result.face_landmarks:
            self.latest_landmarks = None
            self.latest_matrix = None
            return HeadSample(ok=False)

        lm = result.face_landmarks[0]
        # Keep the full landmark list for the viewer overlay; the pointer only
        # needs the nose tip.
        self.latest_landmarks = [(p.x, p.y) for p in lm]
        nose = lm[_NOSE_TIP]
        nose_nxy = (float(nose.x), float(nose.y))

        matrix = None
        if result.facial_transformation_matrixes:
            matrix = result.facial_transformation_matrixes[0]
        self.latest_matrix = matrix
        fwd = head_forward(matrix)
        if fwd is None:
            return HeadSample(ok=False, nose_nxy=nose_nxy)
        return HeadSample(ok=True, forward=fwd, nose_nxy=nose_nxy)

    def get_status(self) -> dict:
        return {
            "available": self._available,
            "frame_count": self._frame_count,
            "has_face": self.latest_landmarks is not None,
        }
