"""Weather through Open-Meteo, which needs no API key.

Geocoding finds the place, then one forecast request supplies the current
conditions plus today's high and low. Only a city name leaves the machine.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "JARVIS/0.1 (local Windows personal assistant) python-httpx"

DESCRIPTIONS = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "a thunderstorm",
    96: "a thunderstorm with hail",
    99: "a heavy thunderstorm with hail",
}


class WeatherError(Exception):
  """Raised when a place cannot be found or the service is unreachable."""


class WeatherClient:
  """Current weather and today's range for a place name."""

  def __init__(self, config, client=None):
    self._config = config
    self._client = client or httpx.Client(
        timeout=config.timeout_s, follow_redirects=True
    )
    self._owns_client = client is None

  def close(self):
    if self._owns_client:
      self._client.close()

  def current(self, location):
    place = self._geocode(location)
    payload = self._forecast(place)
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    if "temperature_2m" not in current:
      raise WeatherError("the weather service returned no temperature")
    imperial = self._config.units == "imperial"
    highs = daily.get("temperature_2m_max") or [None]
    lows = daily.get("temperature_2m_min") or [None]
    return {
        "place": place.get("name") or location,
        "country": place.get("country") or "",
        "description": DESCRIPTIONS.get(
            int(current.get("weather_code") or -1), "unknown conditions"
        ),
        "temperature": current.get("temperature_2m"),
        "feels_like": current.get("apparent_temperature"),
        "humidity": current.get("relative_humidity_2m"),
        "wind": current.get("wind_speed_10m"),
        "high": highs[0],
        "low": lows[0],
        "unit": "°F" if imperial else "°C",
        "wind_unit": "mph" if imperial else "km/h",
    }

  def _geocode(self, location):
    name = " ".join(str(location or "").split())
    if not name:
      raise WeatherError("no place was given")
    try:
      response = self._client.get(
          GEOCODE_URL,
          params={
              "name": name,
              "count": 1,
              "language": "en",
              "format": "json",
          },
          headers={"User-Agent": USER_AGENT},
          timeout=self._config.timeout_s,
      )
      response.raise_for_status()
      payload = response.json()
    except httpx.HTTPError as exc:
      raise WeatherError(f"the weather lookup failed ({exc})") from exc
    except ValueError as exc:
      raise WeatherError("the weather service sent unreadable data") from exc
    results = payload.get("results") or []
    if not results:
      raise WeatherError(f"I couldn't find a place called {name}")
    return results[0]

  def _forecast(self, place):
    imperial = self._config.units == "imperial"
    params = {
        "latitude": place.get("latitude"),
        "longitude": place.get("longitude"),
        "current": (
            "temperature_2m,relative_humidity_2m,apparent_temperature,"
            "weather_code,wind_speed_10m"
        ),
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": "auto",
        "forecast_days": 1,
    }
    if imperial:
      params.update(
          temperature_unit="fahrenheit",
          wind_speed_unit="mph",
          precipitation_unit="inch",
      )
    try:
      response = self._client.get(
          FORECAST_URL,
          params=params,
          headers={"User-Agent": USER_AGENT},
          timeout=self._config.timeout_s,
      )
      response.raise_for_status()
      return response.json()
    except httpx.HTTPError as exc:
      raise WeatherError(f"the weather lookup failed ({exc})") from exc
    except ValueError as exc:
      raise WeatherError("the weather service sent unreadable data") from exc
