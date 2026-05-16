"""
health_viz.py — Cosmic nebula system health visualization.

A swirling vortex of particles expanding outward from the center (positive z-axis),
creating a "coming out of the screen" effect. Health metrics drive color, rotation
speed, particle density, and nebula arm structure.

Colors encode system state:
  - Cyan-teal: healthy processes, low usage
  - Golden amber: elevated resource usage
  - Warm coral: warnings, high load
  - Deep rose: critical errors
  - White/warm sparks: event flashes

Metrics pulled:
  - CPU usage (drives rotation speed + arm brightness)
  - GPU VRAM/util (drives core glow intensity)
  - Temperature (shifts palette warmer)
  - Process count, network connections (particle density)

Run:
    python health_viz.py
    python health_viz.py --width 1000 --height 700 --fps 36

Requires: pip install pygame-ce
"""

import argparse
import math
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import pygame
except ImportError:
    print("pygame not installed. Run: pip install pygame-ce")
    sys.exit(1)

try:
    import psutil
except ImportError:
    psutil = None

import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    try:
        import pynvml
        pynvml.nvmlInit()
        _GPU_AVAILABLE = True
    except Exception:
        _GPU_AVAILABLE = False


# ─── Color Palette ────────────────────────────────────────────────────────────

@dataclass
class HealthColors:
    """Maps health states to color ranges (R, G, B)."""
    healthy: tuple = (30, 190, 180)       # cyan-teal
    good: tuple = (45, 210, 170)          # teal-green
    moderate: tuple = (210, 150, 45)      # golden amber
    warning: tuple = (220, 100, 50)       # warm coral-orange
    critical: tuple = (200, 50, 70)       # deep rose-red
    idle: tuple = (40, 150, 190)          # ocean blue
    spark: tuple = (255, 240, 220)        # warm white
    background: tuple = (8, 8, 14)        # deep space black


COLORS = HealthColors()


# ─── Metrics Collection ───────────────────────────────────────────────────────

@dataclass
class SystemSnapshot:
    """One moment of system health."""
    timestamp: float = 0.0
    cpu_percent: float = 0.0
    cpu_per_core: list = field(default_factory=list)
    ram_percent: float = 0.0
    gpu_vram_percent: float = 0.0
    gpu_util_percent: float = 0.0
    gpu_temp_c: float = 0.0
    cpu_temp_c: float = 0.0
    process_count: int = 0
    zombie_count: int = 0
    net_connections: int = 0
    disk_io_rate: float = 0.0
    bridge_healthy: Optional[bool] = None
    error_count: int = 0


_TEMP_COOL = 40.0
_TEMP_WARM = 65.0
_TEMP_HOT = 80.0
_TEMP_CRIT = 90.0


def _try_cpu_temp_wmi() -> float:
    """Try to read CPU temp via LibreHardwareMonitor WMI."""
    try:
        import wmi
        w = wmi.WMI(namespace='root\\LibreHardwareMonitor')
        sensors = w.Sensor()
        cpu_temps = [s.Value for s in sensors
                     if s.SensorType == 'Temperature' and 'CPU' in s.Name and 'Package' in s.Name]
        if cpu_temps:
            return float(cpu_temps[0])
        cpu_temps = [s.Value for s in sensors
                     if s.SensorType == 'Temperature' and 'CPU' in s.Name]
        if cpu_temps:
            return float(cpu_temps[0])
    except Exception:
        pass
    try:
        import wmi
        w = wmi.WMI(namespace='root\\OpenHardwareMonitor')
        sensors = w.Sensor()
        cpu_temps = [s.Value for s in sensors
                     if s.SensorType == 'Temperature' and 'CPU' in s.Name]
        if cpu_temps:
            return float(cpu_temps[0])
    except Exception:
        pass
    return 0.0


def collect_snapshot() -> SystemSnapshot:
    """Gather current system metrics."""
    snap = SystemSnapshot(timestamp=time.time())
    if psutil:
        snap.cpu_percent = psutil.cpu_percent(interval=0)
        snap.cpu_per_core = psutil.cpu_percent(interval=0, percpu=True)
        snap.ram_percent = psutil.virtual_memory().percent
        snap.process_count = len(psutil.pids())
        snap.zombie_count = 0
        try:
            snap.net_connections = len(psutil.net_connections(kind='inet'))
        except (psutil.AccessDenied, OSError):
            snap.net_connections = 0
        try:
            disk = psutil.disk_io_counters()
            snap.disk_io_rate = (disk.read_bytes + disk.write_bytes) / (1024 * 1024)
        except Exception:
            pass
    if _GPU_AVAILABLE:
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            snap.gpu_vram_percent = (mem_info.used / mem_info.total) * 100
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            snap.gpu_util_percent = util.gpu
            snap.gpu_temp_c = float(pynvml.nvmlDeviceGetTemperature(handle, 0))
        except Exception:
            pass
    snap.cpu_temp_c = _try_cpu_temp_wmi()
    snap.error_count = snap.zombie_count
    if snap.cpu_percent > 90:
        snap.error_count += 1
    if snap.gpu_vram_percent > 95:
        snap.error_count += 1
    if snap.gpu_temp_c > _TEMP_CRIT:
        snap.error_count += 2
    if snap.cpu_temp_c > _TEMP_CRIT:
        snap.error_count += 2
    return snap


# ─── Utilities ────────────────────────────────────────────────────────────────

def lerp_color(c1: tuple, c2: tuple, t: float) -> tuple:
    t = max(0.0, min(1.0, t))
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def health_to_color(value: float, error: bool = False) -> tuple:
    if error:
        return COLORS.critical
    if value < 25:
        return lerp_color(COLORS.healthy, COLORS.good, value / 25.0)
    elif value < 50:
        return lerp_color(COLORS.good, COLORS.moderate, (value - 25) / 25.0)
    elif value < 75:
        return lerp_color(COLORS.moderate, COLORS.warning, (value - 50) / 25.0)
    else:
        return lerp_color(COLORS.warning, COLORS.critical, min((value - 75) / 25.0, 1.0))


# ─── Nebula Particle ─────────────────────────────────────────────────────────

@dataclass
class NebulaParticle:
    """A single particle in the cosmic vortex."""
    angle: float          # radial angle (radians)
    radius: float         # distance from center (0 = core, 1 = edge of screen)
    speed: float          # radial expansion speed
    angular_speed: float  # rotation speed
    size: float           # base size
    color: tuple          # RGB
    brightness: float     # 0..1
    arm: int              # which spiral arm (0-based)
    life: float = 1.0
    z_depth: float = 0.0  # 0 = far (center), 1 = near (edge)


@dataclass
class ShedParticle:
    """A tiny particle shed from a filament — drifts and fades."""
    x: float
    y: float
    vx: float
    vy: float
    size: float
    color: tuple
    life: float = 1.0
    decay: float = 0.01



# ─── Nebula Visualization ─────────────────────────────────────────────────────

class HealthViz:
    def __init__(self, width: int = 900, height: int = 600, fps: int = 36):
        pygame.init()
        pygame.display.set_caption("System Health \u2014 Cosmic Nebula")
        self.width = width
        self.height = height
        self.fps = fps
        self.screen = pygame.display.set_mode((width, height), pygame.RESIZABLE)
        self.clock = pygame.time.Clock()

        # Nebula state
        self.particles: list[NebulaParticle] = []
        self.max_particles = 600
        self.num_arms = 4  # spiral arms
        self.rotation_offset = 0.0  # global rotation
        self.time = 0.0

        # Metrics
        self.sample_interval = 0.2
        self.last_sample = 0.0
        self.last_snapshot = SystemSnapshot()

        # Background star field
        self.stars = [(random.randint(0, width), random.randint(0, height),
                       random.uniform(0.3, 1.0)) for _ in range(80)]

        # Gas nebula — filamentary tendrils like the Crab Nebula
        self.gas_filaments = self._generate_filaments()
        self.shed_particles: list[ShedParticle] = []
        self.max_shed = 300

        # Pre-populate particles
        self._spawn_initial_particles()

    def _generate_filaments(self) -> list:
        """Generate filamentary gas structures — spread out, dusty, Crab Nebula style."""
        filaments = []
        for _ in range(70):
            base_angle = random.uniform(0, math.pi * 2)
            start_radius = random.uniform(0.05, 0.5)
            length = random.uniform(0.2, 0.8)
            wiggle_freq = random.uniform(1.5, 4.0)
            wiggle_amp = random.uniform(0.08, 0.35)
            # Thinner core line, but much wider dust envelope
            base_width = random.uniform(2, 8)
            color_type = random.choice([0, 0, 0, 1, 1, 2, 2])
            brightness = random.uniform(0.15, 0.55)
            drift_phase = random.uniform(0, math.pi * 2)

            filaments.append({
                'base_angle': base_angle,
                'start_radius': start_radius,
                'length': length,
                'wiggle_freq': wiggle_freq,
                'wiggle_amp': wiggle_amp,
                'base_width': base_width,
                'color_type': color_type,
                'brightness': brightness,
                'drift_phase': drift_phase,
                'segments': random.randint(15, 35),
            })
        return filaments

    def _spawn_initial_particles(self):
        """Fill the nebula with particles at various radii."""
        for _ in range(self.max_particles):
            self._spawn_particle(initial=True)

    def _spawn_particle(self, initial: bool = False):
        """Create a new particle near the center (or at random radius if initial)."""
        arm = random.randint(0, self.num_arms - 1)
        arm_angle = (2 * math.pi / self.num_arms) * arm

        if initial:
            radius = random.uniform(0.0, 1.0)
        else:
            radius = random.uniform(0.0, 0.08)

        # Spiral offset — tighter near center, looser at edge
        spiral_twist = radius * 2.5
        angle = arm_angle + spiral_twist + random.gauss(0, 0.3)

        speed = random.uniform(0.08, 0.25)
        # More varied rotation — 30% counter-rotate for asymmetry
        angular_speed = random.uniform(0.08, 0.25) * (1 if random.random() > 0.3 else -1)
        # Add per-particle speed wobble factor
        size = random.uniform(1.5, 4.0)

        self.particles.append(NebulaParticle(
            angle=angle,
            radius=radius,
            speed=speed,
            angular_speed=angular_speed,
            size=size,
            color=COLORS.healthy,
            brightness=random.uniform(0.5, 1.0),
            arm=arm,
            z_depth=radius,
        ))

    def _update_particles(self, dt: float, snap: SystemSnapshot):
        """Move particles outward and rotate them. Health affects behavior."""
        # CPU drives rotation speed
        rotation_mult = 1.0 + snap.cpu_percent / 150.0  # 1x at 0%, ~1.7x at 100%
        # GPU drives expansion speed
        expansion_mult = 1.0 + snap.gpu_util_percent / 80.0
        # Temperature shifts color warmth
        temp = max(snap.gpu_temp_c, snap.cpu_temp_c)
        heat = max(0.0, min(1.0, (temp - _TEMP_COOL) / (_TEMP_CRIT - _TEMP_COOL))) if temp > _TEMP_COOL else 0.0

        # Overall health value for color
        overall_health = (snap.cpu_percent * 0.4 + snap.gpu_vram_percent * 0.3 +
                          snap.ram_percent * 0.3)

        # Irregular global rotation — not constant, uses noise-like variation
        rot_wobble = math.sin(self.time * 0.37) * 0.04 + math.sin(self.time * 0.71) * 0.02
        self.rotation_offset += dt * (0.12 + rot_wobble) * rotation_mult

        alive = []
        for p in self.particles:
            # Expand outward with slight per-particle variation
            speed_jitter = 1.0 + 0.2 * math.sin(self.time * 1.3 + p.angle * 3.7)
            p.radius += p.speed * dt * expansion_mult * 0.15 * speed_jitter
            # Rotate (faster near center for vortex feel)
            center_factor = max(0.2, 1.0 - p.radius)
            p.angle += p.angular_speed * dt * rotation_mult * center_factor
            # Update z-depth
            p.z_depth = p.radius

            # Color based on radius zone + health
            if p.radius < 0.3:
                # Core — driven by GPU (hot = amber, cool = teal)
                zone_health = snap.gpu_vram_percent * (1 + heat * 0.5)
            elif p.radius < 0.6:
                # Mid — driven by CPU
                zone_health = overall_health
            else:
                # Outer — driven by RAM/network
                net_norm = min(snap.net_connections / 500.0 * 60, 80)
                zone_health = snap.ram_percent * 0.6 + net_norm * 0.4

            # Add some per-particle variation
            zone_health += random.uniform(-5, 5)
            zone_health = max(0, min(100, zone_health))

            has_error = snap.error_count > 0 and random.random() < 0.02
            base_color = health_to_color(zone_health, error=has_error)

            # ── Color shifting (like center swirls) ───────────────────────
            # Each particle cycles through an extended palette based on time + angle + arm
            color_phase = (self.time * 0.4 + p.angle * 0.3 + p.arm * 1.2) % (math.pi * 2)
            if color_phase < math.pi * 0.4:
                # Teal → bright cyan
                tc = color_phase / (math.pi * 0.4)
                shift_color = lerp_color((30, 190, 180), (60, 230, 250), tc)
            elif color_phase < math.pi * 0.8:
                # Bright cyan → golden amber
                tc = (color_phase - math.pi * 0.4) / (math.pi * 0.4)
                shift_color = lerp_color((60, 230, 250), (220, 160, 50), tc)
            elif color_phase < math.pi * 1.2:
                # Golden amber → warm coral
                tc = (color_phase - math.pi * 0.8) / (math.pi * 0.4)
                shift_color = lerp_color((220, 160, 50), (230, 90, 60), tc)
            elif color_phase < math.pi * 1.6:
                # Warm coral → soft violet
                tc = (color_phase - math.pi * 1.2) / (math.pi * 0.4)
                shift_color = lerp_color((230, 90, 60), (160, 80, 200), tc)
            else:
                # Soft violet → back to teal
                tc = (color_phase - math.pi * 1.6) / (math.pi * 0.4)
                shift_color = lerp_color((160, 80, 200), (30, 190, 180), tc)

            # Blend health-based color with the shifting color
            # Inner particles shift more, outer particles keep more health color
            shift_strength = 0.5 - p.radius * 0.2  # 0.5 at center, 0.3 at edge
            shift_strength = max(0.2, shift_strength)
            p.color = lerp_color(base_color, shift_color, shift_strength)

            # Apply thermal tint
            if heat > 0:
                warm = (int(200 * heat), int(80 * heat), int(10 * (1 - heat)))
                p.color = lerp_color(p.color, warm, heat * 0.2)

            # ── Edge magenta transition ───────────────────────────────────
            # Particles smoothly shift toward magenta/violet as they approach edges
            if p.radius > 0.4:
                edge_t = min(1.0, (p.radius - 0.4) / 0.6)
                edge_t = edge_t * edge_t * (3.0 - 2.0 * edge_t)
                mag_phase = math.sin(self.time * 0.7 + p.angle * 0.5)
                magenta = (
                    int(180 + 40 * mag_phase),
                    int(40 + 20 * mag_phase),
                    int(160 + 50 * mag_phase),
                )
                p.color = lerp_color(p.color, magenta, edge_t * 0.7)

            # Brightness — irregular, not rhythmic (avoids hypnotic pulsing)
            noise1 = math.sin(self.time * 1.3 + p.angle * 1.7 + p.arm * 2.1)
            noise2 = math.sin(self.time * 0.7 - p.radius * 4.0 + p.angle * 0.9)
            p.brightness = 0.65 + 0.2 * noise1 + 0.15 * noise2

            # Size grows as particle moves outward (perspective)
            p.size = 1.5 + p.radius * 6.0

            # Kill particles that exit the screen
            if p.radius > 1.4:
                continue
            alive.append(p)

        self.particles = alive

        # Spawn new particles to replace dead ones
        deficit = self.max_particles - len(self.particles)
        for _ in range(min(deficit, 8)):  # spawn up to 8 per frame
            self._spawn_particle()

    def _render_nebula(self):
        """Render all particles with perspective projection and internal color gradients."""
        # Center drifts slowly — breaks radial symmetry
        cx = self.width / 2 + math.sin(self.time * 0.23) * 15 + math.sin(self.time * 0.41) * 8
        cy = self.height / 2 + math.cos(self.time * 0.19) * 12 + math.cos(self.time * 0.53) * 6
        max_screen_radius = min(self.width, self.height) * 0.55

        # Sort by z_depth so far particles draw first (painter's algorithm)
        sorted_particles = sorted(self.particles, key=lambda p: p.z_depth)

        for p in sorted_particles:
            # Convert polar to screen coordinates
            screen_r = p.radius * max_screen_radius
            draw_angle = p.angle + self.rotation_offset

            # Perspective: particles further out (closer to viewer) are larger/brighter
            perspective = 0.4 + 0.6 * p.z_depth

            sx = cx + math.cos(draw_angle) * screen_r
            sy = cy + math.sin(draw_angle) * screen_r

            # Skip if off-screen
            if sx < -20 or sx > self.width + 20 or sy < -20 or sy > self.height + 20:
                continue

            # Size with perspective
            draw_size = p.size * perspective
            bright = p.brightness * perspective

            # Core color
            core_color = (
                int(min(255, p.color[0] * bright)),
                int(min(255, p.color[1] * bright)),
                int(min(255, p.color[2] * bright)),
            )

            # Rim color — shifted hue for internal variation
            # Rotate the color toward a complementary/adjacent hue at the rim
            rim_phase = math.sin(self.time * 1.5 + p.angle * 2 + p.arm)
            rim_shift = (
                int(min(255, max(0, p.color[0] * 0.5 + 80 * (0.5 + 0.5 * rim_phase)))),
                int(min(255, max(0, p.color[1] * 0.3 + 60 * (0.5 - 0.3 * rim_phase)))),
                int(min(255, max(0, p.color[2] * 0.6 + 100 * (0.5 + 0.4 * rim_phase)))),
            )
            rim_color = (
                int(min(255, rim_shift[0] * bright)),
                int(min(255, rim_shift[1] * bright)),
                int(min(255, rim_shift[2] * bright)),
            )

            isx, isy = int(sx), int(sy)
            int_size = max(1, int(draw_size))

            if draw_size > 3:
                # Draw concentric rings from outside in for gradient effect
                num_rings = max(3, int(draw_size / 2))
                for ring in range(num_rings, -1, -1):
                    t = ring / max(num_rings, 1)  # 1 at outer edge, 0 at center
                    r = int(draw_size * (0.3 + 0.7 * t))  # outer glow extends past core
                    # Lerp from core_color (center) to rim_color (edge)
                    rc = lerp_color(core_color, rim_color, t)
                    # Fade alpha toward outer edge
                    fade = 1.0 - t * 0.6
                    rc = (int(rc[0] * fade), int(rc[1] * fade), int(rc[2] * fade))
                    if r > 0:
                        pygame.draw.circle(self.screen, rc, (isx, isy), r)
            elif draw_size > 1.5:
                # Medium particles — just core + glow
                glow = (core_color[0] // 3, core_color[1] // 3, core_color[2] // 3)
                pygame.draw.circle(self.screen, glow, (isx, isy), int_size + 1)
                pygame.draw.circle(self.screen, core_color, (isx, isy), int_size)
            else:
                # Tiny particles — single pixel
                pygame.draw.circle(self.screen, core_color, (isx, isy), 1)

    def _render_core_glow(self, snap: SystemSnapshot):
        """Render a planetary nebula-style core — soft violet inner, warm orange outer shell."""
        cx = int(self.width / 2 + math.sin(self.time * 0.23) * 15 + math.sin(self.time * 0.41) * 8)
        cy = int(self.height / 2 + math.cos(self.time * 0.19) * 12 + math.cos(self.time * 0.53) * 6)

        intensity = 0.5 + 0.5 * (snap.gpu_util_percent / 100.0)
        pulse = 0.85 + 0.15 * math.sin(self.time * 2.0)
        intensity *= pulse

        temp = max(snap.gpu_temp_c, snap.cpu_temp_c)
        heat = max(0.0, min(1.0, (temp - _TEMP_COOL) / (_TEMP_CRIT - _TEMP_COOL))) if temp > _TEMP_COOL else 0.0

        # Nebula radii
        outer_radius = 140  # orange/gold outer shell
        inner_radius = 90   # violet/blue inner region
        star_radius = 4     # tiny bright center star

        # ── Outer orange/gold shell (irregular, wispy) ────────────────────
        for r in range(outer_radius + 30, inner_radius, -2):
            ring_t = (r - inner_radius) / (outer_radius + 30 - inner_radius)  # 0=inner, 1=outer edge

            # Irregular edge — use noise to make it non-circular
            noise_val = (
                math.sin(r * 0.07 + self.time * 0.3) * 0.3 +
                math.sin(r * 0.13 - self.time * 0.2) * 0.2 +
                math.sin(r * 0.23 + self.time * 0.15) * 0.15
            )

            # Shell brightness — peaks in the middle of the shell, fades at edges
            sin_val = max(0.0, math.sin(ring_t * math.pi))
            shell_bright = (sin_val ** 0.6) * intensity * 0.45
            shell_bright *= (0.7 + 0.3 * noise_val + 0.3)

            # Outer fade to black
            if ring_t > 0.7:
                fade_val = max(0.0, 1.0 - (ring_t - 0.7) / 0.3)
                shell_bright *= fade_val * fade_val  # squared instead of ** 1.5

            # Color: warm orange/gold, shifting slightly with heat
            orange_base = lerp_color((220, 140, 30), (240, 100, 20), heat)
            # Add some variation — inner parts of shell are more pink/salmon
            shell_color_base = lerp_color((200, 100, 80), orange_base, ring_t)

            shell_bright = max(0.0, float(shell_bright))
            color = (
                int(min(255, max(0, shell_color_base[0] * shell_bright))),
                int(min(255, max(0, shell_color_base[1] * shell_bright))),
                int(min(255, max(0, shell_color_base[2] * shell_bright))),
            )
            if color[0] > 1 or color[1] > 1 or color[2] > 1:
                pygame.draw.circle(self.screen, color, (cx, cy), r)

        # ── Inner violet/lavender cloud ───────────────────────────────────
        for r in range(inner_radius + 20, 8, -1):
            ring_t = r / (inner_radius + 20)  # 1 at outer edge, 0 at center

            # Soft noise for organic texture
            noise_val = (
                math.sin(r * 0.1 + self.time * 0.25) * 0.2 +
                math.sin(r * 0.17 - self.time * 0.18) * 0.15
            )

            # Brightness — brighter toward center, very soft falloff
            inner_bright = (1.0 - ring_t ** 1.2) * intensity * 0.55
            inner_bright *= (0.8 + 0.2 * noise_val + 0.2)

            # Color: violet/lavender center, blending to pink at edges
            violet = (140, 120, 220)
            lavender = (180, 150, 240)
            pink_edge = (190, 110, 160)

            if ring_t < 0.5:
                base = lerp_color(lavender, violet, ring_t * 2)
            else:
                base = lerp_color(violet, pink_edge, (ring_t - 0.5) * 2)

            color = (
                int(min(255, base[0] * inner_bright)),
                int(min(255, base[1] * inner_bright)),
                int(min(255, base[2] * inner_bright)),
            )
            if color[0] > 1 or color[1] > 1 or color[2] > 1:
                pygame.draw.circle(self.screen, color, (cx, cy), r)

        # ── Tiny bright white star at center ──────────────────────────────
        star_bright = 0.8 + 0.2 * math.sin(self.time * 4.0)
        for r in range(star_radius + 3, 0, -1):
            t = r / (star_radius + 3)
            sb = (1.0 - t) * star_bright
            sc = (int(min(255, 255 * sb)), int(min(255, 240 * sb)), int(min(255, 255 * sb)))
            pygame.draw.circle(self.screen, sc, (cx, cy), r)

    def _render_spiral_arms(self, snap: SystemSnapshot):
        """Draw swirling spiral arms — more dense and colorful near center."""
        cx = self.width / 2 + math.sin(self.time * 0.23) * 15 + math.sin(self.time * 0.41) * 8
        cy = self.height / 2 + math.cos(self.time * 0.19) * 12 + math.cos(self.time * 0.53) * 6
        max_r = min(self.width, self.height) * 0.5

        overall = (snap.cpu_percent + snap.ram_percent) / 2

        # Inner swirls — 6 tighter arms near the core with shifting colors
        for arm in range(6):
            arm_base_angle = (2 * math.pi / 6) * arm + self.rotation_offset * 1.2

            prev_x, prev_y = None, None
            for step in range(50):
                t = step / 50.0
                r = t * max_r * 0.45  # inner swirls only reach 45% of max radius

                # Tight spiral twist
                twist = t * 5.0 + math.sin(self.time * 1.2 + arm * 1.1) * 0.5
                angle = arm_base_angle + twist

                x = cx + math.cos(angle) * r
                y = cy + math.sin(angle) * r

                if prev_x is not None:
                    # Brighter near center, fading outward
                    alpha = (1.0 - t) * 0.6
                    pulse = 0.6 + 0.4 * math.sin(self.time * 3.0 - step * 0.2 + arm * 0.8)
                    alpha *= pulse

                    # Color shifts per arm — cycling through teal/cyan/amber
                    color_phase = (self.time * 0.5 + arm * 1.0) % (math.pi * 2)
                    if color_phase < math.pi * 0.667:
                        tc = color_phase / (math.pi * 0.667)
                        arm_color = lerp_color(COLORS.healthy, (50, 220, 240), tc)
                    elif color_phase < math.pi * 1.333:
                        tc = (color_phase - math.pi * 0.667) / (math.pi * 0.667)
                        arm_color = lerp_color((50, 220, 240), COLORS.moderate, tc)
                    else:
                        tc = (color_phase - math.pi * 1.333) / (math.pi * 0.667)
                        arm_color = lerp_color(COLORS.moderate, COLORS.healthy, tc)

                    color = (
                        int(min(255, arm_color[0] * alpha)),
                        int(min(255, arm_color[1] * alpha)),
                        int(min(255, arm_color[2] * alpha)),
                    )
                    if color[0] > 2 or color[1] > 2 or color[2] > 2:
                        pygame.draw.line(self.screen, color,
                                         (int(prev_x), int(prev_y)), (int(x), int(y)), 2)

                prev_x, prev_y = x, y

        # Outer arms — 4 wider arms that extend to the edges with magenta fade
        for arm in range(self.num_arms):
            arm_base_angle = (2 * math.pi / self.num_arms) * arm + self.rotation_offset

            prev_x, prev_y = None, None
            for step in range(80):
                t = step / 80.0
                r = t * max_r
                twist = t * 3.5 + math.sin(self.time * 0.8 + arm) * 0.3
                angle = arm_base_angle + twist

                x = cx + math.cos(angle) * r
                y = cy + math.sin(angle) * r

                if prev_x is not None:
                    alpha = (1.0 - t * 0.7) * 0.3
                    pulse = 0.6 + 0.4 * math.sin(self.time * 2.5 - step * 0.15)
                    alpha *= pulse

                    # Transition from health color to magenta at edges
                    arm_color_base = health_to_color(overall)
                    magenta_edge = (180, 50, 160)
                    arm_color = lerp_color(arm_color_base, magenta_edge, t * t)

                    color = (
                        int(min(255, arm_color[0] * alpha)),
                        int(min(255, arm_color[1] * alpha)),
                        int(min(255, arm_color[2] * alpha)),
                    )
                    if color[0] > 2 or color[1] > 2 or color[2] > 2:
                        pygame.draw.line(self.screen, color,
                                         (int(prev_x), int(prev_y)), (int(x), int(y)), 2)

                prev_x, prev_y = x, y

    def _render_gas_clouds(self):
        """Render Crab Nebula-style filamentary gas — spread out, dusty, blending into core."""
        cx = self.width / 2 + math.sin(self.time * 0.23) * 15 + math.sin(self.time * 0.41) * 8
        cy = self.height / 2 + math.cos(self.time * 0.19) * 12 + math.cos(self.time * 0.53) * 6
        max_screen_radius = min(self.width, self.height) * 0.55

        # Core blend radius — filaments fade out as they approach center
        core_fade_radius = 0.2  # filaments below this radius fade to transparent

        for fil in self.gas_filaments:
            angle = fil['base_angle'] + self.rotation_offset * 0.25
            angle += math.sin(self.time * 0.12 + fil['drift_phase']) * 0.1

            ct = fil['color_type']
            shimmer = 0.7 + 0.3 * math.sin(self.time * 0.4 + fil['drift_phase'])

            if ct == 0:
                base_color = (int(50 * shimmer), int(170 * shimmer), int(160 * shimmer))
            elif ct == 1:
                base_color = (int(190 * shimmer), int(110 * shimmer), int(35 * shimmer))
            else:
                base_color = (int(160 * shimmer), int(45 * shimmer), int(120 * shimmer))

            prev_x, prev_y = None, None
            segments = fil['segments']

            for seg in range(segments):
                t = seg / segments

                # Radius along the filament
                r_norm = fil['start_radius'] + t * fil['length']
                r = r_norm * max_screen_radius

                # Wiggle — more chaotic, dusty feel
                wiggle = fil['wiggle_amp'] * math.sin(
                    t * fil['wiggle_freq'] * math.pi + self.time * 0.3 + fil['drift_phase']
                )
                wiggle += fil['wiggle_amp'] * 0.4 * math.sin(
                    t * fil['wiggle_freq'] * 2.3 * math.pi - self.time * 0.18 + seg * 0.5
                )
                # Third harmonic for dusty irregularity
                wiggle += fil['wiggle_amp'] * 0.2 * math.sin(
                    t * fil['wiggle_freq'] * 4.1 * math.pi + self.time * 0.1
                )

                seg_angle = angle + wiggle
                x = cx + math.cos(seg_angle) * r
                y = cy + math.sin(seg_angle) * r

                if prev_x is not None:
                    # Width — thin and wispy, slight variation along length
                    width_t = 0.4 + 0.6 * math.sin(t * math.pi)
                    width_noise = 0.7 + 0.3 * math.sin(t * 12.0 + fil['drift_phase'])
                    width = max(1, int(fil['base_width'] * width_t * width_noise))

                    # Brightness — fades near core (blend into core glow)
                    core_blend = min(1.0, r_norm / core_fade_radius) if r_norm < core_fade_radius else 1.0
                    # Also fade at the very tips
                    tip_fade = math.sin(t * math.pi) ** 0.5

                    seg_bright = fil['brightness'] * core_blend * tip_fade
                    # Dusty shimmer along the filament
                    seg_bright *= 0.6 + 0.4 * math.sin(self.time * 0.5 + t * 7.0 + fil['drift_phase'])

                    color = (
                        int(min(255, base_color[0] * seg_bright)),
                        int(min(255, base_color[1] * seg_bright)),
                        int(min(255, base_color[2] * seg_bright)),
                    )

                    if color[0] > 1 or color[1] > 1 or color[2] > 1:
                        pygame.draw.line(self.screen, color,
                                         (int(prev_x), int(prev_y)), (int(x), int(y)), width)
                        # Wide diffuse dust envelope behind the filament
                        if seg_bright > 0.08:
                            # Outermost dust — very wide, very faint
                            dust3_color = (color[0] // 6, color[1] // 6, color[2] // 6)
                            pygame.draw.line(self.screen, dust3_color,
                                             (int(prev_x), int(prev_y)), (int(x), int(y)), width + 14)
                            # Mid dust layer
                            dust2_color = (color[0] // 4, color[1] // 4, color[2] // 4)
                            pygame.draw.line(self.screen, dust2_color,
                                             (int(prev_x), int(prev_y)), (int(x), int(y)), width + 8)
                            # Inner dust
                            dust1_color = (color[0] // 3, color[1] // 3, color[2] // 3)
                            pygame.draw.line(self.screen, dust1_color,
                                             (int(prev_x), int(prev_y)), (int(x), int(y)), width + 4)

                        # Shed particles from filament
                        if len(self.shed_particles) < self.max_shed and random.random() < 0.06:
                            # Drift perpendicular to filament direction
                            dx = x - prev_x
                            dy = y - prev_y
                            length = math.sqrt(dx * dx + dy * dy) + 0.001
                            # Perpendicular direction + some radial drift
                            perp_x = -dy / length
                            perp_y = dx / length
                            drift_sign = random.choice([-1, 1])
                            drift_speed = random.uniform(0.3, 1.2)
                            self.shed_particles.append(ShedParticle(
                                x=x + random.uniform(-2, 2),
                                y=y + random.uniform(-2, 2),
                                vx=perp_x * drift_sign * drift_speed + random.uniform(-0.2, 0.2),
                                vy=perp_y * drift_sign * drift_speed + random.uniform(-0.2, 0.2),
                                size=random.uniform(1.0, 2.5),
                                color=color,
                                life=1.0,
                                decay=random.uniform(0.005, 0.02),
                            ))

                prev_x, prev_y = x, y

    def _update_shed_particles(self, dt: float):
        """Update shed particles — drift and fade."""
        alive = []
        for sp in self.shed_particles:
            sp.x += sp.vx
            sp.y += sp.vy
            # Slight deceleration
            sp.vx *= 0.995
            sp.vy *= 0.995
            sp.life -= sp.decay
            if sp.life > 0:
                alive.append(sp)
        self.shed_particles = alive

    def _render_shed_particles(self):
        """Render shed particles as tiny fading dots."""
        for sp in self.shed_particles:
            alpha = sp.life * sp.life  # quadratic fade for soft disappearance
            color = (
                int(sp.color[0] * alpha),
                int(sp.color[1] * alpha),
                int(sp.color[2] * alpha),
            )
            size = max(1, int(sp.size * sp.life))
            if color[0] > 0 or color[1] > 0 or color[2] > 0:
                pygame.draw.circle(self.screen, color, (int(sp.x), int(sp.y)), size)

    def _render_stars(self):
        """Background star field for depth."""
        for sx, sy, brightness in self.stars:
            # Twinkle
            twinkle = 0.5 + 0.5 * math.sin(self.time * 1.5 + sx * 0.01 + sy * 0.01)
            b = brightness * twinkle
            color = (int(180 * b), int(190 * b), int(200 * b))
            self.screen.set_at((sx % self.width, sy % self.height), color)

    def _draw_hud(self):
        """Minimal HUD overlay."""
        font = pygame.font.SysFont("Consolas", 13)
        snap = self.last_snapshot

        lines = [
            f"CPU {snap.cpu_percent:4.1f}%",
            f"RAM {snap.ram_percent:4.1f}%",
        ]
        if _GPU_AVAILABLE:
            lines.append(f"GPU {snap.gpu_vram_percent:4.1f}% VRAM")
            lines.append(f"GPU {snap.gpu_util_percent:4.1f}% util")
        lines.append("")
        if snap.gpu_temp_c > 0:
            lines.append(f"GPU {snap.gpu_temp_c:.0f}\u00b0C")
        if snap.cpu_temp_c > 0:
            lines.append(f"CPU {snap.cpu_temp_c:.0f}\u00b0C")
        elif snap.gpu_temp_c > 0:
            lines.append("CPU temp: n/a")
        lines.append("")
        lines.append(f"Procs {snap.process_count}")
        lines.append(f"Net {snap.net_connections} conn")

        x, y = 10, 10
        for line in lines:
            if not line:
                y += 8
                continue
            text_color = (180, 180, 180)
            if "\u00b0C" in line:
                try:
                    temp_val = float(line.split()[-1].replace("\u00b0C", ""))
                    if temp_val < _TEMP_COOL:
                        text_color = COLORS.healthy
                    elif temp_val < _TEMP_WARM:
                        text_color = COLORS.good
                    elif temp_val < _TEMP_HOT:
                        text_color = COLORS.moderate
                    elif temp_val < _TEMP_CRIT:
                        text_color = COLORS.warning
                    else:
                        text_color = COLORS.critical
                except (ValueError, IndexError):
                    pass
            surf = font.render(line, True, text_color)
            shadow = font.render(line, True, (0, 0, 0))
            self.screen.blit(shadow, (x + 1, y + 1))
            self.screen.blit(surf, (x, y))
            y += 16

    def run(self):
        """Main loop."""
        running = True
        self.last_snapshot = collect_snapshot()
        self.last_sample = time.time()

        while running:
            dt = self.clock.tick(self.fps) / 1000.0
            self.time += dt

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        running = False
                elif event.type == pygame.VIDEORESIZE:
                    self.width, self.height = event.w, event.h
                    self.screen = pygame.display.set_mode(
                        (self.width, self.height), pygame.RESIZABLE)
                    self.stars = [(random.randint(0, self.width),
                                   random.randint(0, self.height),
                                   random.uniform(0.3, 1.0)) for _ in range(80)]

            # Sample metrics
            now = time.time()
            if now - self.last_sample >= self.sample_interval:
                self.last_snapshot = collect_snapshot()
                self.last_sample = now

            # Update
            self._update_particles(dt, self.last_snapshot)
            self._update_shed_particles(dt)

            # Render
            self.screen.fill(COLORS.background)
            self._render_stars()
            self._render_gas_clouds()
            self._render_shed_particles()
            self._render_spiral_arms(self.last_snapshot)
            self._render_core_glow(self.last_snapshot)
            self._render_nebula()
            self._draw_hud()

            pygame.display.flip()

        pygame.quit()


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Cosmic nebula system health visualization")
    parser.add_argument("--width", type=int, default=900, help="Window width")
    parser.add_argument("--height", type=int, default=600, help="Window height")
    parser.add_argument("--fps", type=int, default=36, help="Target FPS")
    args = parser.parse_args()

    viz = HealthViz(width=args.width, height=args.height, fps=args.fps)
    viz.run()


if __name__ == "__main__":
    main()
