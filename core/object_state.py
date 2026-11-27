import time


class ObjectStateManager:

    def __init__(
        self,
        stable_time=1.0,
        missing_time=2.0,
        cooldown=10.0
    ):
        self.stable_time = stable_time
        self.missing_time = missing_time
        self.cooldown = cooldown

        self.first_seen = {}
        self.last_seen = {}
        self.last_announced = {}

        self.active_objects = set()

    def update(self, objects):

        now = time.monotonic()

        current_objects = {
            (obj["label"], obj["position"])
            for obj in objects
        }

        new_objects = []

        # 현재 탐지된 객체 확인
        for key in current_objects:

            if key not in self.first_seen:
                self.first_seen[key] = now

            self.last_seen[key] = now

            duration = now - self.first_seen[key]

            # 일정 시간 이상 유지된 새로운 객체
            if (
                duration >= self.stable_time
                and key not in self.active_objects
            ):
                self.active_objects.add(key)

                last_time = self.last_announced.get(
                    key, float("-inf")
                )

                if now - last_time >= self.cooldown:
                    new_objects.append(key)
                    self.last_announced[key] = now

        # 아직 확정되지 않은 객체의 연속 감지 초기화
        for key in list(self.first_seen):

            if (
                key not in current_objects
                and key not in self.active_objects
            ):
                self.first_seen.pop(key, None)
                self.last_seen.pop(key, None)

        # 일정 시간 이상 사라진 객체 제거
        for key in list(self.active_objects):

            if now - self.last_seen[key] >= self.missing_time:

                self.active_objects.discard(key)

                self.first_seen.pop(key, None)
                self.last_seen.pop(key, None)

        return new_objects