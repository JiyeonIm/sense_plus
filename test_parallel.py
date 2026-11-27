import cv2
import time
import threading

from core.frame_buffer import FrameBuffer
from core.object_state import ObjectStateManager
from models.detector import ObjectDetector
from models.vlm import analyze_image

from core.response_manager import ResponseManager

response_manager = ResponseManager()


# 공통 객체
frame_buffer = FrameBuffer()
detector = ObjectDetector()
object_state = ObjectStateManager()

stop_event = threading.Event()

# YOLO 결과 저장용 변수
latest_yolo_frame = None
result_lock = threading.Lock()


# =========================
# Thread 1 : YOLO
# =========================
def yolo_worker():

    global latest_yolo_frame

    last_frame_id = -1

    while not stop_event.is_set():

        frame, frame_id, timestamp = frame_buffer.get()

        # 새로운 프레임이 없으면 대기
        if frame is None or frame_id == last_frame_id:
            time.sleep(0.01)
            continue

        last_frame_id = frame_id

        # YOLO 추론
        result = detector.detect(frame)

        # 실시간 탐지 기능
        objects = detector.get_objects(result)
        
        # 새로운 객체 확인
        new_objects = object_state.update(objects)

        for label, position in new_objects:
            text = f"화면 {position}에 {label}이 있습니다."

            response_manager.add_response(
                text=text,
                source="YOLO"
            )

        annotated_frame = result.plot()

        # 추론에 사용한 프레임 번호 표시
        cv2.putText(
            annotated_frame,
            f"YOLO Frame: {frame_id}",
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (0, 255, 0),
            2
        )

        # 결과 저장
        with result_lock:
            latest_yolo_frame = annotated_frame


# =========================
# Thread 2 : VLM
# =========================
def vlm_worker():

    while not stop_event.is_set():

        question = input("User >> ")

        if question == "q":
            stop_event.set()
            break

        # 질문 입력 완료 시점의 프레임 확보
        frame, frame_id, timestamp = frame_buffer.get()

        if frame is None:
            print("아직 카메라 이미지가 없습니다.")
            continue

        print(f"[VLM] Frame ID: {frame_id}")
        print("[VLM] 분석 중...")

        response_manager.vlm_busy.set()

        try:
            answer = analyze_image(frame, question)

            response_manager.add_response(
                text=answer,
                source="VLM"
            )

        except Exception as e:
            print(f"[VLM Error] {e}")

        finally:
            response_manager.vlm_busy.clear()


def output_worker():

    while not stop_event.is_set():

        response = response_manager.get_response()

        if response is None:
            continue

        source = response["source"]
        text = response["text"]

        print(f"[OUTPUT - {source}] {text}")

# =========================
# Main Thread : Camera
# =========================
def main():

    cap = cv2.VideoCapture(0)

    if not cap.isOpened():
        raise RuntimeError("카메라를 열 수 없습니다.")

    yolo_thread = threading.Thread(
        target=yolo_worker,
        daemon=True
    )

    vlm_thread = threading.Thread(
        target=vlm_worker,
        daemon=True
    )

    output_thread = threading.Thread(
        target=output_worker,
        daemon=True
    )


    yolo_thread.start()
    vlm_thread.start()
    output_thread.start()

    try:
        while not stop_event.is_set():

            ret, frame = cap.read()

            if not ret:
                break

            # 최신 프레임 업데이트
            frame_buffer.update(frame)

            # YOLO 분석 결과 가져오기
            with result_lock:
                display_frame = latest_yolo_frame

            # YOLO 결과가 아직 없다면 원본 출력
            if display_frame is None:
                display_frame = frame

            cv2.imshow(
                "SENSE+ Parallel Test",
                display_frame
            )

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    finally:
        stop_event.set()
        cap.release()
        cv2.destroyAllWindows()

        yolo_thread.join()
        output_thread.join()

        print("SENSE+ 종료")


if __name__ == "__main__":
    main()