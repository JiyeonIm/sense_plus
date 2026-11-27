import numpy as np


def get_center_depth(depth_map, roi_ratio=0.1):

    height, width = depth_map.shape

    center_x = width // 2
    center_y = height // 2

    half_w = max(1, int(width * roi_ratio / 2))
    half_h = max(1, int(height * roi_ratio / 2))

    roi = depth_map[
        center_y - half_h:center_y + half_h,
        center_x - half_w:center_x + half_w
    ]

    valid = roi[
        np.isfinite(roi) & (roi > 0)
    ]

    if valid.size == 0:
        return None

    return float(np.median(valid))


def summarize_depth(depth_map):

    height, width = depth_map.shape

    regions = {
        "left": depth_map[:, :width // 3],
        "center": depth_map[:, width // 3:width * 2 // 3],
        "right": depth_map[:, width * 2 // 3:]
    }

    summary = {}

    for name, region in regions.items():

        # 유효한 깊이 값만 사용
        valid = region[
            np.isfinite(region) & (region > 0)
        ]

        if valid.size == 0:
            summary[name] = None
            continue

        summary[name] = {
            "near_m": round(
                float(np.percentile(valid, 10)), 2
            ),
            "median_m": round(
                float(np.median(valid)), 2
            )
        }

    return summary