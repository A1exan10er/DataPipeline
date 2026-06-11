import pybullet as p
import pybullet_data
import argparse
import csv
import json
import time
from pathlib import Path

import numpy as np

# Automatically pull URDF paths for our robot pool
from robot_descriptions import panda_description, ur5e_description
from robot_descriptions._xacro import get_urdf_path


DEFAULT_SAMPLE_ROOT = Path(__file__).resolve().parent / "test_sample" / "test_sample"
POSE_COLUMN_GROUPS = {
    "left": ["left_x", "left_y", "left_z", "left_r1", "left_r2", "left_r3", "left_r4", "left_r5", "left_r6"],
    "right": ["right_x", "right_y", "right_z", "right_r1", "right_r2", "right_r3", "right_r4", "right_r5", "right_r6"],
}

# ==========================================
# 1. Configuration and Robot Pool
# ==========================================

# We map a friendly name to its URDF path and its end-effector link index.
ROBOT_POOL = {
    "Franka_Panda": {
        "urdf": get_urdf_path(panda_description),
        "ee_index": 8 
    },
    "Universal_Robots_UR5e": {
        "urdf": get_urdf_path(ur5e_description),
        "ee_index": 7
    }
}

def rotation_6d_to_matrix(rotation_6d):
    """Convert a 6D rotation representation into a 3x3 rotation matrix."""
    first = np.asarray(rotation_6d[:3], dtype=float)
    second = np.asarray(rotation_6d[3:6], dtype=float)

    first_norm = np.linalg.norm(first)
    second_norm = np.linalg.norm(second)
    if first_norm < 1e-8 or second_norm < 1e-8:
        raise ValueError("Invalid 6D rotation: one of the basis vectors is near zero")

    x_axis = first / first_norm
    y_axis = second - np.dot(x_axis, second) * x_axis
    y_axis_norm = np.linalg.norm(y_axis)
    if y_axis_norm < 1e-8:
        raise ValueError("Invalid 6D rotation: basis vectors are nearly collinear")

    y_axis /= y_axis_norm
    z_axis = np.cross(x_axis, y_axis)
    return np.column_stack((x_axis, y_axis, z_axis))


def rotation_matrix_to_quaternion(rotation_matrix):
    """Convert a rotation matrix to a PyBullet-compatible quaternion [x, y, z, w]."""
    m00, m01, m02 = rotation_matrix[0]
    m10, m11, m12 = rotation_matrix[1]
    m20, m21, m22 = rotation_matrix[2]
    trace = m00 + m11 + m22

    if trace > 0.0:
        scale = np.sqrt(trace + 1.0) * 2.0
        qw = 0.25 * scale
        qx = (m21 - m12) / scale
        qy = (m02 - m20) / scale
        qz = (m10 - m01) / scale
    elif m00 > m11 and m00 > m22:
        scale = np.sqrt(1.0 + m00 - m11 - m22) * 2.0
        qw = (m21 - m12) / scale
        qx = 0.25 * scale
        qy = (m01 + m10) / scale
        qz = (m02 + m20) / scale
    elif m11 > m22:
        scale = np.sqrt(1.0 + m11 - m00 - m22) * 2.0
        qw = (m02 - m20) / scale
        qx = (m01 + m10) / scale
        qy = 0.25 * scale
        qz = (m12 + m21) / scale
    else:
        scale = np.sqrt(1.0 + m22 - m00 - m11) * 2.0
        qw = (m10 - m01) / scale
        qx = (m02 + m20) / scale
        qy = (m12 + m21) / scale
        qz = 0.25 * scale

    quaternion = np.array([qx, qy, qz, qw], dtype=float)
    return quaternion / np.linalg.norm(quaternion)


def quaternion_to_rotation_matrix(quaternion):
    """Convert a quaternion [x, y, z, w] into a 3x3 rotation matrix."""
    qx, qy, qz, qw = np.asarray(quaternion, dtype=float)
    norm = np.linalg.norm([qx, qy, qz, qw])
    if norm < 1e-8:
        raise ValueError("Invalid quaternion: norm is near zero")
    qx, qy, qz, qw = qx / norm, qy / norm, qz / norm, qw / norm

    return np.array(
        [
            [1.0 - 2.0 * (qy * qy + qz * qz), 2.0 * (qx * qy - qz * qw), 2.0 * (qx * qz + qy * qw)],
            [2.0 * (qx * qy + qz * qw), 1.0 - 2.0 * (qx * qx + qz * qz), 2.0 * (qy * qz - qx * qw)],
            [2.0 * (qx * qz - qy * qw), 2.0 * (qy * qz + qx * qw), 1.0 - 2.0 * (qx * qx + qy * qy)],
        ],
        dtype=float,
    )


def load_episode_start_pose(episode_root, side):
    """Load the absolute start pose for a side from metadata.json, if present."""
    metadata_path = episode_root / "metadata.json"
    if not metadata_path.exists():
        return None

    with metadata_path.open() as handle:
        metadata = json.load(handle)

    side_metadata = metadata.get("coordinate_reference", {}).get("sides", {}).get(side)
    if not side_metadata:
        return None

    pose = side_metadata.get("pose", {})
    required_keys = ["tcp.x", "tcp.y", "tcp.z", "tcp.qx", "tcp.qy", "tcp.qz", "tcp.qw"]
    if not all(key in pose for key in required_keys):
        return None

    return {
        "translation": np.array([pose["tcp.x"], pose["tcp.y"], pose["tcp.z"]], dtype=float),
        "rotation": quaternion_to_rotation_matrix(
            [pose["tcp.qx"], pose["tcp.qy"], pose["tcp.qz"], pose["tcp.qw"]]
        ),
    }


def load_eef_pose_trajectory(csv_path, side, start_pose=None):
    """Load a left or right eef_pose trajectory from a raw UMI CSV file."""
    if side not in POSE_COLUMN_GROUPS:
        raise ValueError(f"Unsupported side '{side}'")

    trajectory = []
    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        required_columns = ["timestamp_ms", *POSE_COLUMN_GROUPS[side]]
        fieldnames = reader.fieldnames or []
        missing_columns = [column for column in required_columns if column not in fieldnames]
        if missing_columns:
            raise ValueError(f"Missing columns in {csv_path}: {missing_columns}")

        for row in reader:
            rotation_6d = [float(row[f"{side}_r{i}"]) for i in range(1, 7)]
            relative_rotation = rotation_6d_to_matrix(rotation_6d)
            relative_translation = np.array(
                [float(row[f"{side}_x"]), float(row[f"{side}_y"]), float(row[f"{side}_z"])],
                dtype=float,
            )

            if start_pose is not None:
                absolute_rotation = start_pose["rotation"] @ relative_rotation
                absolute_translation = start_pose["translation"] + start_pose["rotation"] @ relative_translation
            else:
                absolute_rotation = relative_rotation
                absolute_translation = relative_translation

            trajectory.append(
                {
                    "timestamp_ms": int(float(row["timestamp_ms"])),
                    "pos": absolute_translation.tolist(),
                    "quat": rotation_matrix_to_quaternion(absolute_rotation),
                }
            )

    return trajectory


def load_episode_pose_streams(episode_root):
    """Load left and right pose streams from an episode directory."""
    pose_dir = episode_root / "observation.state.eef_pose"
    csv_path = pose_dir / "data_raw.csv"
    if not csv_path.exists():
        csv_path = pose_dir / "data.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Missing pose stream: {pose_dir / 'data_raw.csv'}")

    return {
        side: load_eef_pose_trajectory(csv_path, side, load_episode_start_pose(episode_root, side))
        for side in POSE_COLUMN_GROUPS
    }


# ==========================================
# 2. Core Validation Logic
# ==========================================

def quaternion_error_radians(actual_quat, target_quat):
    """Return the angular difference between two quaternions in radians."""
    actual = np.asarray(actual_quat, dtype=float)
    target = np.asarray(target_quat, dtype=float)
    actual /= np.linalg.norm(actual)
    target /= np.linalg.norm(target)
    dot = np.clip(abs(np.dot(actual, target)), -1.0, 1.0)
    return 2.0 * np.arccos(dot)


def validate_trajectory_for_pool(trajectory, robot_pool, *, position_tolerance=0.01, orientation_tolerance_radians=0.2):
    """Iterate through robots until one can execute the full trajectory."""
    
    # Start PyBullet in headless mode (DIRECT) for maximum compute speed
    physics_client = p.connect(p.DIRECT)
    p.setAdditionalSearchPath(pybullet_data.getDataPath())

    try:
        match_found = None

        for robot_name, robot_data in robot_pool.items():
            p.resetSimulation()

            # Load the robot into the math engine.
            robot_id = p.loadURDF(robot_data["urdf"], useFixedBase=True)
            ee_index = robot_data["ee_index"]

            is_viable = True

            for frame in trajectory:
                target_pos = frame["pos"]
                target_quat = frame["quat"]

                # Use the current pose as the seed for the next IK solve.
                joint_poses = p.calculateInverseKinematics(
                    bodyUniqueId=robot_id,
                    endEffectorLinkIndex=ee_index,
                    targetPosition=target_pos,
                    targetOrientation=target_quat,
                    maxNumIterations=100,
                    residualThreshold=1e-4,
                )

                num_joints = p.getNumJoints(robot_id)
                joint_index = 0
                for i in range(num_joints):
                    joint_info = p.getJointInfo(robot_id, i)
                    if joint_info[2] != p.JOINT_FIXED:
                        p.resetJointState(robot_id, i, joint_poses[joint_index])
                        joint_index += 1

                actual_state = p.getLinkState(robot_id, ee_index)
                actual_pos = actual_state[0]
                actual_quat = actual_state[1]

                distance_error = np.linalg.norm(np.array(target_pos) - np.array(actual_pos))
                orientation_error = quaternion_error_radians(actual_quat, target_quat)
                if distance_error > position_tolerance or orientation_error > orientation_tolerance_radians:
                    is_viable = False
                    break

            if is_viable:
                match_found = robot_name
                break

        return match_found
    finally:
        p.disconnect()


def validate_episode_directory(episode_root):
    """Validate both pose streams for a single episode directory."""
    streams = load_episode_pose_streams(episode_root)
    results = {}
    for side, trajectory in streams.items():
        results[side] = {
            "frames": len(trajectory),
            "robot": validate_trajectory_for_pool(trajectory, ROBOT_POOL),
        }
    return results


def discover_episode_directories(sample_root):
    return sorted(path for path in sample_root.glob("episode_*") if path.is_dir())


def main():
    parser = argparse.ArgumentParser(description="Validate UMI eef_pose streams via inverse kinematics.")
    parser.add_argument(
        "--sample-root",
        type=Path,
        default=DEFAULT_SAMPLE_ROOT,
        help="Root folder containing episode_* directories.",
    )
    args = parser.parse_args()

    sample_root = args.sample_root
    episode_dirs = discover_episode_directories(sample_root)

    if not episode_dirs:
        print(f"No episode directories found under {sample_root}")
        return 1

    print(f"Validating {len(episode_dirs)} episodes from {sample_root}...")
    start_time = time.time()

    overall_success = True
    for episode_dir in episode_dirs:
        episode_results = validate_episode_directory(episode_dir)
        print("-" * 30)
        print(episode_dir.name)
        for side, result in episode_results.items():
            if result["robot"]:
                print(f"  {side}: SUCCESS for {result['robot']} ({result['frames']} frames)")
            else:
                overall_success = False
                print(f"  {side}: FAILURE ({result['frames']} frames)")

    end_time = time.time()

    print("-" * 30)
    if overall_success:
        print("Result: SUCCESS. All pose streams were viable for at least one robot in the pool.")
    else:
        print("Result: FAILURE. At least one pose stream could not be executed by the robot pool.")
    print(f"Performance: Evaluated in {end_time - start_time:.4f} seconds.")
    print("-" * 30)
    return 0 if overall_success else 2


if __name__ == "__main__":
    raise SystemExit(main())
