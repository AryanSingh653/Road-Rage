"""Run this the same way you run the game (VS Code Run Python File) and paste ALL output."""
import sys
import traceback

print("Python:", sys.executable)
print("Version:", sys.version.split()[0])
print()

try:
    import cv2
    print("opencv:", cv2.__version__)
except Exception:
    print("opencv IMPORT FAILED")
    traceback.print_exc()
    sys.exit()

try:
    import mediapipe as mp
    print("mediapipe:", getattr(mp, "__version__", "unknown"))
    print("mediapipe has solutions.hands:", hasattr(mp, "solutions") and hasattr(mp.solutions, "hands"))
except Exception:
    print("mediapipe IMPORT FAILED")
    traceback.print_exc()

try:
    import pygame
    print("pygame:", pygame.version.ver)
except Exception:
    print("pygame IMPORT FAILED")
    traceback.print_exc()

print()
print("Trying cameras (index x backend):")
for index in (0, 1, 2, 3):
    for name in ("CAP_DSHOW", "CAP_MSMF", "CAP_ANY"):
        backend = getattr(cv2, name, None)
        if backend is None:
            continue
        try:
            cam = cv2.VideoCapture(index, backend)
            opened = cam.isOpened()
            got_frame = False
            shape = None
            if opened:
                for _ in range(5):  # first frames are sometimes empty
                    ok, frame = cam.read()
                    if ok and frame is not None:
                        got_frame, shape = True, frame.shape
                        break
            cam.release()
            print(f"  index {index} / {name:9}: opened={opened} frame={got_frame} {shape or ''}")
        except Exception as exc:
            print(f"  index {index} / {name:9}: ERROR {exc}")

print()
print("Trying FingerTracker from your project:")
try:
    import finger_input
    t = finger_input.FingerTracker()
    t.start()
    print("  FingerTracker started on camera", t.camera_index, t.camera_backend)
    t.stop()
except Exception:
    traceback.print_exc()
print("Done.")
