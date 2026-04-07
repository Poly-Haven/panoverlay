from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class ImageNode:
    image_id: int
    yaw: float
    pitch: float
    x_ratio: float
    y_ratio: float


@dataclass(frozen=True)
class PairRelationship:
    image_1: int
    image_2: int
    count: int
    average_distance: float


@dataclass(frozen=True)
class OverlayModel:
    project_path: Path
    images: list[ImageNode]
    pairs: list[PairRelationship]


def rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=float)


def rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=float)


def rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=float)


def make_rotation(params):
    yaw = math.radians(params["yaw"])
    pitch = math.radians(params["pitch"])
    roll = math.radians(params["roll"])
    return rot_y(yaw) @ rot_x(-pitch) @ rot_z(-roll)


def principal_point(size, shift):
    w, h = size
    diagonal = math.hypot(w, h)
    dx = shift["shortside"] * diagonal
    dy = shift["longside"] * diagonal
    return (w - 1) / 2 + dx, (h - 1) / 2 - dy


def make_camera(group, lens):
    w, h = group["size"]
    position = group["position"]["params"]
    lens_params = lens["lens"]["params"]
    f_mm = lens_params["focallength"]
    sensor_diag = lens_params["sensordiagonal"]
    shift = lens["shift"]["params"]
    cx, cy = principal_point((w, h), shift)
    return {
        "w": w,
        "h": h,
        "f": f_mm * math.hypot(w, h) / sensor_diag,
        "cx": cx,
        "cy": cy,
        "a": lens_params["a"],
        "b": lens_params["b"],
        "c": lens_params["c"],
        "scale": min(w, h) / 2,
        "R": make_rotation(position),
    }


def distortion_factor(r, cam):
    d = 1 - (cam["a"] + cam["b"] + cam["c"])
    return cam["a"] * r**3 + cam["b"] * r**2 + cam["c"] * r + d


def undistort_xy(x, y, cam):
    rd = math.hypot(x, y) / cam["scale"]
    if rd == 0:
        return x, y

    ru = rd
    d = 1 - (cam["a"] + cam["b"] + cam["c"])
    for _ in range(12):
        value = (cam["a"] * ru**3 + cam["b"] * ru**2 + cam["c"] * ru + d) * ru - rd
        deriv = 4 * cam["a"] * ru**3 + 3 * cam["b"] * ru**2 + 2 * cam["c"] * ru + d
        step = value / deriv
        ru -= step
        if abs(step) < 1e-12:
            break

    return x * (ru / rd), y * (ru / rd)


def distort_xy(x, y, cam):
    ru = math.hypot(x, y) / cam["scale"]
    if ru == 0:
        return x, y

    rd = ru * distortion_factor(ru, cam)
    return x * (rd / ru), y * (rd / ru)


def pixel_to_camera_ray(cam, x, y):
    xd = x - cam["cx"]
    yd = cam["cy"] - y
    xu, yu = undistort_xy(xd, yd, cam)
    ray = np.array([xu / cam["f"], yu / cam["f"], 1.0], dtype=float)
    return ray / np.linalg.norm(ray)


def camera_ray_to_pixel(cam, ray_cam):
    xu = cam["f"] * (ray_cam[0] / ray_cam[2])
    yu = cam["f"] * (ray_cam[1] / ray_cam[2])
    xd, yd = distort_xy(xu, yu, cam)
    x = cam["cx"] + xd
    y = cam["cy"] - yd
    return x, y


def load_project(project_file: str | Path):
    payload = json.loads(Path(project_file).read_text(encoding="utf-8-sig"))
    return payload["project"] if "project" in payload else payload


def normalized_x_from_yaw(yaw: float) -> float:
    return ((yaw / 360.0) + 0.5) % 1.0


def normalized_y_from_pitch(pitch: float) -> float:
    return max(0.0, min(1.0, 0.5 - pitch / 180.0))


def yaw_pitch_to_equirectangular(yaw: float, pitch: float, width: float, height: float) -> tuple[float, float]:
    return normalized_x_from_yaw(yaw) * width, normalized_y_from_pitch(pitch) * height


def has_viewport_correction(group) -> bool:
    params = group["position"]["params"]
    return any(abs(float(params.get(name, 0.0))) > 1e-9 for name in ("vpx", "vpy", "vpd"))


def build_image_nodes(project) -> list[ImageNode]:
    nodes = []
    for image_id, group in enumerate(project["imagegroups"], start=1):
        if has_viewport_correction(group):
            continue
        params = group["position"]["params"]
        yaw = float(params["yaw"])
        pitch = float(params["pitch"])
        nodes.append(
            ImageNode(
                image_id=image_id,
                yaw=yaw,
                pitch=pitch,
                x_ratio=normalized_x_from_yaw(yaw),
                y_ratio=normalized_y_from_pitch(pitch),
            )
        )
    return nodes


def compute_control_point_distances(project):
    lens = project["globallenses"][0]
    cameras = [make_camera(group, lens) for group in project["imagegroups"]]
    ignored_indices = {
        image_index for image_index, group in enumerate(project["imagegroups"]) if has_viewport_correction(group)
    }
    rows = []

    for cp in project["controlpoints"]:
        if cp.get("t", 0) != 0:
            continue

        first = cp["0"]
        second = cp["1"]
        if first[0] <= second[0]:
            i1, _, x1, y1 = first
            i2, _, x2, y2 = second
        else:
            i1, _, x1, y1 = second
            i2, _, x2, y2 = first

        if i1 in ignored_indices or i2 in ignored_indices:
            continue

        source_ray_world = cameras[i1]["R"] @ pixel_to_camera_ray(cameras[i1], x1, y1)
        target_ray_cam = cameras[i2]["R"].T @ source_ray_world
        if target_ray_cam[2] <= 0:
            distance = float("inf")
        else:
            projected = camera_ray_to_pixel(cameras[i2], target_ray_cam)
            distance = math.hypot(projected[0] - x2, projected[1] - y2)

        rows.append(
            {
                "image_1": i1 + 1,
                "image_2": i2 + 1,
                "type": "Normal",
                "distance": distance,
            }
        )

    return rows


def aggregate_pair_relationships(project) -> list[PairRelationship]:
    grouped: dict[tuple[int, int], list[float]] = {}
    for row in compute_control_point_distances(project):
        key = (row["image_1"], row["image_2"])
        grouped.setdefault(key, []).append(row["distance"])

    relationships = []
    for (image_1, image_2), distances in sorted(grouped.items()):
        finite_distances = [value for value in distances if math.isfinite(value)]
        average_distance = sum(finite_distances) / len(finite_distances) if finite_distances else float("inf")
        relationships.append(
            PairRelationship(
                image_1=image_1,
                image_2=image_2,
                count=len(distances),
                average_distance=average_distance,
            )
        )

    return relationships


def load_overlay_model(project_file: str | Path) -> OverlayModel:
    project_path = Path(project_file)
    project = load_project(project_path)
    return OverlayModel(
        project_path=project_path,
        images=build_image_nodes(project),
        pairs=aggregate_pair_relationships(project),
    )


def format_distance(value: float):
    if not math.isfinite(value):
        return None
    rounded = round(value, 1)
    return int(rounded) if float(rounded).is_integer() else rounded


def relationships_to_rows(relationships: list[PairRelationship]) -> list[dict[str, int | float | None]]:
    rows = []
    for relationship in sorted(
        relationships,
        key=lambda item: (item.count, item.average_distance if math.isfinite(item.average_distance) else float("inf")),
        reverse=True,
    ):
        rows.append(
            {
                "image_1": relationship.image_1,
                "image_2": relationship.image_2,
                "count": relationship.count,
                "average_distance": format_distance(relationship.average_distance),
            }
        )
    return rows
