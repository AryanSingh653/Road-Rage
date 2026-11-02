# ==============================
# Camera settings
# ==============================

# Laptop webcam: used only for finger tracking.
FINGER_CAMERA_INDEX = 0

# Pixy USB camera: used only for the FPV game view.
FPV_CAMERA_INDEX = 1

# Camera frame dimensions.
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480


# ==============================
# Finger tracking settings
# ==============================

# Finger position boundaries, measured from 0.0 to 1.0 (35% left / 30% center / 35% right).
ZONE_LEFT_LIMIT = 0.35
ZONE_RIGHT_LIMIT = 0.65

# Extra distance the hand must come back past a boundary before steering stops.
# Prevents flickering between steering and neutral at the zone edges.
ZONE_HYSTERESIS = 0.04

# Number of consecutive frames needed to confirm a zone change.
ZONE_STABLE_FRAMES = 2

# Time without a detected hand before switching to NEUTRAL.
NO_HAND_TIMEOUT = 0.2

# Minimum time between confirmed zone changes.
ZONE_HOLD_TIME = 0.08

# Smaller frame dimensions make tracking faster.
FINGER_FRAME_WIDTH = 320
FINGER_FRAME_HEIGHT = 240

# Keep OpenCV/MediaPipe off the Pygame render thread and limit CPU use.
HAND_TRACKING_FPS = 15


# ==============================
# Game settings
# ==============================

COUNTDOWN_SECONDS = 1.0
#MAX_SPEED = 6.0
ACCELERATION = 2.0

# Points earned per second are multiplied by current speed.
SCORE_MULTIPLIER = 1.0


# ==============================
# Display settings
# ==============================

WINDOW_WIDTH = 960
WINDOW_HEIGHT = 540

# OpenCV uses BGR colour order, not RGB.
WHITE = (255, 255, 255)
GREEN = (0, 255, 0)
YELLOW = (0, 255, 255)
RED = (0, 0, 255)
BLUE = (255, 0, 0)
BLACK = (0, 0, 0)

# Text settings.
FONT_SCALE = 0.7
FONT_THICKNESS = 2


# ==============================
# Debug settings
# ==============================

DEBUG_MODE = False
SHOW_FINGER_PREVIEW = False

# OpenCV window names.
GAME_WINDOW_NAME = "EV3 FPV Game"
FINGER_WINDOW_NAME = "Finger Tracking Debug"


# SMASH OPERATION
SMASH_FINGER_RATIO = 0.37