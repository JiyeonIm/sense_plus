import threading
import time

"""
frame	    가장 최근에 촬영된 이미지
frame_id	프레임을 구분하는 고유 번호
timestamp	프레임이 Buffer에 저장된 시점
lock	    여러 Thread가 동시에 데이터에 접근할 때 충돌 방지 : YOLO, VLM의 충돌 방지 - frame, time stamp, frame id를 한번에 관리
"""

class FrameBuffer:

    def __init__(self):
        self.frame = None
        self.frame_id = 0
        self.timestamp = None
        self.lock = threading.Lock()

    def update(self, frame):
        with self.lock:
            self.frame = frame.copy()
            self.frame_id += 1
            self.timestamp = time.monotonic()

    def get(self):
        with self.lock:
            if self.frame is None:
                return None, None, None

            return (
                self.frame.copy(),
                self.frame_id,
                self.timestamp
            )