from ultralytics import YOLO


class ObjectDetector:

    def __init__(self):
        # self.model = YOLO("yolo11n.pt")

        self.model = YOLO("yolo11x.pt")

    def detect(self, frame):

        results = self.model.predict(
            frame,
            device=0,
            conf=0.5,
            verbose=False
        )

        return results[0]

    def get_objects(self, result):

        height, width = result.orig_shape

        objects = []

        for box in result.boxes:

            x1, y1, x2, y2 = box.xyxy[0].tolist()

            class_id = int(box.cls.item())
            confidence = float(box.conf.item())

            label = result.names[class_id]

            # Bounding Box 중심점
            center_x = (x1 + x2) / 2

            # 화면 내 상대적 위치
            relative_x = center_x / width

            if relative_x < 1 / 3:
                position = "왼쪽"

            elif relative_x < 2 / 3:
                position = "중앙"

            else:
                position = "오른쪽"

            objects.append({
                "label": label,
                "confidence": confidence,
                "position": position,
                "bbox": [x1, y1, x2, y2]
            })

        return objects