import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np


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


def principal_point(size, shift, focal_pixels):
    w, h = size
    diagonal = math.hypot(w, h)

    # PTGui stores shift on short-side / long-side axes rather than raw x/y.
    # The short-side term aligns with focal-normalized x, while the long-side
    # term aligns with the image diagonal for this portrait project.
    dx = shift["shortside"] * focal_pixels
    dy = shift["longside"] * diagonal
    return (w - 1) / 2 - dx, (h - 1) / 2 - dy


def make_camera(group, lens):
    w, h = group["size"]
    p = group["position"]["params"]
    f_mm = lens["lens"]["params"]["focallength"]
    sensor_diag = lens["lens"]["params"]["sensordiagonal"]
    f = f_mm * math.hypot(w, h) / sensor_diag
    shift = lens["shift"]["params"]
    cx, cy = principal_point((w, h), shift, f)

    return {
        "w": w,
        "h": h,
        "f": f,
        "cx": cx,
        "cy": cy,
        "a": lens["lens"]["params"]["a"],
        "b": lens["lens"]["params"]["b"],
        "c": lens["lens"]["params"]["c"],
        "scale": min(w, h) / 2,
        "R": make_rotation(p),
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


def control_point_distances(project):
    lens = project["globallenses"][0]
    cams = [make_camera(group, lens) for group in project["imagegroups"]]
    pair_counts = {}
    rows = []
    details = {}

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
        num = pair_counts[pair]

        source_ray_world = cams[i1]["R"] @ pixel_to_camera_ray(cams[i1], x1, y1)
        target_ray_cam = cams[i2]["R"].T @ source_ray_world
        if target_ray_cam[2] <= 0:
            dist = float("inf")
            projected = (float("nan"), float("nan"))
        else:
            projected = camera_ray_to_pixel(cams[i2], target_ray_cam)
            dist = math.hypot(projected[0] - x2, projected[1] - y2)

        row = {
            "image_1": pair[0],
            "image_2": pair[1],
            "num": num,
            "type": "Normal",
            "distance": dist,
        }
        rows.append(row)
        details[(pair[0], pair[1], num)] = {
            "source_pixel": (x1, y1),
            "projected_pixel": projected,
            "target_pixel": (x2, y2),
            "distance": dist,
        }

    return rows, details


def load_reference_rows(ref_path):
    refs = json.loads(Path(ref_path).read_text())
    return [r for r in refs if isinstance(r.get("distance"), (int, float))]


def validate(rows, ref_path):
    refs = load_reference_rows(ref_path)
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


def validate_pair(rows, ref_path, image_1, image_2, top_n=10):
    refs = load_reference_rows(ref_path)
    predicted = {
        (r["image_1"], r["image_2"], r["num"]): r["distance"]
        for r in rows
        if r["image_1"] == image_1 and r["image_2"] == image_2
    }
    mismatches = []
    for ref in refs:
        key = (ref["image_1"], ref["image_2"], ref["num"])
        if key in predicted and math.isfinite(predicted[key]):
            mismatches.append(
                {
                    "num": ref["num"],
                    "expected": ref["distance"],
                    "predicted": predicted[key],
                    "abs_error": abs(predicted[key] - ref["distance"]),
                }
            )

    mismatches.sort(key=lambda row: row["abs_error"], reverse=True)
    print(f"pair_validation=({image_1},{image_2})")
    print(f"pair_validation_count={len(mismatches)}")
    if mismatches:
        print(f"pair_mean_absolute_error={sum(row['abs_error'] for row in mismatches) / len(mismatches):.3f}")
        print(f"pair_max_error={mismatches[0]['abs_error']:.3f}")
        print("pair_worst_mismatches=")
        print(json.dumps(mismatches[:top_n], indent=2))
    else:
        print("pair_validation: no comparable rows found")


def write_sample_comparison_csv(rows, ref_path, project_file):
    refs = load_reference_rows(ref_path)
    by_key = {(r["image_1"], r["image_2"], r["num"]): r["distance"] for r in rows}
    csv_rows = []

    for ref in refs:
        key = (ref["image_1"], ref["image_2"], ref["num"])
        if key not in by_key:
            continue
        our_distance = by_key[key]
        csv_rows.append(
            {
                "image_1": ref["image_1"],
                "image_2": ref["image_2"],
                "num": ref["num"],
                "our_distance": "" if not math.isfinite(our_distance) else our_distance,
                "sample_distance": ref["distance"],
            }
        )

    csv_rows.sort(key=lambda row: (row["image_1"], row["image_2"], row["num"]))

    csv_path = Path(project_file).with_suffix(".sample_comparison.csv")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["image_1", "image_2", "num", "our_distance", "sample_distance"],
        )
        writer.writeheader()
        writer.writerows(csv_rows)

    print(f"sample_comparison_csv={csv_path}")


def print_debug_point(details, image_1, image_2, num):
    key = (image_1, image_2, num)
    if key not in details:
        print(f"debug_point_missing={key}")
        return
    detail = details[key]
    print(f"debug_point={key}")
    print(f"source_pixel={detail['source_pixel']}")
    print(f"projected_pixel={detail['projected_pixel']}")
    print(f"target_pixel={detail['target_pixel']}")
    print(f"error={detail['distance']:.6f}")


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
    parser.add_argument(
        "--focus-pair",
        nargs=2,
        type=int,
        metavar=("IMAGE_1", "IMAGE_2"),
        help="Validate a single image pair",
    )
    parser.add_argument(
        "--debug-num",
        type=int,
        help="Print detailed reprojection info for one control point in --focus-pair",
    )
    args = parser.parse_args()

    project = json.loads(Path(args.project_file).read_text())["project"]
    rows, details = control_point_distances(project)
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
        write_sample_comparison_csv(rows, ref_path, args.project_file)
        if args.focus_pair:
            validate_pair(rows, ref_path, args.focus_pair[0], args.focus_pair[1])
    if args.focus_pair and args.debug_num is not None:
        print_debug_point(details, args.focus_pair[0], args.focus_pair[1], args.debug_num)


if __name__ == "__main__":
    main()
