import cv2
from core.frame_buffer import FrameBuffer

buffer = FrameBuffer()

cap = cv2.VideoCapture(0)

if not cap.isOpened():
    raise RuntimeError("카메라를 열 수 없습니다.")

try:
    while True:
        ret, frame = cap.read()

        if not ret:
            break

        # Buffer에 이미지 저장
        buffer.update(frame)

        # Buffer에서 이미지 가져오기
        image, frame_id, timestamp = buffer.get()

        if image is None:
            continue

        cv2.putText(
            image,
            f"Frame ID: {frame_id}",
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (0, 255, 0),
            2
        )

        cv2.imshow("Frame Buffer Test", image)

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

finally:
    cap.release()
    cv2.destroyAllWindows()