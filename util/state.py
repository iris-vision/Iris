import enum
import json
import logging
import os
import platform
import shutil
import subprocess
import tomllib

import cv2
import pyapriltags
from dynaconf import Dynaconf, loaders
from dynaconf.utils.boxing import DynaBox
from platformdirs import site_config_dir
from wpimath.geometry import Pose3d, Quaternion, Rotation3d, Translation3d

from util.vision_types import TagCoordinates


class Platform(enum.Enum):
    IRIS = 1
    DEV = 2
    TEST = 3


exec_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
config_dir = site_config_dir(appname="iris", appauthor="irisvision")
snapshots_dir = os.path.join(config_dir, "snapshots")

logging.basicConfig(
    level="INFO",
    format="%(asctime)s: [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Iris")

logger.info("Logger initialized")


def load_calibration(settings, name="current"):
    """Hook to load calibration data into settings."""
    if not os.path.exists(os.path.join(config_dir, "calibration", "current")):
        shutil.copytree(
            os.path.join(exec_dir, "calibration", "default"),
            os.path.join(config_dir, "calibration", "current"),
            dirs_exist_ok=True,
        )

    with open(
        os.path.join(
            config_dir,
            "calibration",
            name,
            "calibration.toml",
        ),
        "rb",
    ) as c:
        calibration = tomllib.load(c)
    print(calibration)
    return {"calibration": calibration}


def reload_calibration():
    settings["calibration"] = load_calibration(settings)["calibration"]


# TODO: fix
def get_git_info():
    try:
        # Run the git command to get the latest tag description
        last_tagged_version = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        current_commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        return last_tagged_version, current_commit

    except subprocess.CalledProcessError as e:
        logging.error(
            f"An error occurred while fetching the git description: {e.stderr}"
        )
        return "Unknown", "Unknown"
    except IndexError:
        logging.error("Invalid git description format")
        return "Unknown", "Unknown"


def get_platform():
    release = platform.release()
    if "rk2312" in release:
        return Platform.IRIS
    if release == "6.1.0-1025-rockchip":
        return Platform.TEST

    return Platform.DEV


def get_device_id():
    return subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()


if os.path.exists(os.path.join(exec_dir, "config.toml")):
    config_path = "config.toml"
else:
    config_path = "default_config.toml"

settings = Dynaconf(
    envvar_prefix="DYNACONF",
    settings_files=[config_path],
    post_hooks=[load_calibration],
)

version, git_hash = get_git_info()
current_platform: Platform = get_platform()
device_id: str = get_device_id()

logger.info("Load Configuration Successful")

nt_listener = None

logger.info("NT server started")


# not constants
def get_apriltag3_detector() -> pyapriltags.Detector:
    return pyapriltags.Detector(
        families=settings.apriltag3.families,
        nthreads=settings.apriltag3.threads,
        quad_decimate=settings.apriltag3.quad_decimate,
        quad_sigma=settings.apriltag3.quad_sigma,
        refine_edges=settings.apriltag3.refine_edges,
        decode_sharpening=settings.apriltag3.decode_sharpening,
    )


def get_aruco_dict_from_string(
    family: str = settings.apriltag3.families,
) -> cv2.aruco.Dictionary:
    match family:
        case "tag36h11":
            return cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
        case "tag25h9":
            return cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_25h9)
        case "tag16h5":
            return cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_16h5)
        case _:
            return cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)


def get_aruco_detection_params() -> cv2.aruco.DetectorParameters:
    aruco_detection_params = cv2.aruco.DetectorParameters()
    aruco_detection_params.useAruco3Detection = settings.aruco.aruco3
    aruco_detection_params.aprilTagQuadDecimate = settings.apriltag3.quad_decimate
    aruco_detection_params.cornerRefinementMethod = (
        cv2.aruco.CORNER_REFINE_SUBPIX
        if settings.aruco.corner_refinement
        else cv2.aruco.CORNER_REFINE_NONE
    )
    aruco_detection_params.relativeCornerRefinmentWinSize = (
        settings.aruco.relative_refinement_window
    )
    aruco_detection_params.cornerRefinementWinSize = (
        settings.aruco.max_refinement_window
    )
    return aruco_detection_params


apriltag3_detector = get_apriltag3_detector()

aruco_dict = get_aruco_dict_from_string()
aruco_detection_params = get_aruco_detection_params()

detector_update_needed = False

last_frame = None
filtered_detections = None
ignored_detections = None

last_frame_time = 0.0
fps: float = 0.0
frame_times: list[float] = []

poses = []
tags2d = []
last_logged_timestamp = 0.0
new_data = False
bad_frames = 0

robot_last_enabled = False

# Define apriltag locations
apriltag_size = 0.1651  # 36h11

m = open(os.path.join(exec_dir, settings.map), "r")
tags = json.load(m)["tags"]
m.close()

tag_world_coords = {}

ignored_tags = []

for tag in tags:
    tag_pose = Pose3d(
        Translation3d(
            tag["pose"]["translation"]["x"],
            tag["pose"]["translation"]["y"],
            tag["pose"]["translation"]["z"],
        ),
        Rotation3d(
            Quaternion(
                tag["pose"]["rotation"]["quaternion"]["W"],
                tag["pose"]["rotation"]["quaternion"]["X"],
                tag["pose"]["rotation"]["quaternion"]["Y"],
                tag["pose"]["rotation"]["quaternion"]["Z"],
            )
        ),
    )
    tag_world_coords[tag["ID"]] = TagCoordinates(tag_pose, apriltag_size)

logger.info("Load Field Map Successful")


def get_server_ip():
    return (
        "10."
        + str(settings.team_number // 100)
        + "."
        + str(settings.team_number % 100)
        + ".2"
    )


# condense config variables into a json
def is_serializable(v):
    try:
        json.dumps(v)
        return True
    except (TypeError, OverflowError):
        return False


def to_json():
    # Filter out non-serializable items, functions, built-ins, and modules
    log_exclude = [
        "settings",
        "tag",
        "last_frame",
        "detections",
        "poses",
        "new_data",
        "log_exclude",
        "fps",
        "tags",
        "bad_frames",
        "ignored_tags",
    ]
    module_vars = {
        k: v
        for k, v in globals().items()
        if k not in log_exclude
        and not k.startswith("__")
        and not callable(v)
        and is_serializable(v)
    }

    return json.dumps(module_vars, indent=4)


def save_settings():
    log_exclude = ["CALIBRATION"]

    out = {
        k.lower(): v
        for k, v in settings.as_dict().items()
        if k not in log_exclude
        and not k.startswith("__")
        and not callable(v)
        and is_serializable(v)
    }
    loaders.write(os.path.join(exec_dir, "config.toml"), DynaBox(out).to_dict())


config_json = to_json()
save_settings()
