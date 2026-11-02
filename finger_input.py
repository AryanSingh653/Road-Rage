import threading
import time

import cv2
import mediapipe as mp

import config


class FingerTracker:
    """Capture and process hand input away from Pygame's render loop."""

    def __init__(self):
        self.camera = None
        self.hands = None
        self.camera_index = None
        self.camera_backend = None
        self.frame = None

        self.current_zone = 0
        self.candidate_zone = None
        self.stable_frames = 0
        self.fingertip_x = None
        self.hand_detected = False
        self.smash = False
        self.smash_requested = False

        self.last_hand_seen = time.monotonic()
        self.last_zone_change = 0.0

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread = None
        self._worker_error = None
        self._was_fist = False

        self.mp_hands = mp.solutions.hands

    def start(self):
        # Try the preferred device first, then common Windows webcam indices
        # and backends. Some PCs expose the built-in webcam as index 1.
        preferred = config.FINGER_CAMERA_INDEX
        candidates = list(dict.fromkeys((preferred, 0, 1, 2, 3)))
        backends = []
        for name in ("CAP_DSHOW", "CAP_MSMF", "CAP_ANY"):
            backend = getattr(cv2, name, None)
            if backend is not None and backend not in [entry[1] for entry in backends]:
                backends.append((name, backend))

        for camera_index in candidates:
            for backend_name, backend in backends:
                camera = cv2.VideoCapture(camera_index, backend)
                if camera.isOpened():
                    camera.set(cv2.CAP_PROP_FRAME_WIDTH, config.CAMERA_WIDTH)
                    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, config.CAMERA_HEIGHT)
                    camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    success, _ = camera.read()
                    if not success:
                        success, _ = camera.read()
                    if success:
                        self.camera = camera
                        self.camera_index = camera_index
                        self.camera_backend = backend_name
                        break
                camera.release()
            if self.camera is not None:
                break

        if self.camera is None:
            raise RuntimeError(
                "No webcam could be opened (tried indices "
                f"{candidates}). Close other camera apps and check "
                "Windows Settings > Privacy & security > Camera."
            )

        self.hands = self.mp_hands.Hands(
            static_image_mode=False,
            max_num_hands=1,
            model_complexity=0,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._worker_error = None
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._tracking_loop,
            name="OpenCV hand tracking",
            daemon=True,
        )
        self._worker_thread.start()
        print(
            f"Hand tracking initialized on camera {self.camera_index} "
            f"({self.camera_backend}); processing at up to "
            f"{config.HAND_TRACKING_FPS} FPS."
        )

    def _tracking_loop(self):
        frame_interval = 1.0 / max(1, config.HAND_TRACKING_FPS)
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                success, frame = self.camera.read()
                if success:
                    self._process_frame(frame)
            except Exception as exc:
                self._worker_error = exc
                self._stop_event.set()
                return

            remaining = frame_interval - (time.monotonic() - started)
            if remaining > 0:
                self._stop_event.wait(remaining)

    def _process_frame(self, frame):
        # Mirror the image so moving a hand right on screen steers right.
        frame = cv2.flip(frame, 1)
        frame = cv2.resize(
            frame,
            (config.FINGER_FRAME_WIDTH, config.FINGER_FRAME_HEIGHT),
        )
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.hands.process(rgb_frame)
        detected_hand = bool(results.multi_hand_landmarks)
        fist = False
        detected_zone = None

        if detected_hand:
            landmarks = results.multi_hand_landmarks[0].landmark
            wrist = landmarks[0]

            def finger_curled(tip_id, middle_id):
                tip = landmarks[tip_id]
                middle = landmarks[middle_id]
                tip_distance = ((tip.x - wrist.x) ** 2 + (tip.y - wrist.y) ** 2) ** 0.5
                middle_distance = ((middle.x - wrist.x) ** 2 + (middle.y - wrist.y) ** 2) ** 0.5
                return tip_distance < middle_distance

            # A fist is the separate smash gesture. All four fingers should be
            # curled; steering otherwise uses the center of any detected hand.
            fist = all(
                finger_curled(tip, middle)
                for tip, middle in ((8, 6), (12, 10), (16, 14), (20, 18))
            )

            if not fist:
                # Landmark 9 is the center of the palm. This makes steering
                # depend on hand position, not a precise finger pose.
                self.fingertip_x = landmarks[9].x
                x = self.fingertip_x
                left, right = config.ZONE_LEFT_LIMIT, config.ZONE_RIGHT_LIMIT
                hyst = config.ZONE_HYSTERESIS
                # Hysteresis: once steering, the hand must come back a little
                # past the boundary before the bike goes straight again.
                if self.current_zone == -1 and x < left + hyst:
                    detected_zone = -1
                elif self.current_zone == 1 and x > right - hyst:
                    detected_zone = 1
                elif x < left:
                    detected_zone = -1
                elif x > right:
                    detected_zone = 1
                else:
                    detected_zone = 0

        now = time.monotonic()
        if detected_hand:
            self.last_hand_seen = now

        with self._lock:
            self.frame = frame
            self.hand_detected = detected_hand
            self.smash = fist

            if fist and not self._was_fist:
                self.smash_requested = True
            self._was_fist = fist

            if fist:
                # A fist (smash) always means "stop steering": go neutral
                # immediately, without waiting for frame confirmation.
                self.current_zone = 0
                self.candidate_zone = None
                self.stable_frames = 0
                return

            if detected_zone is None:
                self.candidate_zone = None
                self.stable_frames = 0
                if now - self.last_hand_seen >= config.NO_HAND_TIMEOUT:
                    self.current_zone = 0
                return

            if detected_zone == self.current_zone:
                self.candidate_zone = None
                self.stable_frames = 0
                return

            if detected_zone == self.candidate_zone:
                self.stable_frames += 1
            else:
                self.candidate_zone = detected_zone
                self.stable_frames = 1

            if (
                self.stable_frames >= config.ZONE_STABLE_FRAMES
                and now - self.last_zone_change >= config.ZONE_HOLD_TIME
            ):
                self.current_zone = detected_zone
                self.last_zone_change = now
                self.candidate_zone = None
                self.stable_frames = 0

    def update(self):
        """Return the latest cached steering zone without doing vision work."""
        if self._worker_error is not None:
            raise RuntimeError(f"Hand tracking failed: {self._worker_error}")
        with self._lock:
            return self.current_zone

    def snapshot(self):
        """Return a consistent state sample and copy of the latest preview frame."""
        with self._lock:
            frame = self.frame.copy() if self.frame is not None else None
            return {
                "zone": self.current_zone,
                "frame": frame,
                "hand_detected": self.hand_detected,
                "fist": self.smash,
            }

    def consume_smash(self):
        """Return True once for each newly detected fist."""
        with self._lock:
            requested = self.smash_requested
            self.smash_requested = False
            return requested

    def stop(self):
        self._stop_event.set()
        worker = self._worker_thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.0)
        self._worker_thread = None

        if self.camera is not None:
            self.camera.release()
            self.camera = None

        if self.hands is not None:
            self.hands.close()
            self.hands = None

        cv2.destroyAllWindows()