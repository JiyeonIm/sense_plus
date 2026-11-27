import time
import threading

from queue import PriorityQueue, Empty
from itertools import count


"""
사용자 질문에 대한 답변	    우선 출력
새로운 객체 자동 안내	    일반 출력
VLM 분석 중	              자동 안내 생략
오래된 자동 안내	       폐기
여러 응답 동시 발생	       순차적으로 출력
queue에 저장되는 값 (튜플)
(priority, sequence, timestamp, source, text)
priority : 작을수록 우선순위 높음
sequence : 동일 우선순위에서 입력 순서 유지
timestamp : 	오래된 안내 판단
source : 	YOLO 또는 VLM 구분
text : 최종 출력할 문장
"""


class ResponseManager:

    def __init__(self):
        self.queue = PriorityQueue()
        self.counter = count()

        # VLM이 사용자 질문을 처리 중인지 확인
        self.vlm_busy = threading.Event()

    def add_response(self, text, source):

        if not text:
            return

        if source == "YOLO":

            # 사용자 질문 처리 중에는 자동 안내 생략
            if self.vlm_busy.is_set():
                return

            priority = 2

        else:
            priority = 1

        self.queue.put((
            priority,
            next(self.counter),
            time.monotonic(),
            source,
            text
        ))

    def get_response(self):

        try:
            item = self.queue.get(timeout=0.1)

        except Empty:
            return None

        priority, seq, timestamp, source, text = item

        # 오래된 자동 안내는 폐기
        if source == "YOLO":

            if time.monotonic() - timestamp > 3:
                return None

            if self.vlm_busy.is_set():
                return None

        return {
            "source": source,
            "text": text,
            "timestamp": timestamp
        }