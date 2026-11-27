import cv2
from ultralytics import YOLO

model = YOLO("yolo11n.pt")

cap = cv2.VideoCapture(0)

if not cap.isOpened():
    raise RuntimeError("카메라를 열 수 없습니다.")

while True:
    ret, frame = cap.read()

    if not ret:
        break

    results = model.predict(
        frame,
        device=0,
        conf=0.5,
        verbose=False
    )

    annotated_frame = results[0].plot()

    cv2.imshow("SENSE+ YOLO Test", annotated_frame)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

cap.release()
cv2.destroyAllWindows()