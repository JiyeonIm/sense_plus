import cv2
import torch
import numpy as np

from PIL import Image
from transformers import (
    AutoImageProcessor,
    AutoModelForDepthEstimation
)


class DepthEstimator:

    def __init__(self, depth_scale=0.55):
        self.depth_scale = depth_scale

        self.device = "cuda" if torch.cuda.is_available() else "cpu"

        self.model_name = (
            "depth-anything/"
            "Depth-Anything-V2-Metric-Indoor-Small-hf"
        )

        self.processor = AutoImageProcessor.from_pretrained(
            self.model_name
        )

        self.model = AutoModelForDepthEstimation.from_pretrained(
            self.model_name
        ).to(self.device)

        self.model.eval()

        print(f"[Depth] Device: {self.device}")

    @torch.inference_mode()
    def predict(self, frame):

        height, width = frame.shape[:2]

        # OpenCV BGR → RGB
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)

        # Preprocessing
        inputs = self.processor(
            images=image,
            return_tensors="pt"
        ).to(self.device)

        # Inference
        outputs = self.model(**inputs)

        # 원본 이미지 크기로 복원
        prediction = self.processor.post_process_depth_estimation(
            outputs,
            target_sizes=[(height, width)]
        )

        depth_map = prediction[0]["predicted_depth"]

        # return depth_map.cpu().numpy()
        return depth_map.cpu().numpy() * self.depth_scale

    def visualize(self, depth_map, max_distance=5.0):

        # 시각화 범위 제한
        depth = np.clip(depth_map, 0, max_distance)

        # 가까울수록 높은 값으로 변환
        depth = 255 * (1 - depth / max_distance)

        depth = depth.astype(np.uint8)

        return cv2.applyColorMap(
            depth,
            cv2.COLORMAP_TURBO
        )