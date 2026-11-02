import cv2
import config
from finger_input import FingerTracker


tracker = FingerTracker()

try:
    tracker.start()
    print("Tracking started. Press Q to quit.")

    while True:
        zone = tracker.update()

        print(f"\rSteering zone: {zone:<7}", end="")

        # Display the camera feed for visual testing.
        if tracker.frame is not None:
            cv2.imshow(config.FINGER_WINDOW_NAME, tracker.frame)

        if cv2.waitKey(1) & 0xff == ord("q"):
            break

finally:
    tracker.stop()
    cv2.destroyAllWindows()
    print("\nTest finished.")
