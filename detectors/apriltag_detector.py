import numpy as np

import util.state as state
from util.vision_types import TagObservation


def find_corners(image):
    # Detect AprilTags in the image
    detections = state.apriltag3_detector.detect(image)
    if len(detections) == 0:
        return []

    # change corner order to match aruco
    result = []
    for detection in detections:
        if detection.decision_margin < state.settings.apriltag3.decision_margin:
            continue
        ordered_corners = np.vstack((detection.corners[2:], detection.corners[:2]))[
            ::-1
        ]
        result.append(
            TagObservation(
                detection.tag_id,
                ordered_corners.reshape((1, 4, 2)).astype(np.float32),
            )
        )
    return result
