import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation as R


CONFIG_PATH = Path(__file__).with_name("ee_trajectory_config.json")
EEF_POSE_DIR = "observation.state.eef_pose"
ROTATION_COLUMNS = ("r1", "r2", "r3", "r4", "r5", "r6")
SIDES = ("left", "right")


def load_config(config_path=None):
    path = Path(config_path) if config_path is not None else CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"Transform config does not exist: {path}")

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)



def save_config(config, config_path=None):
    path = Path(config_path) if config_path is not None else CONFIG_PATH
    with path.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def build_align_rotation(config):
    rotation = R.identity()
    for step in config["rotation_sequence"]:
        axis = step["axis"].lower()
        if axis not in {"x", "y", "z"}:
            raise ValueError(f"Unsupported rotation axis: {axis}")
        rotation = rotation * R.from_euler(axis, float(step["degrees"]), degrees=True)
    return rotation


def build_rotation_sequence(steps):
    rotation = R.identity()
    for step in steps:
        axis = step["axis"].lower()
        if axis not in {"x", "y", "z"}:
            raise ValueError(f"Unsupported rotation axis: {axis}")
        rotation = rotation * R.from_euler(axis, float(step["degrees"]), degrees=True)
    return rotation


def position_offset(config):
    offset = config["position_offset_m"]
    return np.array(
        [float(offset.get("x", 0.0)), float(offset.get("y", 0.0)), float(offset.get("z", 0.0))],
        dtype=np.float64,
    )


def right_position_offset(config):
    offset = config.get("right_position_offset_m", {})
    return np.array(
        [float(offset.get("x", 0.0)), float(offset.get("y", 0.0)), float(offset.get("z", 0.0))],
        dtype=np.float64,
    )


def local_eef_vector(config):
    local = config.get("local_ee_projection", {}).get("tracker_based_ee_local_position", {})
    return np.array(
        [float(local.get("x", 0.0)), float(local.get("y", 0.0)), float(local.get("z", 0.0))],
        dtype=np.float64,
    )


def tracker_axes_in_eef_matrix(config):
    axes = config.get("local_ee_projection", {}).get(
        "tracker_axes_in_eef_frame",
        {"x": [1.0, 0.0, 0.0], "y": [0.0, 1.0, 0.0], "z": [0.0, 0.0, 1.0]},
    )
    matrix = np.column_stack(
        (
            np.asarray(axes["x"], dtype=np.float64),
            np.asarray(axes["y"], dtype=np.float64),
            np.asarray(axes["z"], dtype=np.float64),
        )
    )
    validate_rotation_matrix(matrix, "tracker_axes_in_eef_frame")
    return matrix


def world_axes_in_transformed_matrix(config):
    axes = config.get("world_projection", {}).get(
        "world_axes_in_transformed_frame",
        {"x": [0.0, 0.0, 1.0], "y": [0.0, -1.0, 0.0], "z": [1.0, 0.0, 0.0]},
    )
    matrix = np.column_stack(
        (
            np.asarray(axes["x"], dtype=np.float64),
            np.asarray(axes["y"], dtype=np.float64),
            np.asarray(axes["z"], dtype=np.float64),
        )
    )
    validate_rotation_matrix(matrix, "world_axes_in_transformed_frame")
    return matrix


def world_position_offset(config):
    offset = config.get("world_projection", {}).get("world_position_offset_m", {})
    return np.array(
        [float(offset.get("x", 0.0)), float(offset.get("y", 0.0)), float(offset.get("z", 0.0))],
        dtype=np.float64,
    )


def zero_rpy_pose_in_transformed_matrix(config):
    sequence = (
        config.get("world_projection", {})
        .get("zero_rpy_pose_in_transformed_frame", {})
        .get("rotation_sequence", [{"axis": "y", "degrees": -90.0}])
    )
    return build_rotation_sequence(sequence).as_matrix()


def zero_world_eef_pose_matrix(config):
    sequence = (
        config.get("world_projection", {})
        .get("zero_world_eef_pose", {})
        .get("rotation_sequence", [{"axis": "y", "degrees": 90.0}])
    )
    return build_rotation_sequence(sequence).as_matrix()


def validate_rotation_matrix(matrix, name):
    if matrix.shape != (3, 3):
        raise ValueError(f"{name} must be a 3x3 matrix")
    should_be_identity = matrix.T @ matrix
    if not np.allclose(should_be_identity, np.eye(3), atol=1e-6):
        raise ValueError(f"{name} is not orthonormal:\n{matrix}")
    determinant = np.linalg.det(matrix)
    if not np.isclose(determinant, 1.0, atol=1e-6):
        raise ValueError(f"{name} must be right-handed, det={determinant:.9f}")


def normalize(vector):
    norm = np.linalg.norm(vector)
    if norm < 1e-9:
        raise ValueError(f"Cannot normalize near-zero vector: {vector}")
    return vector / norm


def rotation_6d_to_matrix(values):
    first_col = np.asarray(values[:3], dtype=np.float64)
    second_col = np.asarray(values[3:6], dtype=np.float64)

    b1 = normalize(first_col)
    second_col = second_col - np.dot(b1, second_col) * b1
    b2 = normalize(second_col)
    b3 = np.cross(b1, b2)

    return np.column_stack((b1, b2, b3))


def matrix_to_rotation_6d(matrix):
    return np.concatenate((matrix[:, 0], matrix[:, 1]))


def transform_pose_to_transformed_tracker(position, rotation_6d, side="left", config=None):
    config = load_config() if config is None else config
    coordinate_rotation = build_align_rotation(config).as_matrix().T
    rotation_matrix = rotation_6d_to_matrix(rotation_6d)

    transformed_position = coordinate_rotation @ np.asarray(position, dtype=np.float64) + position_offset(config)
    if side == "right":
        transformed_position = transformed_position + right_position_offset(config)
    transformed_rotation = coordinate_rotation @ rotation_matrix

    return transformed_position, transformed_rotation


def transform_transformed_tracker_to_world_eef(position, rotation_matrix, config=None, original_tracker_rotation=None):
    config = load_config() if config is None else config

    eef_from_tracker = tracker_axes_in_eef_matrix(config).T
    eef_position_transformed = np.asarray(position, dtype=np.float64) + rotation_matrix @ local_eef_vector(config)

    transformed_from_world = world_axes_in_transformed_matrix(config)
    world_from_transformed = transformed_from_world.T
    eef_position_world = world_from_transformed @ eef_position_transformed + world_position_offset(config)

    if original_tracker_rotation is None:
        original_tracker_rotation = build_align_rotation(config).as_matrix() @ rotation_matrix
    eef_rotation_original = original_tracker_rotation @ eef_from_tracker
    zero_eef_rotation_original = eef_from_tracker
    delta_original_eef = zero_eef_rotation_original.T @ eef_rotation_original
    delta_world = world_from_transformed @ delta_original_eef @ transformed_from_world
    eef_rotation_world = delta_world @ zero_world_eef_pose_matrix(config)

    return eef_position_world, eef_rotation_world


def transform_tracker_pose_to_world_eef_pose(position, rotation_6d, side="left", config=None):
    config = load_config() if config is None else config
    original_tracker_rotation = rotation_6d_to_matrix(rotation_6d)
    transformed_position, transformed_rotation = transform_pose_to_transformed_tracker(
        position, rotation_6d, side=side, config=config
    )
    world_position, world_rotation = transform_transformed_tracker_to_world_eef(
        transformed_position,
        transformed_rotation,
        config=config,
        original_tracker_rotation=original_tracker_rotation,
    )
    return world_position, matrix_to_rotation_6d(world_rotation)


def transform_pose(position, rotation_6d, config=None):
    return transform_tracker_pose_to_world_eef_pose(position, rotation_6d, config=config)


def transform_row(row, config=None):
    config = load_config() if config is None else config
    transformed = dict(row)

    for side in SIDES:
        position_keys = [f"{side}_x", f"{side}_y", f"{side}_z"]
        rotation_keys = [f"{side}_{name}" for name in ROTATION_COLUMNS]

        position = np.array([float(row[key]) for key in position_keys], dtype=np.float64)
        rotation_6d = np.array([float(row[key]) for key in rotation_keys], dtype=np.float64)
        new_position, new_rotation_6d = transform_tracker_pose_to_world_eef_pose(
            position, rotation_6d, side=side, config=config
        )

        for key, value in zip(position_keys, new_position):
            transformed[key] = f"{value:.9f}"
        for key, value in zip(rotation_keys, new_rotation_6d):
            transformed[key] = f"{value:.9f}"

    return transformed


def main():
    config = load_config()
    print(f"Loaded shared transform config: {CONFIG_PATH}")
    print(json.dumps(config, indent=2))
    print()
    print("This file is a shared transform module, not the web visualizer.")
    print("Convert trajectories:")
    print("  python standard/ee_trajectory.py standard/battery")
    print("Open the web visualizer:")
    print("  python standard/visualize_ee_trajectory.py standard/battery")


if __name__ == "__main__":
    main()
