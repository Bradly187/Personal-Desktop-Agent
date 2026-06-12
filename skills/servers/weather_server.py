"""weather_server — Open-Meteo current/forecast weather (stdio MCP skill).

Keyless and free; the location is set ONCE by voice ("set my location to
Austin") via Open-Meteo's geocoder and persisted to ~/.claude/skills/weather.json.
Output includes the **barometric pressure trend** — pressure drops correlate
with joint-pain flares, so "pressure falling" is a personally meaningful signal
for an RA user (and a future PainDayEngine input).

Feeds the morning brief: "every morning at 8 brief me on the weather" runs
through the existing scheduler → plan → SKILL_QUERY chain.

Run standalone:  python -m skills.servers.weather_server
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("weather")

_GEO_API = "https://geocoding-api.open-meteo.com/v1/search"
_API = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_S = 10

# WMO weather interpretation codes (subset; unknown codes report numerically)
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    80: "rain showers", 81: "showers", 82: "heavy showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm",
}


def _config_path() -> Path:
    return Path(os.environ.get("DA_WEATHER_CONFIG") or
                (Path.home() / ".claude" / "skills" / "weather.json"))


def _http_json(url: str, *, timeout: float = _TIMEOUT_S) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "personal-desktop-agent"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _load_location(path: Path) -> dict | None:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if {"lat", "lon"} <= set(cfg):
            return cfg
    except (OSError, ValueError):
        pass
    return None


# ---------------------------------------------------------------------------
# Plain logic (unit-testable with a fake fetch)
# ---------------------------------------------------------------------------

def _geocode_and_save(place: str, path: Path, *, fetch=_http_json) -> str:
    qs = urllib.parse.urlencode({"name": place, "count": 1})
    try:
        results = fetch(f"{_GEO_API}?{qs}").get("results") or []
    except Exception as exc:
        return f"Location lookup failed: {exc}"
    if not results:
        return f"I couldn't find a place called {place!r}."
    r = results[0]
    cfg = {"lat": r["latitude"], "lon": r["longitude"],
           "place": f"{r.get('name', place)}, {r.get('admin1', '')}".rstrip(", ")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return f"Location set to {cfg['place']}."


def _pressure_trend(hourly: dict) -> str:
    pressures = (hourly or {}).get("surface_pressure") or []
    if len(pressures) < 13:
        return ""
    delta = pressures[12] - pressures[0]
    if delta <= -4:
        return (f" Barometric pressure is falling {abs(delta):.0f} hPa over the "
                f"next 12 hours — a flare-relevant drop.")
    if delta >= 4:
        return f" Pressure is rising {delta:.0f} hPa over the next 12 hours."
    return " Pressure is steady."


def _current(cfg: dict, *, fetch=_http_json) -> str:
    qs = urllib.parse.urlencode({
        "latitude": cfg["lat"], "longitude": cfg["lon"],
        "current": "temperature_2m,apparent_temperature,precipitation,"
                   "weather_code,wind_speed_10m",
        "hourly": "surface_pressure",
        "forecast_days": 1,
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
    })
    try:
        data = fetch(f"{_API}?{qs}")
    except Exception as exc:
        return f"Weather lookup failed: {exc}"
    cur = data.get("current") or {}
    cond = _WMO.get(cur.get("weather_code"), f"code {cur.get('weather_code')}")
    return (f"{cfg.get('place', 'Here')}: {cur.get('temperature_2m')}°F "
            f"(feels like {cur.get('apparent_temperature')}°F), {cond}, "
            f"wind {cur.get('wind_speed_10m')} mph."
            f"{_pressure_trend(data.get('hourly'))}")


def _forecast(cfg: dict, days: int = 2, *, fetch=_http_json) -> str:
    days = max(1, min(int(days), 7))
    qs = urllib.parse.urlencode({
        "latitude": cfg["lat"], "longitude": cfg["lon"],
        "daily": "temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,weather_code",
        "forecast_days": days, "temperature_unit": "fahrenheit",
    })
    try:
        daily = fetch(f"{_API}?{qs}").get("daily") or {}
    except Exception as exc:
        return f"Forecast lookup failed: {exc}"
    lines = []
    for i, day in enumerate(daily.get("time") or []):
        cond = _WMO.get((daily.get("weather_code") or [None]*9)[i], "?")
        lines.append(
            f"{day}: {(daily.get('temperature_2m_min') or ['?']*9)[i]}–"
            f"{(daily.get('temperature_2m_max') or ['?']*9)[i]}°F, {cond}, "
            f"{(daily.get('precipitation_probability_max') or ['?']*9)[i]}% rain")
    return "\n".join(lines) or "No forecast data."


_NO_LOCATION = ("I don't have your location yet — say something like "
                "'set my location to Austin Texas'.")


# ---------------------------------------------------------------------------
# MCP tool wrappers
# ---------------------------------------------------------------------------

@mcp.tool()
def set_location(place: str) -> str:
    """Geocode a city/place name and save it as the weather location (one-time)."""
    return _geocode_and_save(place, _config_path())


@mcp.tool()
def current_weather() -> str:
    """Current temperature, conditions, wind, and the 12-hour barometric
    pressure trend (read-only)."""
    cfg = _load_location(_config_path())
    return _current(cfg) if cfg else _NO_LOCATION


@mcp.tool()
def forecast(days: int = 2) -> str:
    """Daily highs/lows, conditions, and rain probability (read-only)."""
    cfg = _load_location(_config_path())
    return _forecast(cfg, days) if cfg else _NO_LOCATION


if __name__ == "__main__":
    mcp.run(transport="stdio")
