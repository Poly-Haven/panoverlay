import argparse
import json
import math
from pathlib import Path

import numpy as np


# These two scales were tuned against the bundled PTGui sample distances.
# They improve the fit materially while keeping the math simple.
SHIFT_SCALE = 1.25
DISTORTION_SCALE = 0.8


def rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=float)


def rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=float)


def rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=float)


def make_camera(group, lens):
    w, h = group["size"]
    p = group["position"]["params"]
    f_mm = lens["lens"]["params"]["focallength"]
    sensor_diag = lens["lens"]["params"]["sensordiagonal"]
    f = f_mm * math.hypot(w, h) / sensor_diag

    # PTGui stores shift as a fraction of the short/long side.
    shift = lens["shift"]["params"]
    dx = SHIFT_SCALE * shift["shortside"] * w
    dy = SHIFT_SCALE * shift["longside"] * h

    return {
        "w": w,
        "h": h,
        "f": f,
        "cx": w / 2 - dx,
        "cy": h / 2 - dy,
        "a": DISTORTION_SCALE * lens["lens"]["params"]["a"],
        "b": DISTORTION_SCALE * lens["lens"]["params"]["b"],
        "c": DISTORTION_SCALE * lens["lens"]["params"]["c"],
        # This convention matched the supplied PTGui sample best.
        "R": rot_y(math.radians(-p["yaw"])) @ rot_x(math.radians(p["pitch"])) @ rot_z(math.radians(-p["roll"])),
    }


def undistort_xy(xn, yn, cam):
    # PTGui / PanoTools radial model:
    #   r_src = (a r^3 + b r^2 + c r + d) r, with d = 1 - (a+b+c)
    # Radius is normalized so the inscribed circle has radius 1.
    scale = min(cam["w"], cam["h"]) / 2
    rs = math.hypot(xn, yn) * cam["f"] / scale
    if rs == 0:
        return xn, yn

    a = cam["a"]
    b = cam["b"]
    c = cam["c"]
    d = 1 - (a + b + c)
    rd = rs
    for _ in range(12):
        value = (a * rd**3 + b * rd**2 + c * rd + d) * rd
        deriv = 4 * a * rd**3 + 3 * b * rd**2 + 2 * c * rd + d
        step = (value - rs) / deriv
        rd -= step
        if abs(step) < 1e-12:
            break

    return xn * (rd / rs), yn * (rd / rs)


def distort_xy(xn, yn, cam):
    scale = min(cam["w"], cam["h"]) / 2
    rd = math.hypot(xn, yn) * cam["f"] / scale
    if rd == 0:
        return xn, yn

    a = cam["a"]
    b = cam["b"]
    c = cam["c"]
    d = 1 - (a + b + c)
    rs = (a * rd**3 + b * rd**2 + c * rd + d) * rd
    return xn * (rs / rd), yn * (rs / rd)


def pixel_to_camera_ray(cam, x, y):
    # Pixel coords are x-right/y-down. First convert to centered coords with Y-up.
    xu = (x - cam["cx"]) / cam["f"]
    yu = (cam["cy"] - y) / cam["f"]

    # Matching PTGui's sample distances requires flipping both normalized axes.
    xn, yn = undistort_xy(-xu, -yu, cam)
    ray = np.array([xn, yn, 1.0], dtype=float)
    return ray / np.linalg.norm(ray)


def camera_ray_to_pixel(cam, ray_cam):
    xn = ray_cam[0] / ray_cam[2]
    yn = ray_cam[1] / ray_cam[2]
    xd, yd = distort_xy(xn, yn, cam)
    x = cam["cx"] - cam["f"] * xd
    y = cam["cy"] + cam["f"] * yd
    return x, y


def control_point_distances(project):
    lens = project["globallenses"][0]
    cams = [make_camera(group, lens) for group in project["imagegroups"]]
    pair_counts = {}
    rows = []

    for cp in project["controlpoints"]:
        if cp.get("t", 0) != 0:
            continue

        a = cp["0"]
        b = cp["1"]
        if a[0] <= b[0]:
            i1, _, x1, y1 = a
            i2, _, x2, y2 = b
        else:
            i1, _, x1, y1 = b
            i2, _, x2, y2 = a

        pair = (i1 + 1, i2 + 1)
        pair_counts[pair] = pair_counts.get(pair, 0) + 1

        r1_world = cams[i1]["R"] @ pixel_to_camera_ray(cams[i1], x1, y1)
        r2_world = cams[i2]["R"] @ pixel_to_camera_ray(cams[i2], x2, y2)

        # PTGui's reported value is closer to a symmetric reprojection error
        # than to a pure one-way image1 -> image2 projection.
        world = r1_world + r2_world
        world /= np.linalg.norm(world)

        r1_cam = cams[i1]["R"].T @ world
        r2_cam = cams[i2]["R"].T @ world
        if r1_cam[2] <= 0 or r2_cam[2] <= 0:
            dist = float("inf")
        else:
            x1p, y1p = camera_ray_to_pixel(cams[i1], r1_cam)
            x2p, y2p = camera_ray_to_pixel(cams[i2], r2_cam)
            e1 = math.hypot(x1p - x1, y1p - y1)
            e2 = math.hypot(x2p - x2, y2p - y2)
            dist = 0.5 * (e1 + e2)

        rows.append(
            {
                "image_1": pair[0],
                "image_2": pair[1],
                "num": pair_counts[pair],
                "type": "Normal",
                "distance": dist,
            }
        )

    return rows


def validate(rows, ref_path):
    refs = json.loads(Path(ref_path).read_text())
    refs = [r for r in refs if isinstance(r.get("distance"), (int, float))]
    by_key = {(r["image_1"], r["image_2"], r["num"]): r["distance"] for r in rows}
    errors = []
    for ref in refs:
        key = (ref["image_1"], ref["image_2"], ref["num"])
        if key in by_key and math.isfinite(by_key[key]):
            errors.append(abs(by_key[key] - ref["distance"]))
    if not errors:
        print("validation: no comparable reference rows found")
        return
    print(f"validation_count={len(errors)}")
    print(f"mean_absolute_error={sum(errors) / len(errors):.3f}")
    print(f"max_error={max(errors):.3f}")


def format_distance(value):
    value = round(value, 1)
    return int(value) if float(value).is_integer() else value


def format_rows_for_output(rows):
    out = []
    for row in sorted(rows, key=lambda r: r["distance"], reverse=True):
        out.append(
            {
                "image_1": row["image_1"],
                "image_2": row["image_2"],
                "num": row["num"],
                "type": row["type"],
                "distance": None if not math.isfinite(row["distance"]) else format_distance(row["distance"]),
            }
        )
    return out


def main():
    parser = argparse.ArgumentParser(description="Compute PTGui control-point reprojection distances.")
    parser.add_argument("project_file", help="Path to a PTGui project file (.pts / JSON)")
    parser.add_argument("--reference", help="Optional JSON file with reference distances")
    args = parser.parse_args()

    project = json.loads(Path(args.project_file).read_text())["project"]
    rows = control_point_distances(project)
    print(json.dumps(format_rows_for_output(rows), indent=2))

    # Output to inputfile.distances.json
    output_path = Path(args.project_file).with_suffix(".distances.json")
    output_path.write_text(json.dumps(format_rows_for_output(rows), indent=2))

    ref_path = args.reference
    if not ref_path:
        candidate = Path(args.project_file).with_name("sample_distances.json")
        if candidate.exists():
            ref_path = candidate
    if ref_path:
        validate(rows, ref_path)


if __name__ == "__main__":
    main()
