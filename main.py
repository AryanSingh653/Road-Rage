"""ROAD RAGE: a five-lane pixel-art traffic survival game.

Keyboard controls are the default adapter. A future webcam controller can call
GameInput.set_steering(-1/0/1) and GameInput.trigger_smash() without changing
the simulation or renderer.
"""

import json
import math
import random
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

import pygame


WIDTH, HEIGHT = 960, 720
FPS = 60
# Bike speed: starts slow and ramps up gradually to a lower top speed.
START_SPEED = 5.0   # constant for the whole first minute
MAX_SPEED = 12.0
SPEED_ACCEL = 0.7   # speed gained per second
DRIFT_SPEED = 310.0  # sideways steering speed in px/s (back to the original 1x)

# Tutorial intro: no traffic for the first 30 seconds.
# In the last 10 seconds a few cars appear (in the lane next to the rider) so
# the player can practise the smash. Normal gameplay starts after that.
TUTORIAL_SECONDS = 30.0
TUTORIAL_SMASH_START = 20.0
TUTORIAL_CAR_GAP = 2.5

# Difficulty schedule (all times in seconds from the start of the run):
#   0-60    speed = START_SPEED, light traffic (1-2 cars per wave)
#   60-120  speed rises 10% over the first 10s, then holds
#   120-180 more traffic
#   180-240 speed rises another 10% (of the current speed) over 10s, then holds
#   240-300 more traffic
#   300+    normal gameplay: speed keeps climbing at SPEED_ACCEL up to MAX_SPEED
STAGE_SECONDS = 60.0
BOOST_SECONDS = 10.0
BOOST_FACTOR = 1.10


def speed_at(t):
    """Bike speed (game units) after t seconds of play."""
    speed = START_SPEED
    for start in (STAGE_SECONDS, 3 * STAGE_SECONDS):
        gain = speed * (BOOST_FACTOR - 1.0)
        if t < start:
            return speed
        if t < start + BOOST_SECONDS:
            return speed + gain * (t - start) / BOOST_SECONDS
        speed += gain
    normal_start = 5 * STAGE_SECONDS
    if t < normal_start:
        return speed
    return min(MAX_SPEED, speed + (t - normal_start) * SPEED_ACCEL)


def traffic_at(t):
    """Return (max cars per wave, base spawn gap in seconds, extra-car chance)."""
    if t < 2 * STAGE_SECONDS:
        return 2, 0.86, 0.0
    if t < 4 * STAGE_SECONDS:
        return 2, 0.74, 0.10
    if t < 5 * STAGE_SECONDS:
        return 2, 0.64, 0.20
    normal = t - 5 * STAGE_SECONDS
    return (2 if normal < 10 else 3), max(.32, .64 - normal * .015), clamp(.20 + normal * .0125, 0, .40)
ROAD_LEFT, ROAD_RIGHT = 206, 754
LANES = 5
LANE_W = (ROAD_RIGHT - ROAD_LEFT) / LANES
PLAYER_Y = 548
# Player collision box is 64 px wide, larger than the 48 px rider silhouette.
PLAYER_HITBOX_HALF_WIDTH = 32
PLAYER_HITBOX_HALF_HEIGHT = 54

BG = (12, 11, 21)
ROAD = (30, 29, 43)
PALETTE = [(255, 176, 58), (100, 212, 220), (235, 90, 129), (164, 134, 232), (226, 224, 210)]


def clamp(value, low, high):
    return max(low, min(high, value))


class GameInput:
    """Small external-input surface for keyboard or future hand tracking.

    Steering is persistent: set -1/1 to keep drifting, and set 0 to stop.
    Smash is an edge-triggered event; call trigger_smash() once per fist gesture.
    """
    def __init__(self):
        self.steering = 0
        self.smash_requested = False

    def set_steering(self, direction):
        self.steering = int(clamp(direction, -1, 1))

    def trigger_smash(self):
        self.smash_requested = True

    def consume_smash(self):
        value, self.smash_requested = self.smash_requested, False
        return value


@dataclass
class Car:
    x: float
    y: float
    color: tuple
    lane: int
    kind: str = "car"
    near_miss_checked: bool = False
    destroyed: bool = False
    lane_changer: bool = False
    target_lane: int = -1
    signal_timer: float = -1.0
    changing: bool = False
    offset_target: float = 0.0
    offset_timer: float = 0.0


@dataclass
class Particle:
    x: float
    y: float
    vx: float
    vy: float
    life: float
    color: tuple
    size: int


@dataclass
class Soul:
    x: float
    y: float
    vx: float
    vy: float
    size: int
    phase: float = 0.0


class RoadRage:
    def __init__(self):
        global WIDTH, ROAD_LEFT, ROAD_RIGHT, LANE_W
        pygame.init()
        pygame.display.set_caption("ROAD RAGE // Ghost Rider")
        self.window = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        monitor_w, monitor_h = self.window.get_size()
        logical_width = max(960, round(HEIGHT * monitor_w / monitor_h))
        side_growth = (logical_width - 960) // 2
        WIDTH = logical_width
        ROAD_LEFT, ROAD_RIGHT = 206 + side_growth, 754 + side_growth
        LANE_W = (ROAD_RIGHT - ROAD_LEFT) / LANES
        self.screen = pygame.Surface((WIDTH, HEIGHT))
        self.clock = pygame.time.Clock()
        self.font = pygame.font.Font(None, 26)
        self.small = pygame.font.Font(None, 19)
        self.title_font = pygame.font.Font(None, 76)
        self.big_font = pygame.font.Font(None, 50)
        self.input = GameInput()
        self.keyboard_steering = 0
        self.finger_tracker = None
        self.camera_error = ""
        self.camera_snapshot = None
        self.state = "menu"
        self.high_score = 0
        self.leaderboard_path = Path(__file__).resolve().with_name("leaderboard.json")
        self.data_notice = ""
        self.leaderboard = self.load_leaderboard()
        self.current_player = None
        self.player_form = {"name": "", "enrollment": "", "phone": ""}
        self.form_field_order = ("name", "enrollment", "phone")
        self.form_focus = 0
        self.form_consent = False
        self.form_error = ""
        self.gameover_message = ""
        self.score_submitted = False
        self.ui_buttons = {}
        self.embers = []
        self.ember_spawn_timer = 0.0
        self.reset()
        self.start_camera()

    def start_camera(self):
        """Start webcam hand tracking; keep the keyboard playable if it fails."""
        try:
            from finger_input import FingerTracker
            self.finger_tracker = FingerTracker()
            self.finger_tracker.start()
            self.camera_error = ""
        except Exception as exc:
            self.camera_error = str(exc)
            print("Webcam startup failed; full error follows:")
            traceback.print_exc()
            if self.finger_tracker is not None:
                try:
                    self.finger_tracker.stop()
                except Exception:
                    pass
            self.finger_tracker = None
            print(f"Webcam unavailable; keyboard controls remain active. {self.camera_error}")

    def stop_camera(self):
        tracker = self.finger_tracker
        self.finger_tracker = None
        if tracker is not None:
            try:
                tracker.stop()
            except Exception as exc:
                print(f"Webcam cleanup warning: {exc}")

    def poll_camera_input(self):
        """Copy webcam control into GameInput once per frame (vision runs in its own thread)."""
        tracker = self.finger_tracker
        if tracker is None:
            if self.state == "playing":
                self.input.set_steering(self.keyboard_steering)
            return
        try:
            # update() raises if the tracking thread died; without this call a
            # dead worker would leave the last steering zone stuck forever.
            tracker.update()
            self.camera_snapshot = tracker.snapshot()
            if self.state == "playing":
                camera_zone = self.camera_snapshot["zone"]
                # A held keyboard key temporarily takes priority over hand steering.
                steering = self.keyboard_steering if self.keyboard_steering else camera_zone
                self.input.set_steering(steering)
                if tracker.consume_smash():
                    self.input.trigger_smash()
            else:
                tracker.consume_smash()
        except Exception as exc:
            self.camera_error = str(exc)
            print("Hand tracking stopped; full error follows:")
            traceback.print_exc()
            self.stop_camera()
            self.camera_snapshot = None
            print(f"Hand tracking stopped; keyboard controls remain active. {self.camera_error}")
            if self.state == "playing":
                self.input.set_steering(self.keyboard_steering)

    def reset(self):
        self.player_x = (ROAD_LEFT + ROAD_RIGHT) / 2
        self.speed = START_SPEED
        self.elapsed = 0.0
        self.score = 0
        self.combo = 1
        self.combo_timer = 0
        self.cars = []
        self.particles = []
        self.souls = []
        self.spawn_timer = 0.0
        self.score_submitted = False
        self.gameover_message = ""
        self.traffic_pattern = None
        self.traffic_pattern_rows = 0
        self.traffic_pattern_step = 0
        self.traffic_pattern_anchor = 0
        self.traffic_gap_start = 0
        self.on_lane_line = False
        self.lane_line_index = None
        self.road_offset = 0.0
        self.graveyard_offset = 0.0
        self.smash_cooldown = 0.0
        self.chain_timer = 0.0
        self.chain_target = None
        self.shake = 0.0
        self.flash = 0.0
        self.death_scare_time = 0.0
        self.scare_timer = random.uniform(18, 30)
        self.scare = 0.0
        self.message = ""
        self.message_timer = 0.0
        self.keyboard_steering = 0
        self.input.set_steering(0)
        self.input.smash_requested = False

    def lane_x(self, lane):
        return ROAD_LEFT + LANE_W * (lane + 0.5)

    def pop(self, x, y, amount=18, colors=None, speed=180):
        colors = colors or [(255, 203, 76), (255, 102, 37), (230, 50, 57), (255, 240, 176)]
        for _ in range(amount):
            angle = random.random() * math.tau
            mag = random.uniform(speed * .25, speed)
            self.particles.append(Particle(x, y, math.cos(angle) * mag,
                math.sin(angle) * mag, random.uniform(.22, .65), random.choice(colors), random.randint(2, 6)))

    def event(self, event):
        if event.type == pygame.QUIT:
            return False
        if event.type == pygame.KEYDOWN and event.key == pygame.K_F11:
            self.toggle_fullscreen()
            return True

        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            point = self.mouse_to_game(event.pos)
            button = next(
                (name for name, rect in self.ui_buttons.items() if rect.collidepoint(point)),
                None,
            )
            self.handle_ui_click(button, point)
            return True

        if self.state == "player_form" and event.type == pygame.TEXTINPUT:
            field = self.form_field_order[self.form_focus]
            limits = {"name": 32, "enrollment": 32, "phone": 24}
            self.player_form[field] = (self.player_form[field] + event.text)[:limits[field]]
            self.form_error = ""
            return True

        if event.type == pygame.KEYDOWN:
            if self.state == "menu":
                if event.key in (pygame.K_RETURN, pygame.K_p):
                    self.open_player_form()
                elif event.key == pygame.K_l:
                    self.open_leaderboard()
                elif event.key == pygame.K_c:
                    self.state = "controls"
                elif event.key == pygame.K_ESCAPE:
                    return False
            elif self.state == "player_form":
                if event.key == pygame.K_ESCAPE:
                    self.go_home()
                elif event.key == pygame.K_TAB:
                    self.form_focus = (self.form_focus + 1) % len(self.form_field_order)
                elif event.key == pygame.K_BACKSPACE:
                    field = self.form_field_order[self.form_focus]
                    self.player_form[field] = self.player_form[field][:-1]
                elif event.key == pygame.K_RETURN:
                    if self.form_focus < len(self.form_field_order) - 1:
                        self.form_focus += 1
                    else:
                        self.submit_player_form()
            elif self.state == "leaderboard":
                if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE, pygame.K_RETURN):
                    self.go_home()
            elif self.state == "controls":
                if event.key in (pygame.K_ESCAPE, pygame.K_RETURN, pygame.K_BACKSPACE):
                    self.state = "menu"
            elif self.state == "playing":
                if event.key in (pygame.K_LEFT, pygame.K_a):
                    self.keyboard_steering = -1
                elif event.key in (pygame.K_RIGHT, pygame.K_d):
                    self.keyboard_steering = 1
                elif event.key in (pygame.K_SPACE, pygame.K_LCTRL, pygame.K_RCTRL):
                    self.input.trigger_smash()
                elif event.key == pygame.K_ESCAPE:
                    self.state = "paused"
            elif self.state == "paused":
                if event.key == pygame.K_ESCAPE:
                    self.state = "playing"
                elif event.key == pygame.K_r:
                    self.restart_game()
                elif event.key == pygame.K_m:
                    self.go_home()
            elif self.state == "gameover":
                if event.key in (pygame.K_RETURN, pygame.K_r):
                    self.restart_game()
                elif event.key == pygame.K_l:
                    self.open_leaderboard()
                elif event.key in (pygame.K_ESCAPE, pygame.K_m):
                    self.go_home()

        elif event.type == pygame.KEYUP:
            # Handled in every state so a key released during pause can't stay "held".
            if event.key in (pygame.K_LEFT, pygame.K_a) and self.keyboard_steering < 0:
                self.keyboard_steering = 0
            elif event.key in (pygame.K_RIGHT, pygame.K_d) and self.keyboard_steering > 0:
                self.keyboard_steering = 0
        return True

    def mouse_to_game(self, position):
        window_w, window_h = self.window.get_size()
        return (
            int(position[0] * WIDTH / max(1, window_w)),
            int(position[1] * HEIGHT / max(1, window_h)),
        )

    def handle_ui_click(self, button, point):
        if self.state == "menu":
            if button == "play":
                self.open_player_form()
            elif button == "leaderboard":
                self.open_leaderboard()
            elif button == "controls":
                self.state = "controls"
            elif button == "exit":
                pygame.event.post(pygame.event.Event(pygame.QUIT))
        elif self.state == "player_form":
            if button == "start":
                self.submit_player_form()
            elif button == "form_back":
                self.go_home()
            elif button == "consent":
                self.form_consent = not self.form_consent
            elif button and button.startswith("field_"):
                self.form_focus = self.form_field_order.index(button[6:])
        elif self.state == "leaderboard":
            if button == "leaderboard_back":
                self.go_home()
        elif self.state == "controls":
            if button == "controls_back":
                self.state = "menu"
        elif self.state == "paused":
            if button == "resume":
                self.state = "playing"
            elif button == "pause_restart":
                self.restart_game()
            elif button == "pause_home":
                self.go_home()
        elif self.state == "gameover":
            if button == "game_restart":
                self.restart_game()
            elif button == "game_leaderboard":
                self.open_leaderboard()
            elif button == "game_home":
                self.go_home()

    def open_player_form(self):
        self.player_form = {"name": "", "enrollment": "", "phone": ""}
        self.form_focus = 0
        self.form_consent = False
        self.form_error = ""
        self.current_player = None
        self.state = "player_form"
        pygame.key.start_text_input()

    def submit_player_form(self):
        name = self.player_form["name"].strip()
        enrollment = self.player_form["enrollment"].strip()
        phone = self.player_form["phone"].strip()
        digits = "".join(char for char in phone if char.isdigit())

        if len(name) < 2:
            self.form_error = "Please enter a name with at least 2 characters."
            return
        if not 2 <= len(enrollment) <= 32:
            self.form_error = "Enter an enrollment number between 2 and 32 characters."
            return
        if any(not (char.isdigit() or char in "+()- ") for char in phone) or not 7 <= len(digits) <= 15:
            self.form_error = "Enter a valid phone number with 7 to 15 digits."
            return
        if not self.form_consent:
            self.form_error = "Please agree to local storage if your score reaches the Top 10."
            return

        self.current_player = {
            "name": name,
            "enrollment": enrollment,
            "phone": phone,
        }
        pygame.key.stop_text_input()
        self.restart_game()

    def restart_game(self):
        self.reset()
        self.state = "playing"

    def go_home(self):
        pygame.key.stop_text_input()
        self.reset()
        self.current_player = None
        self.state = "menu"

    def open_leaderboard(self):
        pygame.key.stop_text_input()
        self.state = "leaderboard"

    def load_leaderboard(self):
        if not self.leaderboard_path.exists():
            return []
        try:
            payload = json.loads(self.leaderboard_path.read_text(encoding="utf-8"))
            entries = payload.get("entries", []) if isinstance(payload, dict) else []
            clean = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name", "")).strip()
                enrollment = str(entry.get("enrollment", "")).strip()
                phone = str(entry.get("phone", "")).strip()
                score = max(0, int(entry.get("score", 0)))
                if name and enrollment and phone:
                    clean.append({
                        "name": name[:32],
                        "enrollment": enrollment[:32],
                        "phone": phone[:24],
                        "score": score,
                        "saved_at": float(entry.get("saved_at", 0)),
                    })
            clean.sort(key=lambda row: (-row["score"], row["saved_at"]))
            return clean[:10]
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.data_notice = f"Leaderboard could not be read: {exc}"
            return []

    def save_leaderboard(self):
        payload = {"version": 1, "entries": self.leaderboard[:10]}
        temporary = self.leaderboard_path.with_name(self.leaderboard_path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.leaderboard_path)

    def save_score_if_top_ten(self):
        if self.score_submitted:
            return
        self.score_submitted = True
        if not self.current_player:
            self.gameover_message = "No player details were entered; score was not saved."
            return

        enrollment_key = self.current_player["enrollment"].strip().casefold()
        existing = next(
            (row for row in self.leaderboard
             if row["enrollment"].strip().casefold() == enrollment_key),
            None,
        )
        if existing and self.score <= existing["score"]:
            self.gameover_message = "Your previous best score remains on the leaderboard."
            return

        candidate = {
            **self.current_player,
            "score": int(self.score),
            "saved_at": time.time(),
        }
        merged = [
            row for row in self.leaderboard
            if row["enrollment"].strip().casefold() != enrollment_key
        ]
        merged.append(candidate)
        merged.sort(key=lambda row: (-row["score"], row["saved_at"]))
        updated = merged[:10]
        rank = next(
            (index + 1 for index, row in enumerate(updated)
             if row["enrollment"].strip().casefold() == enrollment_key),
            None,
        )
        if rank is None:
            self.gameover_message = "Score did not reach the Top 10; details were not saved."
            return

        self.leaderboard = updated
        try:
            self.save_leaderboard()
            self.gameover_message = f"TOP 10 SCORE SAVED - RANK #{rank}"
        except OSError as exc:
            self.gameover_message = f"Could not save leaderboard: {exc}"

    def toggle_fullscreen(self):
        if pygame.display.get_surface().get_flags() & pygame.FULLSCREEN:
            self.window = pygame.display.set_mode((WIDTH, HEIGHT), pygame.RESIZABLE)
        else:
            self.window = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)

    def update(self, dt):
        dt = min(dt, .04)
        for soul in self.souls:
            soul.phase += dt*1.7
            soul.x += (soul.vx+math.sin(soul.phase*1.3)*25)*dt
            soul.y += (soul.vy+math.cos(soul.phase*1.1)*18)*dt
            if soul.x < 20:
                soul.x = 20; soul.vx = abs(soul.vx)
            elif soul.x > WIDTH-20:
                soul.x = WIDTH-20; soul.vx = -abs(soul.vx)
            if soul.y < 75:
                soul.y = 75; soul.vy = abs(soul.vy)
            elif soul.y > HEIGHT-55:
                soul.y = HEIGHT-55; soul.vy = -abs(soul.vy)

        if self.state == "gameover":
            self.death_scare_time += dt
            self.shake = max(0, self.shake-dt*30)
            self.flash = max(0, self.flash-dt*3)
            return

        if self.state != "playing":
            return

        self.elapsed += dt
        self.speed = speed_at(self.elapsed)
        self.score += int(dt*self.speed*1.2)
        self.road_offset = (self.road_offset+self.speed*35*dt) % 80
        self.graveyard_offset = (self.graveyard_offset+self.speed*35*dt) % 448
        self.player_x += self.input.steering*DRIFT_SPEED*dt
        self.player_x = clamp(self.player_x, ROAD_LEFT+28, ROAD_RIGHT-28)
        self.lane_line_index = min(
            range(1, LANES),
            key=lambda lane: abs(self.player_x - (ROAD_LEFT + LANE_W * lane)),
        )
        divider_x = ROAD_LEFT + LANE_W * self.lane_line_index
        self.on_lane_line = abs(self.player_x - divider_x) < 10

        if self.elapsed < TUTORIAL_SECONDS:
            # Tutorial: empty road, then a few smash-practice cars.
            if self.elapsed < TUTORIAL_SMASH_START:
                self.spawn_timer = 0.0
            elif self.elapsed < TUTORIAL_SECONDS - 2:
                self.spawn_timer -= dt
                if self.spawn_timer <= 0:
                    self.spawn_tutorial_car()
                    self.spawn_timer = TUTORIAL_CAR_GAP
            else:
                self.spawn_timer = 1.5  # short pause before normal traffic
        else:
            self.spawn_timer -= dt
            _, spawn_gap, extra_car_chance = traffic_at(self.elapsed)
            if self.spawn_timer <= 0:
                spawned = self.spawn_traffic_wave()
                if spawned:
                    # Packs contain more cars, so give the next pack more room.
                    self.spawn_timer = spawn_gap*spawned/(1+extra_car_chance)*random.uniform(.88, 1.12)
                else:
                    self.spawn_timer = spawn_gap*.5
        if self.input.consume_smash():
            self.smash()

        self.smash_cooldown = max(0, self.smash_cooldown-dt)
        self.chain_timer = max(0, self.chain_timer-dt)
        self.shake = max(0, self.shake-dt*23)
        self.flash = max(0, self.flash-dt*2.8)
        self.message_timer = max(0, self.message_timer-dt)
        self.combo_timer -= dt
        if self.combo_timer <= 0:
            self.combo = 1

        self.scare_timer -= dt
        if self.scare_timer <= 0:
            self.scare = 1.0
            self.scare_timer = random.uniform(22, 38)
        self.scare = max(0, self.scare-dt*1.8)

        for car in self.cars:
            car.y += (self.speed*42+60)*dt

            if car.lane_changer and not car.changing:
                # Start the signal while the car is still well above the rider.
                if car.signal_timer < 0 and car.y >= 0:
                    car.signal_timer = .42
                elif car.signal_timer > 0:
                    car.signal_timer = max(0, car.signal_timer-dt)
                elif car.signal_timer == 0:
                    car.changing = True

            if not car.changing:
                car.offset_timer -= dt
                if car.offset_timer <= 0:
                    car.offset_target = random.choice((-24, -15, 0, 15, 24))
                    car.offset_timer = random.uniform(1.4, 2.3)

            if car.changing:
                target_x = self.lane_x(car.target_lane)+car.offset_target
                shift = target_x-car.x; step = 300*dt
                car.x += clamp(shift, -step, step)
                if abs(shift) <= step:
                    car.x = target_x; car.lane = car.target_lane
                    car.changing = False; car.lane_changer = False
            else:
                target_x = self.lane_x(car.lane)+car.offset_target
                shift = target_x-car.x; step = 38*dt
                car.x += clamp(shift, -step, step)

            dx, dy = abs(car.x-self.player_x), abs(car.y-PLAYER_Y)

            # Center-distance hitbox: it still covers the visible rider, but
            # no longer treats an entire neighboring lane as part of the bike.
            if dx < PLAYER_HITBOX_HALF_WIDTH and dy < PLAYER_HITBOX_HALF_HEIGHT:
                self.pop(self.player_x, PLAYER_Y, 28)
                self.state = "gameover"; self.death_scare_time = 0.0
                self.shake = 22; self.flash = 1.0
                self.high_score = max(self.score, self.high_score)
                self.save_score_if_top_ten()
                break

            if not car.near_miss_checked and car.y > PLAYER_Y+38:
                car.near_miss_checked = True
                if abs(car.x-self.player_x) < 77:
                    self.award(100, "NEAR MISS  +100")
                    self.pop(car.x, PLAYER_Y, 8, [(255, 207, 83), (255, 115, 43)], 75)

        self.cars = [c for c in self.cars if c.y < HEIGHT+90 and not c.destroyed]

        for particle in self.particles:
            particle.x += particle.vx*dt; particle.y += particle.vy*dt
            particle.vx *= .98; particle.vy *= .98; particle.life -= dt
        self.particles = [particle for particle in self.particles if particle.life > 0]

        self.ember_spawn_timer -= dt
        if self.ember_spawn_timer <= 0:
            for _ in range(random.randint(1, 2)):
                side_x = random.uniform(35, max(36, ROAD_LEFT-20)) if random.random() < .5 else random.uniform(ROAD_RIGHT+20, max(ROAD_RIGHT+21, WIDTH-20))
                self.embers.append(Particle(side_x, random.uniform(HEIGHT-15, HEIGHT+10), random.uniform(-5, 5),
                    random.uniform(-44, -22), random.uniform(1.4, 2.8), random.choice([(147, 66, 51), (171, 101, 55), (92, 64, 104)]), random.randint(1, 3)))
            self.ember_spawn_timer = random.uniform(.12, .24)

        for particle in self.embers:
            particle.x += particle.vx*dt; particle.y += particle.vy*dt; particle.life -= dt
        self.embers = [particle for particle in self.embers if particle.life > 0 and particle.y > -10]

    def _next_traffic_pattern_lanes(self):
        if self.traffic_pattern_rows <= 0:
            self.traffic_pattern = random.choices(
                ("weave", "shifting_gap", "convoy"),
                weights=(4, 4, 3),
                k=1,
            )[0]
            self.traffic_pattern_rows = random.randint(3, 4)
            self.traffic_pattern_step = 0
            self.traffic_pattern_anchor = random.randrange(LANES)
            self.traffic_gap_start = random.randrange(LANES - 1)

        step = self.traffic_pattern_step
        if self.traffic_pattern == "weave":
            parity = (self.traffic_pattern_anchor + step) % 2
            lanes = [lane for lane in range(LANES) if lane % 2 == parity]
        elif self.traffic_pattern == "shifting_gap":
            gap_start = (self.traffic_gap_start + step) % (LANES - 1)
            gap = {gap_start, gap_start + 1}
            lanes = [lane for lane in range(LANES) if lane not in gap]
        else:  # A two-car convoy shifts one lane at a time.
            pair_start = (self.traffic_pattern_anchor + step) % (LANES - 1)
            lanes = [pair_start, pair_start + 1]

        self.traffic_pattern_step += 1
        self.traffic_pattern_rows -= 1
        return lanes

    def spawn_tutorial_car(self):
        """Spawn one plain car in a lane next to the rider so it can be smashed."""
        player_lane = int(clamp((self.player_x-ROAD_LEFT)//LANE_W, 0, LANES-1))
        lanes = [lane for lane in (player_lane-1, player_lane+1) if 0 <= lane < LANES]
        lane = random.choice(lanes)
        self.cars.append(Car(
            self.lane_x(lane), -80, random.choice(PALETTE), lane,
            random.choice(("sedan", "muscle", "van", "taxi")),
            lane_changer=False, target_lane=-1, signal_timer=-1.0,
            offset_target=0, offset_timer=random.uniform(1.6, 2.3),
        ))

    def spawn_traffic_wave(self):
        pattern_lanes = self._next_traffic_pattern_lanes()

        # Leave space between waves and avoid stacking cars in the same lane
        # while the previous row is still entering the screen.
        occupied = set()
        for car in self.cars:
            if -180 < car.y < 140:
                occupied.add(car.lane)
                if car.changing and car.target_lane >= 0:
                    occupied.add(car.target_lane)

        available = [lane for lane in range(LANES) if lane not in occupied]
        lanes = [lane for lane in pattern_lanes if lane not in occupied]
        if not lanes:
            lanes = available
        if not lanes:
            return 0

        max_cars = traffic_at(self.elapsed)[0]
        if len(lanes) > max_cars:
            lanes = random.sample(lanes, max_cars)
        lanes = sorted(lanes)
        row_lanes = set(lanes)

        player_lane = int(clamp((self.player_x-ROAD_LEFT)//LANE_W, 0, LANES-1))
        # Keep the old one-in-five lane-change rate, with at most one clearly
        # signaled changer in a wave.
        change_lane = None
        change_chance = 1 - (0.8 ** len(lanes))
        if random.random() < change_chance:
            eligible = {}
            for lane in lanes:
                targets = [
                    target for target in (lane-1, lane+1)
                    if 0 <= target < LANES
                    and target not in row_lanes
                    and target not in occupied
                ]
                if targets:
                    eligible[lane] = targets
            if eligible:
                change_lane = random.choice(list(eligible))
                targets = eligible[change_lane]
                target_lane = player_lane if player_lane in targets else random.choice(targets)
            else:
                target_lane = -1
        else:
            target_lane = -1

        for lane in lanes:
            neighbors = [other for other in lanes if abs(other-lane) == 1]
            if neighbors:
                neighbor = random.choice(neighbors)
                offset_target = 24 if neighbor > lane else -24
            else:
                offset_target = random.choice((-18, -12, 12, 18))

            car = Car(
                self.lane_x(lane),
                -80-random.uniform(0, 24),
                random.choice(PALETTE),
                lane,
                random.choice(("sedan", "muscle", "van", "taxi")),
                lane_changer=(lane == change_lane),
                target_lane=(target_lane if lane == change_lane else -1),
                signal_timer=-1.0,
                offset_target=offset_target,
                offset_timer=random.uniform(1.6, 2.3),
            )
            self.cars.append(car)

        return len(lanes)

    def award(self, points, message):
        self.combo = min(8, self.combo + 1)
        self.combo_timer = 4.2
        self.score += points * self.combo
        self.message = f"{message}   x{self.combo}"
        self.message_timer = 1.45

    def smash(self):
        if self.smash_cooldown > 0:
            self.message = "CHAIN RECHARGING"
            self.message_timer = .8
            return

        player_lane = int(clamp((self.player_x-ROAD_LEFT)//LANE_W, 0, LANES-1))
        candidates = []
        for car in self.cars:
            car_lane = int(clamp((car.x-ROAD_LEFT)//LANE_W, 0, LANES-1))
            if abs(car.y-PLAYER_Y) < 170 and abs(car_lane-player_lane) == 1:
                candidates.append(car)

        self.smash_cooldown = 1.15
        self.chain_timer = .24

        if not candidates:
            self.message = "NO CAR IN ADJACENT LANE"
            self.message_timer = .8
            return

        target = min(candidates, key=lambda c: abs(c.x-self.player_x) + abs(c.y-PLAYER_Y)*.5)
        target.destroyed = True
        self.chain_target = (target.x, target.y)
        self.souls.append(Soul(target.x, target.y, random.uniform(-48, 48),
            random.uniform(-34, 34), random.randint(29, 39), random.uniform(0, math.tau)))
        self.pop(target.x, target.y, 34)
        self.shake = 12
        self.flash = .42
        self.award(250, "CHAIN SMASH  +250")

    def draw_gravestone(self, x, y, w, h, style, index):
        x, y = int(x), int(y)
        if style == 0:  # rounded headstone
            shape = [(x, y+h), (x, y+12), (x+4, y+5), (x+10, y+1), (x+w-10, y+1), (x+w-4, y+6), (x+w, y+13), (x+w, y+h)]
        elif style == 1:  # pointed gothic marker
            shape = [(x, y+h), (x, y+12), (x+w//2, y), (x+w, y+12), (x+w, y+h)]
        else:  # old square slab
            shape = [(x, y+h), (x, y+5), (x+5, y), (x+w-5, y), (x+w, y+5), (x+w, y+h)]

        colors = [(57, 61, 77), (63, 58, 75), (55, 64, 76), (68, 58, 73)]
        fill = colors[index % len(colors)]
        edge = tuple(min(255, c+29) for c in fill)
        pygame.draw.polygon(self.screen, (7, 8, 15), [(px+3, py+4) for px, py in shape])
        pygame.draw.polygon(self.screen, (34, 35, 49), shape)
        inner = [(x+3, y+h-3), (x+3, y+13), (x+w//2, y+4), (x+w-3, y+13), (x+w-3, y+h-3)] if style == 1 else [(x+3, y+h-3), (x+3, y+13), (x+8, y+4), (x+w-8, y+4), (x+w-3, y+13), (x+w-3, y+h-3)]
        pygame.draw.polygon(self.screen, fill, inner)
        pygame.draw.line(self.screen, edge, (x+4, y+h-4), (x+4, y+14), 2)
        pygame.draw.line(self.screen, tuple(max(0, c-17) for c in fill), (x+w-4, y+15), (x+w-4, y+h-4), 2)
        pygame.draw.rect(self.screen, (43, 43, 58), (x-3, y+h-5, w+6, 6), border_radius=2)
        if index % 3 == 0:
            cx = x+w//2
            pygame.draw.rect(self.screen, (126, 117, 126), (cx-2, y+15, 4, 16))
            pygame.draw.rect(self.screen, (126, 117, 126), (cx-6, y+19, 12, 4))
        else:
            pygame.draw.line(self.screen, (119, 116, 128), (x+8, y+18), (x+w-8, y+18), 1)
            pygame.draw.line(self.screen, (119, 116, 128), (x+8, y+22), (x+w-12, y+22), 1)
            pygame.draw.circle(self.screen, (177, 105, 78), (x+w//2, y+h-10), 2)

    def draw_shipwreck(self, x, y, variant=0):
        x, y = int(x), int(y)
        pygame.draw.ellipse(self.screen, (7, 15, 23), (x-7, y+68, 112, 37))
        pygame.draw.polygon(self.screen, (29, 25, 31), [(x+5, y+65), (x+88, y+63), (x+103, y+73), (x+86, y+94), (x+23, y+98), (x+4, y+82)])
        pygame.draw.polygon(self.screen, (82, 48, 39), [(x+9, y+68), (x+86, y+66), (x+96, y+74), (x+82, y+88), (x+25, y+91), (x+10, y+80)])
        for plank in range(4):
            pygame.draw.line(self.screen, (122, 75, 51), (x+18, y+71+plank*5), (x+84-plank*2, y+73+plank*5), 2)
        pygame.draw.line(self.screen, (51, 42, 40), (x+51, y+70), (x+54, y+4), 5)
        pygame.draw.line(self.screen, (61, 45, 40), (x+53, y+29), (x+22, y+(11 if variant % 2 == 0 else 17)), 4)
        pygame.draw.line(self.screen, (57, 43, 39), (x+54, y+47), (x+89, y+27), 4)
        pygame.draw.polygon(self.screen, (69, 57, 63), [(x+56, y+9), (x+27, y+16), (x+38, y+38), (x+54, y+32)])
        pygame.draw.polygon(self.screen, (59, 50, 57), [(x+58, y+33), (x+84, y+30), (x+72, y+52), (x+56, y+53)])
        pygame.draw.line(self.screen, (154, 118, 81), (x+14, y+75), (x+81, y+74), 2)
        pygame.draw.line(self.screen, (78, 55, 43), (x+12, y+73), (x-2, y+53), 3)
        pygame.draw.line(self.screen, (108, 86, 75), (x+22, y+18), (x+48, y+70), 1)
        pygame.draw.line(self.screen, (108, 86, 75), (x+84, y+35), (x+56, y+70), 1)

    def draw_background(self):
        self.screen.fill(BG)
        pygame.draw.rect(self.screen, (20, 22, 31), (0, 0, ROAD_LEFT-8, HEIGHT))
        pygame.draw.rect(self.screen, (53, 43, 43), (ROAD_RIGHT+8, 0, WIDTH-ROAD_RIGHT-8, HEIGHT))
        pygame.draw.circle(self.screen, (48, 45, 69), (min(83, ROAD_LEFT//2), 92), 62)
        pygame.draw.circle(self.screen, (161, 157, 165), (min(83, ROAD_LEFT//2), 92), 43)
        fog = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        fog_shift = int(math.sin(self.elapsed*.28)*15)
        for side_x, side_w in ((15+fog_shift, max(40, ROAD_LEFT-30)), (ROAD_RIGHT+15, max(40, WIDTH-ROAD_RIGHT-25))):
            for row in range(4):
                fy = 145+row*155+int(math.sin(self.elapsed*.42+row)*10)
                pygame.draw.ellipse(fog, (104, 91, 131, 18), (side_x, fy, side_w, 42))
        self.screen.blit(fog, (0, 0))

        # The left plot remains a graveyard. More screen width reveals more headstones.
        cols = max(4, int((ROAD_LEFT-25)/42))
        for i in range(cols):
            base = (i*112+self.graveyard_offset) % 448-40
            for y in (base-448, base, base+448):
                x = 18+i*42; w = 30+(i % 2)*5; h = 39+(i % 3)*7
                if -20 < y < HEIGHT+20:
                    self.draw_gravestone(x, y, w, h, i % 3, i)

        for i in range(2):
            base = (i*224+self.graveyard_offset) % 448-70
            for y in (base-448, base, base+448):
                if -150 < y < HEIGHT+100:
                    tx = 16+i*max(65, ROAD_LEFT//3)
                    pygame.draw.line(self.screen, (30, 31, 43), (tx, y+80), (tx-8, y), 5)
                    pygame.draw.line(self.screen, (30, 31, 43), (tx-5, y+34), (tx-25, y+10), 3)
                    pygame.draw.line(self.screen, (30, 31, 43), (tx-6, y+45), (tx+20, y+20), 3)

        # Right side expands as the display gets wider: wet sand, waves and wrecked ships.
        phase = self.graveyard_offset*(math.tau/448)

        def shoreline_x(y):
            return ROAD_RIGHT+82+math.sin(y*.035+phase)*7+math.sin(y*.012+2*phase)*3

        shore = [(int(shoreline_x(y)), y) for y in range(-20, HEIGHT+24, 12)]
        pygame.draw.polygon(self.screen, (21, 48, 62), shore+[(WIDTH, HEIGHT+20), (WIDTH, -20)])
        for y in range(-10, HEIGHT+20, 20):
            sx = int(shoreline_x(y)); pygame.draw.line(self.screen, (130, 148, 143), (sx-3, y), (sx+8, y-2), 2)
        for row in range(-25, HEIGHT+35, 43):
            pts = [(x, int(row+math.sin(x*.045+phase+row*.02)*3)) for x in range(ROAD_RIGHT+95, WIDTH+12, 12)]
            if len(pts) > 1:
                pygame.draw.lines(self.screen, (48, 83, 94), False, pts, 2)
        for i in range(25):
            x = ROAD_RIGHT+18+(i*29) % max(24, min(48, WIDTH-ROAD_RIGHT-26))
            base = (i*53+self.graveyard_offset) % 448
            for y in (base-448, base, base+448):
                if -8 < y < HEIGHT+8:
                    pygame.draw.circle(self.screen, (119, 91, 73), (x, int(y)), 1+(i % 3 == 0))
        for i in range(2):
            base = (i*224+self.graveyard_offset) % 448-50
            for y in (base-448, base, base+448):
                if -130 < y < HEIGHT+30:
                    wreck_x = min(ROAD_RIGHT+70+i*100, WIDTH-118)
                    self.draw_shipwreck(wreck_x, y, i)

        # Continuous iron rails separate both side scenes from the playable lanes.
        for fx in (ROAD_LEFT-13, ROAD_RIGHT+13):
            for dx, color, width in ((-6, (42, 42, 54), 3), (0, (103, 87, 92), 3), (6, (43, 42, 54), 3)):
                pygame.draw.line(self.screen, color, (fx+dx, 0), (fx+dx, HEIGHT), width)
            for base in range(-56, HEIGHT+56, 56):
                yy = int(base+self.graveyard_offset % 56)
                pygame.draw.line(self.screen, (78, 69, 82), (fx-7, yy), (fx+7, yy), 3)
                pygame.draw.line(self.screen, (118, 95, 92), (fx, yy-16), (fx, yy+14), 2)
                pygame.draw.polygon(self.screen, (145, 118, 102), [(fx-4, yy-14), (fx+4, yy-14), (fx, yy-22)])
        pygame.draw.rect(self.screen, (110, 51, 47), (ROAD_LEFT-8, 0, 8, HEIGHT))
        pygame.draw.rect(self.screen, (112, 51, 62), (ROAD_RIGHT, 0, 8, HEIGHT))
        pygame.draw.rect(self.screen, ROAD, (ROAD_LEFT, 0, ROAD_RIGHT-ROAD_LEFT, HEIGHT))
        for i in range(80):
            x = ROAD_LEFT+(i*79) % (ROAD_RIGHT-ROAD_LEFT); y = (i*97+int(self.road_offset*1.7)) % HEIGHT
            self.screen.fill((38, 37, 51), (x, y, 2, 2))

        for lane in range(1, LANES):
            x = int(ROAD_LEFT+LANE_W*lane)
            for y in range(-80, HEIGHT, 80):
                yy = int(y+self.road_offset)
                divider_active = self.on_lane_line and lane == self.lane_line_index
                dash_color = (255, 91, 72) if divider_active else (112, 82, 118)
                pygame.draw.rect(self.screen, dash_color, (x-2, yy, 4, 37))
                pygame.draw.rect(self.screen, (47, 41, 62), (x+4, yy, 2, 37))

        for y in range(-40, HEIGHT, 80):
            yy = int(y+self.road_offset)
            pygame.draw.rect(self.screen, (255, 132, 49), (ROAD_LEFT+3, yy, 3, 22))
            pygame.draw.rect(self.screen, (255, 132, 49), (ROAD_RIGHT-6, yy, 3, 22))

    def draw_ghost(self, x, y, size, alpha=70, tint=(175, 218, 231), phase=0.0):
        w = max(22, int(size)); h = int(w*1.25)
        layer = pygame.Surface((w+12, h+12), pygame.SRCALPHA)
        color = (*tint, int(clamp(alpha, 0, 255)))
        tail = h-13; wave = int(math.sin(phase)*5)
        pygame.draw.ellipse(layer, color, (6, 2, w, h-10))
        pygame.draw.polygon(layer, color, [(7, tail), (w//3, tail+5), (w//2, tail-2+wave),
            (w*2//3, tail+6), (w+5, tail-2), (w+5, tail+9), (w*2//3, tail+12),
            (w//2, tail+5-wave), (w//3, tail+12), (7, tail+8)])
        eye = (*tuple(max(0, v-75) for v in tint), int(clamp(alpha*.8, 0, 255)))
        pygame.draw.ellipse(layer, eye, (w//3, h//3, max(3, w//8), max(4, h//9)))
        pygame.draw.ellipse(layer, eye, (w*2//3-w//8, h//3, max(3, w//8), max(4, h//9)))
        pygame.draw.arc(layer, eye, (w//3, h//2, w//3, h//5), 0, math.pi, 2)
        self.screen.blit(layer, (int(x-w/2-6), int(y-h/2-3)))

    def draw_death_jumpscare(self):
        t = self.death_scare_time
        self.screen.fill((37, 3, 12) if math.sin(t*30) > -.2 else (3, 3, 10))
        for i in range(18):
            x = i*64-20
            pygame.draw.polygon(self.screen, (49, 7, 18), [(WIDTH//2, HEIGHT//2), (x, 0), (x+38, 0)])
        skull = pygame.Surface((330, 390), pygame.SRCALPHA)
        pygame.draw.polygon(skull, (143, 27, 19), [(36, 193), (8, 113), (51, 137), (54, 37), (104, 105), (145, 12), (178, 104), (242, 43), (238, 145), (320, 87), (290, 217)])
        pygame.draw.polygon(skull, (255, 91, 23), [(54, 183), (37, 111), (78, 144), (87, 68), (127, 124), (151, 48), (181, 125), (230, 77), (220, 159), (289, 117), (266, 215)])
        pygame.draw.polygon(skull, (255, 198, 83), [(94, 184), (83, 131), (123, 153), (152, 87), (177, 157), (214, 119), (207, 191)])
        pygame.draw.ellipse(skull, (238, 222, 190), (55, 92, 220, 215))
        pygame.draw.polygon(skull, (221, 204, 172), [(73, 218), (115, 231), (214, 231), (258, 214), (244, 292), (220, 330), (106, 330), (76, 286)])
        pygame.draw.rect(skull, (194, 175, 149), (101, 286, 128, 45), border_radius=12)
        pygame.draw.ellipse(skull, (21, 8, 17), (78, 158, 70, 70)); pygame.draw.ellipse(skull, (21, 8, 17), (182, 158, 70, 70))
        pygame.draw.ellipse(skull, (255, 111, 25), (94, 178, 38, 28)); pygame.draw.ellipse(skull, (255, 111, 25), (198, 178, 38, 28))
        pygame.draw.ellipse(skull, (255, 230, 146), (104, 185, 18, 12)); pygame.draw.ellipse(skull, (255, 230, 146), (208, 185, 18, 12))
        pygame.draw.polygon(skull, (48, 22, 27), [(145, 227), (185, 227), (165, 256)])
        pygame.draw.rect(skull, (37, 20, 27), (118, 259, 94, 10), border_radius=4)
        for tx in range(124, 207, 14):
            pygame.draw.rect(skull, (245, 225, 188), (tx, 266, 9, 37), border_radius=2)
        pygame.draw.line(skull, (126, 87, 78), (103, 127), (139, 143), 4); pygame.draw.line(skull, (126, 87, 78), (226, 119), (203, 145), 4)
        scale = .55+min(1.0, t/.42)*.78
        sw, sh = int(330*scale), int(390*scale)
        face = pygame.transform.smoothscale(skull, (sw, sh))
        shake_x = random.randint(-10, 10) if t < .5 else 0
        shake_y = random.randint(-8, 8) if t < .5 else 0
        self.screen.blit(face, (WIDTH//2-sw//2+shake_x, HEIGHT//2-sh//2-12+shake_y))
        if t < .18:
            red = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
            red.fill((255, 20, 14, int(90*(1-t/.18))))
            self.screen.blit(red, (0, 0))
        if t > .62:
            self.label("THE SPIRIT CLAIMS YOU", WIDTH//2, HEIGHT-87, (255, 182, 116), self.big_font, True)

    def draw_car(self, car):
        x, y = int(car.x), int(car.y)
        # Detailed arcade-style cars, drawn from above with body, glass, trim and lights.
        van = car.kind == "van"
        w = 38 if van else 36
        pygame.draw.ellipse(self.screen, (8, 7, 14), (x-22, y-35, 44, 74))
        for wy in (-19, 13):
            pygame.draw.rect(self.screen, (8, 8, 13), (x-w//2-4, y+wy, 6, 14), border_radius=2)
            pygame.draw.rect(self.screen, (8, 8, 13), (x+w//2-2, y+wy, 6, 14), border_radius=2)
        shade = tuple(max(0, c-50) for c in car.color)
        light = tuple(min(255, c+32) for c in car.color)
        pygame.draw.rect(self.screen, (5, 5, 10), (x-w//2-2, y-34, w+4, 68), border_radius=10)
        pygame.draw.rect(self.screen, shade, (x-w//2, y-33, w, 66), border_radius=9)
        pygame.draw.rect(self.screen, car.color, (x-w//2+2, y-32, w-4, 62), border_radius=8)
        # Hood and a bright narrow grille make the front easy to read.
        pygame.draw.polygon(self.screen, light, [(x-w//2+5, y-30), (x+w//2-5, y-30), (x+w//2-8, y-18), (x-w//2+8, y-18)])
        pygame.draw.rect(self.screen, (43, 53, 69), (x-10, y-17, 20, 4), border_radius=2)
        pygame.draw.rect(self.screen, (41, 73, 91), (x-13, y-12, 26, 17), border_radius=5)
        pygame.draw.line(self.screen, (114, 165, 177), (x-10, y-9), (x-8, y+1), 2)
        if van:
            pygame.draw.rect(self.screen, (36, 63, 80), (x-13, y+6, 26, 16), border_radius=3)
            pygame.draw.rect(self.screen, (112, 91, 134), (x-9, y+25, 18, 2))
        else:
            pygame.draw.rect(self.screen, (32, 56, 72), (x-11, y+8, 22, 13), border_radius=4)
            pygame.draw.rect(self.screen, (92, 137, 153), (x-9, y+9, 7, 2))
        # Taxi roof checker stripe; muscle cars get a twin racing stripe.
        if car.kind == "taxi":
            pygame.draw.rect(self.screen, (245, 210, 116), (x-13, y+1, 26, 4))
            for sx in (-10, -2, 6):
                pygame.draw.rect(self.screen, (28, 27, 36), (x+sx, y+1, 4, 4))
        elif car.kind == "muscle":
            pygame.draw.rect(self.screen, (232, 218, 183), (x-7, y-28, 3, 15))
            pygame.draw.rect(self.screen, (232, 218, 183), (x+4, y-28, 3, 15))
        pygame.draw.rect(self.screen, (255, 241, 187), (x-w//2+3, y-32, 8, 4), border_radius=2)
        pygame.draw.rect(self.screen, (255, 241, 187), (x+w//2-11, y-32, 8, 4), border_radius=2)
        pygame.draw.rect(self.screen, (255, 74, 59), (x-w//2+3, y+27, 8, 4), border_radius=2)
        pygame.draw.rect(self.screen, (255, 74, 59), (x+w//2-11, y+27, 8, 4), border_radius=2)
        pygame.draw.line(self.screen, (255, 160, 83), (x-8, y+33), (x+8, y+33), 2)
        if car.lane_changer and car.target_lane >= 0:
            direction = 1 if car.target_lane > car.lane else -1
            blink = (pygame.time.get_ticks()//160) % 2 == 0
            amber = (255, 196, 67) if blink or car.changing else (166, 111, 55)
            # Floating directional chevron is the lane-change warning; side lamps blink too.
            ay = y-43
            pygame.draw.rect(self.screen, (23, 18, 25), (x-18, ay-9, 36, 18), border_radius=5)
            if direction > 0:
                points = [(x+10, ay), (x-3, ay-7), (x-3, ay-3), (x-12, ay-3), (x-12, ay+3), (x-3, ay+3), (x-3, ay+7)]
            else:
                points = [(x-10, ay), (x+3, ay-7), (x+3, ay-3), (x+12, ay-3), (x+12, ay+3), (x+3, ay+3), (x+3, ay+7)]
            pygame.draw.polygon(self.screen, amber, points)
            if blink or car.changing:
                pygame.draw.rect(self.screen, (255, 177, 48), (x-21, y-8, 4, 6), border_radius=2)
                pygame.draw.rect(self.screen, (255, 177, 48), (x+17, y-8, 4, 6), border_radius=2)

    def draw_rider(self, x, y):
        x, y = int(x), int(y)
        # Rear exhaust flicker is short and attached to the back wheel.
        phase = math.sin(self.elapsed * 19)
        pygame.draw.polygon(self.screen, (184, 57, 34), [(x-7, y+43), (x-4, y+50+int(phase*2)), (x, y+46), (x+4, y+52-int(phase*2)), (x+7, y+43)])
        pygame.draw.polygon(self.screen, (255, 151, 46), [(x-3, y+43), (x, y+49+int(phase*2)), (x+3, y+43)])
        pygame.draw.ellipse(self.screen, (7, 6, 13), (x-24, y-44, 48, 101))
        # Rear wheel and front wheel, with tread and metallic hubs.
        pygame.draw.ellipse(self.screen, (8, 8, 13), (x-9, y+29, 18, 26))
        pygame.draw.ellipse(self.screen, (117, 120, 132), (x-5, y+34, 10, 16))
        pygame.draw.ellipse(self.screen, (11, 10, 17), (x-8, y-48, 16, 24))
        pygame.draw.ellipse(self.screen, (174, 180, 188), (x-4, y-43, 8, 14))
        pygame.draw.line(self.screen, (215, 176, 101), (x-5, y-30), (x-7, y+30), 3)
        pygame.draw.line(self.screen, (215, 176, 101), (x+5, y-30), (x+7, y+30), 3)
        # Chopper frame, tank and broad handlebars.
        pygame.draw.polygon(self.screen, (107, 31, 36), [(x-8, y-26), (x+8, y-26), (x+13, y-5), (x+7, y+23), (x-7, y+23), (x-13, y-5)])
        pygame.draw.polygon(self.screen, (218, 75, 40), [(x-6, y-23), (x+6, y-23), (x+9, y-7), (x, y+1), (x-9, y-7)])
        pygame.draw.polygon(self.screen, (255, 176, 67), [(x-4, y-19), (x+4, y-19), (x+5, y-9), (x, y-5), (x-5, y-9)])
        pygame.draw.line(self.screen, (185, 190, 199), (x-21, y-25), (x+21, y-25), 4)
        pygame.draw.circle(self.screen, (218, 190, 127), (x-21, y-25), 4)
        pygame.draw.circle(self.screen, (218, 190, 127), (x+21, y-25), 4)
        # Rider's dark coat and arms reach out to the handlebars.
        pygame.draw.polygon(self.screen, (20, 18, 28), [(x-7, y-5), (x+7, y-5), (x+12, y+10), (x+7, y+28), (x-7, y+28), (x-12, y+10)])
        pygame.draw.polygon(self.screen, (57, 48, 58), [(x-7, y-3), (x-13, y+1), (x-20, y-20), (x-16, y-23), (x-8, y-10)])
        pygame.draw.polygon(self.screen, (57, 48, 58), [(x+7, y-3), (x+13, y+1), (x+20, y-20), (x+16, y-23), (x+8, y-10)])
        pygame.draw.line(self.screen, (190, 159, 117), (x-8, y+9), (x+8, y+9), 2)
        pygame.draw.line(self.screen, (183, 148, 102), (x-6, y+18), (x+6, y+18), 2)
        # Flaming skull: layered flame silhouette, bone face and dark eye sockets.
        flick = int(2*phase)
        pygame.draw.polygon(self.screen, (199, 53, 31), [(x-12, y-26), (x-16, y-38), (x-9, y-35), (x-8, y-49), (x-1, y-40), (x+4, y-53+flick), (x+8, y-38), (x+14, y-45), (x+13, y-27)])
        pygame.draw.polygon(self.screen, (255, 132, 35), [(x-8, y-27), (x-10, y-38), (x-3, y-33), (x+2, y-47+flick), (x+5, y-32), (x+11, y-39), (x+9, y-26)])
        pygame.draw.ellipse(self.screen, (245, 224, 177), (x-10, y-32, 20, 20))
        pygame.draw.rect(self.screen, (224, 207, 171), (x-7, y-18, 14, 5), border_radius=2)
        pygame.draw.ellipse(self.screen, (23, 19, 28), (x-7, y-27, 5, 6))
        pygame.draw.ellipse(self.screen, (23, 19, 28), (x+2, y-27, 5, 6))
        pygame.draw.polygon(self.screen, (67, 48, 49), [(x-3, y-18), (x+3, y-18), (x, y-14)])
        for tooth_x in (-5, 0, 5):
            pygame.draw.line(self.screen, (96, 74, 67), (x+tooth_x, y-17), (x+tooth_x, y-14), 1)

    def draw_button(self, key, text, center_y, width=320, height=48):
        rect = pygame.Rect(WIDTH//2-width//2, int(center_y-height//2), width, height)
        self.ui_buttons[key] = rect
        mouse = self.mouse_to_game(pygame.mouse.get_pos())
        hovered = rect.collidepoint(mouse)
        fill = (104, 43, 47) if hovered else (41, 28, 43)
        border = (255, 175, 92) if hovered else (142, 94, 94)
        pygame.draw.rect(self.screen, (7, 6, 13), rect.move(0, 4), border_radius=9)
        pygame.draw.rect(self.screen, fill, rect, border_radius=9)
        pygame.draw.rect(self.screen, border, rect, 2, border_radius=9)
        self.label(text, rect.centerx, rect.y+13, (255, 230, 199), self.font, center=True)

    def draw_panel(self, width=560, height=620):
        layer = pygame.Surface((width, height), pygame.SRCALPHA)
        layer.fill((7, 7, 18, 218))
        rect = layer.get_rect(center=(WIDTH//2, HEIGHT//2))
        self.screen.blit(layer, rect)
        pygame.draw.rect(self.screen, (121, 68, 80), rect, 2, border_radius=18)

    def draw_home(self):
        shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        shade.fill((3, 4, 13, 105))
        self.screen.blit(shade, (0, 0))
        self.draw_panel(500, 640)
        self.label("ROAD RAGE", WIDTH//2, 78, (255, 147, 69), self.title_font, True)
        self.label("GHOST RIDER  //  TRAFFIC SURVIVAL", WIDTH//2, 155, (224, 203, 197), self.small, True)
        self.label("DODGE THE TRAFFIC. FREE THE SOULS.", WIDTH//2, 208, (177, 205, 212), self.small, True)
        self.draw_button("play", "PLAY  /  ENTER PLAYER DETAILS", 300)
        self.draw_button("leaderboard", "SEE LEADERBOARD", 370)
        self.draw_button("controls", "HOW TO PLAY", 440)
        self.draw_button("exit", "EXIT", 510)
        if self.data_notice:
            self.label(self.data_notice[:70], WIDTH//2, 590, (255, 172, 128), self.small, True)

    def draw_player_form(self):
        self.draw_panel(560, 650)
        self.label("PLAYER DETAILS", WIDTH//2, 66, (255, 147, 69), self.big_font, True)
        self.label("Saved only if your score makes the Top 10.", WIDTH//2, 124, (203, 193, 196), self.small, True)

        fields = (
            ("name", "NAME"),
            ("enrollment", "ENROLLMENT NUMBER"),
            ("phone", "PHONE NUMBER"),
        )
        field_x = WIDTH//2-215
        for index, (key, title) in enumerate(fields):
            y = 198 + index*96
            self.label(title, field_x, y, (226, 204, 180), self.small)
            rect = pygame.Rect(field_x, y+24, 430, 48)
            self.ui_buttons["field_"+key] = rect
            selected = index == self.form_focus
            pygame.draw.rect(self.screen, (25, 23, 34), rect, border_radius=6)
            pygame.draw.rect(
                self.screen,
                (255, 167, 76) if selected else (102, 83, 97),
                rect,
                2,
                border_radius=6,
            )
            value = self.player_form[key]
            if selected:
                value += "|"
            self.label(value[-38:], rect.x+12, rect.y+14, (245, 236, 224), self.font)

        consent_rect = pygame.Rect(WIDTH//2-215, 503, 430, 34)
        self.ui_buttons["consent"] = consent_rect
        pygame.draw.rect(self.screen, (28, 25, 37), (consent_rect.x, consent_rect.y+5, 22, 22), border_radius=3)
        pygame.draw.rect(self.screen, (205, 160, 106), (consent_rect.x, consent_rect.y+5, 22, 22), 2, border_radius=3)
        if self.form_consent:
            pygame.draw.rect(self.screen, (255, 167, 76), (consent_rect.x, consent_rect.y+5, 22, 22), border_radius=3)
            pygame.draw.rect(self.screen, (255, 226, 164), (consent_rect.x, consent_rect.y+5, 22, 22), 2, border_radius=3)
        self.label("I agree to local storage if I reach the Top 10.", consent_rect.x+32, consent_rect.y+7, (211, 199, 199), self.small)

        if self.form_error:
            self.label(self.form_error[:65], WIDTH//2, 548, (255, 112, 99), self.small, True)
        self.draw_button("start", "START RIDE", 602, width=205)
        back_rect = pygame.Rect(WIDTH//2-95, 630, 190, 30)
        self.ui_buttons["form_back"] = back_rect
        self.label("BACK TO HOME  (ESC)", WIDTH//2, 634, (177, 168, 177), self.small, True)

    def draw_leaderboard(self):
        self.draw_panel(650, 650)
        self.label("TOP 10 RIDERS", WIDTH//2, 67, (255, 147, 69), self.big_font, True)
        self.label("Name and score are public; contact details stay private.", WIDTH//2, 126, (198, 188, 192), self.small, True)
        self.label("RANK", WIDTH//2-260, 180, (255, 186, 102), self.small)
        self.label("RIDER", WIDTH//2-170, 180, (255, 186, 102), self.small)
        self.label("SCORE", WIDTH//2+235, 180, (255, 186, 102), self.small, True)

        if not self.leaderboard:
            self.label("No scores yet. Be the first to make the board.", WIDTH//2, 286, (218, 208, 213), self.font, True)
        else:
            for index, row in enumerate(self.leaderboard[:10]):
                y = 218 + index*34
                color = (255, 213, 132) if index == 0 else (225, 220, 218)
                self.label(f"{index+1:02d}", WIDTH//2-260, y, color, self.font)
                self.label(row["name"][:24], WIDTH//2-170, y, color, self.font)
                self.label(f"{row['score']:06d}", WIDTH//2+235, y, color, self.font, True)

        self.draw_button("leaderboard_back", "BACK TO HOME", 610, width=250)

    def draw_controls(self):
        self.draw_panel(560, 500)
        self.label("HOW TO PLAY", WIDTH//2, 117, (255, 147, 69), self.big_font, True)
        lines = [
            "A / D or LEFT / RIGHT  -  STEER",
            "SPACE  -  WHIP THE CHAIN",
            "ESC  -  PAUSE",
            "Stay inside a lane; lane lines are dangerous.",
            "Watch amber arrows before cars change lanes.",
            "A near miss or smash builds your score combo.",
        ]
        for index, line in enumerate(lines):
            self.label(line, WIDTH//2, 225+index*43, (228, 215, 211), self.font, True)
        self.draw_button("controls_back", "BACK TO HOME", 560, width=250)

    def draw_pause(self):
        self.overlay("PAUSED", ["Take a breath or restart the run."], (237, 183, 102))
        self.draw_button("resume", "RESUME", 385, width=280)
        self.draw_button("pause_restart", "RESTART THIS RUN", 455, width=280)
        self.draw_button("pause_home", "MAIN MENU", 525, width=280)
        self.label("ESC RESUME    R RESTART    M HOME", WIDTH//2, 585, (190, 178, 183), self.small, True)

    def draw_game_over(self):
        if self.death_scare_time < 1.45:
            self.draw_death_jumpscare()
            self.draw_button("game_restart", "R / ENTER  -  RESTART", HEIGHT-55, width=310)
            return

        lines = [
            f"FINAL SCORE  {self.score:06d}",
            f"BEST THIS SESSION  {self.high_score:06d}",
            self.gameover_message,
        ]
        self.overlay("WRECKED", lines, (240, 91, 79))
        self.draw_button("game_restart", "PLAY AGAIN  (R / ENTER)", 485, width=330)
        self.draw_button("game_leaderboard", "SEE LEADERBOARD  (L)", 550, width=330)
        self.draw_button("game_home", "MAIN MENU  (ESC)", 615, width=330)

    def draw_hud(self):
        # Right-hand column: COMBO box on top, SCORE box directly below it.
        pygame.draw.rect(self.screen, (12, 11, 23), (WIDTH-246, 18, 220, 82), border_radius=8)
        pygame.draw.rect(self.screen, (117, 59, 88), (WIDTH-246, 18, 220, 82), 2, border_radius=8)
        self.label(f"COMBO  x{self.combo}", WIDTH-228, 31, (255, 219, 137), self.font)
        pygame.draw.rect(self.screen, (12, 11, 23), (WIDTH-246, 110, 220, 82), border_radius=8)
        pygame.draw.rect(self.screen, (117, 59, 88), (WIDTH-246, 110, 220, 82), 2, border_radius=8)
        self.label(f"SCORE  {self.score:06d}", WIDTH-228, 123, (255, 223, 179), self.font)
        self.label(f"SPEED  {self.speed:04.1f} / {MAX_SPEED:.0f}", WIDTH-228, 154, (255, 133, 64), self.small)
        self.label("AMBER ARROW = LANE CHANGE", WIDTH//2, 18, (224, 163, 82), self.small, center=True)
        self.label(f"SOULS {len(self.souls):02d} / 50", WIDTH//2, 43, (177, 220, 232), self.small, center=True)
        if self.on_lane_line:
            self.label("DANGER: MOVE OFF THE LANE LINE", WIDTH//2, 67, (255, 105, 86), self.small, center=True)

        ready = self.smash_cooldown <= 0
        self.label("CHAIN  READY" if ready else f"CHAIN  {self.smash_cooldown:.1f}s", WIDTH-228, 62,
                   (255, 111, 62) if ready else (159, 142, 158), self.small)

        if self.message_timer > 0:
            t = self.font.render(self.message, True, (255, 226, 164))
            self.screen.blit(t, (WIDTH//2-t.get_width()//2, 127))

        if self.state == "playing" and self.elapsed < TUTORIAL_SECONDS:
            if self.elapsed < TUTORIAL_SMASH_START:
                self.label(f"WARM-UP  -  STEER WITH YOUR HAND  -  {TUTORIAL_SMASH_START-self.elapsed:.0f}s",
                           WIDTH//2, 160, (177, 220, 232), self.small, center=True)
            else:
                self.label("SMASH PRACTICE", WIDTH//2, 160, (255, 226, 164), self.font, center=True)
                self.label("FIST OR SPACE SMASHES A CAR IN THE NEXT LANE",
                           WIDTH//2, 188, (255, 190, 118), self.small, center=True)

        self.label("HAND: LEFT / CENTER / RIGHT     FIST: SMASH", WIDTH//2, HEIGHT-50, (188, 174, 191), self.small, center=True)
        self.label("A / D or ← / → STEER     SPACE SMASH     ESC PAUSE", WIDTH//2, HEIGHT-27, (151, 142, 165), self.small, center=True)

    def draw_camera_pip(self):
        """Draw a mirrored live webcam preview and tracking status over gameplay."""
        pip_w, pip_h = 212, 159
        box_x, box_y = 22, 18                    # top-left corner
        x, y = box_x + 4, box_y + 28
        panel = pygame.Rect(box_x, box_y, pip_w + 8, pip_h + 52)
        pygame.draw.rect(self.screen, (8, 8, 16), panel, border_radius=7)
        pygame.draw.rect(self.screen, (171, 91, 72), panel, 2, border_radius=7)

        snapshot = self.camera_snapshot
        frame = snapshot.get("frame") if snapshot else None
        if frame is not None:
            import cv2
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = pygame.image.frombytes(rgb.tobytes(), (rgb.shape[1], rgb.shape[0]), "RGB")
            image = pygame.transform.smoothscale(image, (pip_w, pip_h))
            self.screen.blit(image, (x, y))
            # These guides show the same left 35%, center 30%, right 35% zones used by tracking.
            for ratio in (0.35, 0.65):
                guide_x = x + int(pip_w * ratio)
                pygame.draw.line(self.screen, (255, 0, 0), (guide_x, y), (guide_x, y + pip_h), 2)
            hand_seen = snapshot["hand_detected"]
            fist_seen = snapshot["fist"]
            zone_names = {-1: "LEFT", 0: "CENTER", 1: "RIGHT"}
            camera_label = f"HAND {zone_names.get(snapshot['zone'], 'CENTER')}" if hand_seen else "SHOW YOUR HAND"
            if fist_seen:
                camera_label = "FIST · SMASH"
            color = (148, 241, 165) if hand_seen else (255, 190, 118)
        else:
            pygame.draw.rect(self.screen, (24, 21, 31), (x, y, pip_w, pip_h), border_radius=4)
            camera_label = "CAMERA STARTING" if self.finger_tracker else "CAMERA OFF"
            color = (255, 190, 118) if self.finger_tracker else (255, 150, 133)
            if not self.finger_tracker:
                self.label("Camera unavailable -", x + pip_w // 2, y + pip_h // 2 - 16, (255, 190, 118), self.small, center=True)
                self.label("keyboard active", x + pip_w // 2, y + pip_h // 2 + 4, (255, 190, 118), self.small, center=True)

        self.label("WEBCAM · MIRRORED", x, y - 24, (245, 220, 194), self.small)
        self.label(camera_label, x, y + pip_h + 6, color, self.small)

    def label(self, text, x, y, color, font=None, center=False):
        surf = (font or self.font).render(text, True, color)
        self.screen.blit(surf, (x-surf.get_width()//2 if center else x, y))

    def overlay(self, title, lines, accent=(255, 133, 59)):
        shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA); shade.fill((5, 5, 15, 205)); self.screen.blit(shade, (0, 0))
        self.label(title, WIDTH//2, 176, accent, self.title_font, True)
        y = 292
        for line in lines:
            self.label(line, WIDTH//2, y, (235, 221, 217), self.font, True); y += 42

    def render(self):
        self.ui_buttons.clear()
        offset = (random.uniform(-self.shake, self.shake), random.uniform(-self.shake, self.shake)) if self.shake else (0, 0)

        world = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        original = self.screen
        self.screen = world
        self.draw_background()
        for car in self.cars:
            self.draw_car(car)
        for soul in self.souls:
            glow = 74+int(22*(.5+.5*math.sin(soul.phase*2)))
            self.draw_ghost(soul.x, soul.y, soul.size, alpha=glow, tint=(178, 222, 236), phase=soul.phase)
        self.draw_rider(self.player_x, PLAYER_Y)

        # Fast chain lash: sharp segmented arc to nearby target.
        if self.chain_timer > 0 and self.chain_target:
            tx, ty = self.chain_target
            progress = 1 - self.chain_timer/.24
            reach = min(1, progress*2.5)
            points = [(int(self.player_x+18), PLAYER_Y-10), (int(self.player_x+48*reach), PLAYER_Y-35),
                      (int(self.player_x+(tx-self.player_x)*reach), int(PLAYER_Y+(ty-PLAYER_Y)*reach))]
            pygame.draw.lines(self.screen, (255, 195, 76), False, points, 5)
            pygame.draw.lines(self.screen, (255, 92, 38), False, points, 2)

        for p in self.particles + self.embers:
            alpha = int(255 * clamp(p.life, 0, 1))
            pygame.draw.rect(self.screen, p.color, (int(p.x), int(p.y), p.size, p.size))

        self.screen = original
        self.screen.blit(world, offset)

        # atmospheric vignette
        vignette = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        pygame.draw.rect(vignette, (4, 3, 13, 75), (0, 0, WIDTH, 8)); pygame.draw.rect(vignette, (4, 3, 13, 75), (0, HEIGHT-14, WIDTH, 14))
        self.screen.blit(vignette, (0, 0))

        if self.state == "playing":
            self.draw_hud()
        elif self.state == "menu":
            self.draw_home()
        elif self.state == "player_form":
            self.draw_player_form()
        elif self.state == "leaderboard":
            self.draw_leaderboard()
        elif self.state == "controls":
            self.draw_controls()
        elif self.state == "paused":
            self.draw_pause()
        elif self.state == "gameover":
            self.draw_game_over()

        if self.scare > .1 and self.state == "playing":
            s = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA); s.fill((130, 5, 22, int(65*self.scare))); self.screen.blit(s, (0, 0))
            if self.scare > .68:
                # Brief pixel skull flash, built from rectangles (no external assets needed).
                pygame.draw.rect(self.screen, (236, 225, 206), (WIDTH//2-31, 250, 62, 63))
                pygame.draw.rect(self.screen, (22, 10, 23), (WIDTH//2-21, 269, 13, 13)); pygame.draw.rect(self.screen, (22, 10, 23), (WIDTH//2+8, 269, 13, 13))
                pygame.draw.rect(self.screen, (22, 10, 23), (WIDTH//2-7, 291, 14, 9))

        if self.flash > 0:
            s = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA); s.fill((255, 179, 77, int(95*self.flash))); self.screen.blit(s, (0, 0))

        if self.state in ("playing", "paused"):
            self.draw_camera_pip()

        window_w, window_h = self.window.get_size()
        scaled = pygame.transform.scale(self.screen, (window_w, window_h))
        self.window.blit(scaled, (0, 0))
        pygame.display.flip()

    def run(self):
        alive = True
        try:
            while alive:
                dt = self.clock.tick(FPS) / 1000
                for e in pygame.event.get():
                    alive = self.event(e) and alive
                self.poll_camera_input()
                self.update(dt)
                self.render()
        finally:
            self.stop_camera()
            pygame.quit()
        return 0


if __name__ == "__main__":
    sys.exit(RoadRage().run())