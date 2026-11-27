import cv2
import time
import numpy as np

from models.depth import DepthEstimator


estimator = DepthEstimator()

cap = cv2.VideoCapture(0)

if not cap.isOpened():
    raise RuntimeError("카메라를 열 수 없습니다.")

try:

    while True:

        ret, frame = cap.read()

        if not ret:
            break

        start = time.perf_counter()

        # Depth Estimation
        depth_map = estimator.predict(frame)

        elapsed = time.perf_counter() - start
        fps = 1 / max(elapsed, 1e-6)

        # Depth Map 시각화
        depth_color = estimator.visualize(depth_map)

        height, width = frame.shape[:2]

        cx = width // 2
        cy = height // 2

        # 중앙 21x21 픽셀의 깊이 중앙값
        radius = 10

        region = depth_map[
            max(0, cy - radius):cy + radius + 1,
            max(0, cx - radius):cx + radius + 1
        ]

        valid = region[np.isfinite(region) & (region > 0)]

        center_depth = (
            np.median(valid) if valid.size else float("nan")
        )

        # 원본 이미지와 Depth Map에 정보 표시
        for img in [frame, depth_color]:

            cv2.circle(
                img,
                (cx, cy),
                5,
                (255, 255, 255),
                -1
            )

            cv2.putText(
                img,
                f"Depth: {center_depth:.2f} m",
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (255, 255, 255),
                2
            )

            cv2.putText(
                img,
                f"Inference FPS: {fps:.1f}",
                (20, 80),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (255, 255, 255),
                2
            )

        # 두 이미지를 나란히 출력
        combined = np.hstack([frame, depth_color])

        cv2.imshow("SENSE+ Depth Test", combined)

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

finally:

    cap.release()
    cv2.destroyAllWindows()