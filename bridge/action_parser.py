"""Parse <pidog_action> and <pidog_leds> tags from AI response text."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Canonical action names understood by the AI/SOUL.md persona.
# Entries that differ from pidog's ActionDict are remapped in ACTION_MAP below.
VALID_ACTIONS: set[str] = {
    "forward",
    "backward",
    "turn_left",
    "turn_right",
    "sit",
    "stand",
    "lie_down",
    "bark",
    "bark_harder",
    "howling",
    "pant",
    "wag_tail",
    "shake_hand",
    "high_five",
    "push_up",
    "stretch",
    "body_twisting",
    "tilting_head_left",
    "tilting_head_right",
    "head_up",
    "head_down",
    "nod",
    "shake_head",
    "think",
    "surprise",
}

# Maps AI-facing action names → pidog do_action() keys where they differ.
# pidog accepts both ActionDict keys (underscores) and OPERATIONS keys (spaces).
# Actions not in this map pass through unchanged (e.g. howling, pant, nod, think, surprise
# are all valid OPERATIONS names).
ACTION_MAP: dict[str, str] = {
    "lie_down":      "lie",
    "bark_harder":   "bark harder",   # OPERATIONS key
    "shake_hand":    "handshake",     # OPERATIONS key (shake_hand is not a valid pidog name)
    "high_five":     "high five",     # OPERATIONS key (space, not underscore)
    "body_twisting": "twist body",    # OPERATIONS key
    "head_up":       "head_up_down",  # ActionDict
    "head_down":     "head_up_down",  # ActionDict
}

VALID_LED_MODES: set[str] = {"solid", "blink", "breath", "trail"}

_ACTION_RE = re.compile(r"<pidog_action>(.*?)</pidog_action>", re.DOTALL)
_LEDS_RE = re.compile(r"<pidog_leds>(.*?)</pidog_leds>", re.DOTALL)
_ALL_TAGS_RE = re.compile(
    r"<pidog_(?:action|leds)>.*?</pidog_(?:action|leds)>", re.DOTALL
)


@dataclass
class PiDogAction:
    """A validated physical action command."""

    action: str
    speed: int = 80

    def __post_init__(self) -> None:
        self.speed = max(0, min(100, self.speed))


@dataclass
class LEDCommand:
    """A validated LED command."""

    mode: str = "breath"
    color: str = "#333399"
    brightness: int = 80

    def __post_init__(self) -> None:
        self.brightness = max(0, min(100, self.brightness))


@dataclass
class ParsedResponse:
    """Result of parsing an AI response."""

    clean_text: str
    actions: list[PiDogAction]
    led_commands: list[LEDCommand]


def parse_response(text: str, default_speed: int = 80) -> ParsedResponse:
    """Extract all <pidog_action> and <pidog_leds> tags from response text.

    Invalid JSON inside tags is logged and skipped.
    Unknown action names are logged and skipped.
    Returns actions in order of appearance.
    """
    actions: list[PiDogAction] = []
    led_commands: list[LEDCommand] = []

    # Extract actions
    for match in _ACTION_RE.finditer(text):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in <pidog_action>: %s", raw)
            continue

        action_name = data.get("action", "")
        if action_name not in VALID_ACTIONS:
            logger.warning("Unknown action: %s", action_name)
            continue

        speed = data.get("speed", default_speed)
        if not isinstance(speed, (int, float)):
            speed = default_speed
        pidog_action = ACTION_MAP.get(action_name, action_name)
        actions.append(PiDogAction(action=pidog_action, speed=int(speed)))

    # Extract LED commands
    for match in _LEDS_RE.finditer(text):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in <pidog_leds>: %s", raw)
            continue

        mode = data.get("mode", "breath")
        if mode not in VALID_LED_MODES:
            logger.warning("Unknown LED mode: %s", mode)
            continue

        color = data.get("color", "#333399")
        brightness = data.get("brightness", 80)
        if not isinstance(brightness, (int, float)):
            brightness = 80
        led_commands.append(
            LEDCommand(mode=mode, color=str(color), brightness=int(brightness))
        )

    # Clean text: remove all tags
    clean_text = _ALL_TAGS_RE.sub("", text).strip()
    # Collapse multiple whitespace/newlines left by tag removal
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)

    return ParsedResponse(
        clean_text=clean_text,
        actions=actions,
        led_commands=led_commands,
    )


def strip_action_tags(text: str) -> str:
    """Remove all pidog tags from text (for TTS output)."""
    return _ALL_TAGS_RE.sub("", text).strip()
