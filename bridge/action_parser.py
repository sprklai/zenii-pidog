"""Parse <pidog_action> and <pidog_leds> tags from AI response text."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Canonical action names understood by the AI/SOUL.md persona.
# Source of truth: official SunFounder voice-assistant example (20_voice_active_dog_gpt.py)
# Entries that differ from pidog's do_action() keys are remapped in ACTION_MAP below.
VALID_ACTIONS: set[str] = {
    # Movement
    "forward",
    "backward",
    "turn_left",
    "turn_right",
    # Posture
    "sit",
    "stand",
    "lie_down",
    "stretch",
    "push_up",
    # Vocals
    "bark",
    "bark_harder",
    "howling",
    "pant",
    # Expressive
    "wag_tail",
    "shake_head",
    "nod",
    "think",
    "recall",
    "surprise",
    "fluster",
    # Interaction
    "shake_hand",
    "high_five",
    "lick_hand",
    "scratch",
    # Head motion
    "tilting_head_left",
    "tilting_head_right",
    "head_up",
    "head_down",
    "relax_neck",
    # Legacy/compound
    "body_twisting",
}

# Maps AI-facing action names → pidog do_action() keys where they differ.
# pidog accepts both ActionDict keys (underscores) and OPERATIONS keys (spaces).
# Actions not listed here pass through unchanged — they are valid OPERATIONS names
# as-is (bark, howling, pant, nod, think, recall, surprise, fluster, scratch).
ACTION_MAP: dict[str, str] = {
    # Posture
    "lie_down":      "lie",
    # Vocals
    "bark_harder":   "bark harder",    # OPERATIONS key uses space
    # Interaction
    "shake_hand":    "handshake",      # OPERATIONS key (no underscore)
    "high_five":     "high five",      # OPERATIONS key uses space
    "lick_hand":     "lick hand",      # OPERATIONS key uses space
    # Expressive
    "relax_neck":    "relax neck",     # OPERATIONS key uses space
    "body_twisting": "twist body",     # OPERATIONS key uses space
    # Head motion
    "head_up":       "head_up_down",   # ActionDict
    "head_down":     "head_up_down",   # ActionDict
}

VALID_LED_MODES: set[str] = {"solid", "blink", "breath", "trail", "listen", "bark"}

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


# Common LLM-invented aliases → canonical VALID_ACTIONS names
_ACTION_ALIASES: dict[str, str] = {
    # stand
    "stand_up": "stand", "get_up": "stand", "rise": "stand", "standing": "stand",
    # sit
    "sit_down": "sit", "stay": "sit", "sitting": "sit",
    # lie_down
    "lay_down": "lie_down", "lie": "lie_down", "sleep": "lie_down", "lying": "lie_down",
    # stretch / push_up
    "bow": "stretch", "stretching": "stretch",
    "push_up": "push_up", "pushup": "push_up", "jump": "push_up",
    # movement
    "walk": "forward", "go_forward": "forward", "move_forward": "forward",
    "go_back": "backward", "back_up": "backward", "retreat": "backward",
    "go_left": "turn_left", "turn_l": "turn_left",
    "go_right": "turn_right", "turn_r": "turn_right",
    # wag_tail
    "wag": "wag_tail", "tail_wag": "wag_tail", "shake_tail": "wag_tail",
    "wagging": "wag_tail", "wag_my_tail": "wag_tail",
    # shake_head
    "head_shake": "shake_head", "no": "shake_head",
    # shake_hand
    "handshake": "shake_hand", "paw": "shake_hand", "give_paw": "shake_hand",
    "shake_hands": "shake_hand", "give_me_your_paw": "shake_hand",
    # high_five
    "high_5": "high_five", "hi5": "high_five",
    # lick_hand
    "kiss": "lick_hand", "lick": "lick_hand",
    # head motion
    "tilt_left": "tilting_head_left", "look_left": "tilting_head_left",
    "tilt_right": "tilting_head_right", "look_right": "tilting_head_right",
    "look_up": "head_up", "head_raise": "head_up",
    "look_down": "head_down", "head_lower": "head_down",
    # body_twisting
    "twist": "body_twisting", "spin": "body_twisting",
    "twirl": "body_twisting", "dance": "body_twisting", "rotate": "body_twisting",
    # bark
    "woof": "bark", "speak": "bark", "barking": "bark",
    "bark_hard": "bark_harder", "bark_loud": "bark_harder", "loud_bark": "bark_harder",
    # howling
    "howl": "howling", "sing": "howling",
    # pant
    "panting": "pant", "breathe": "pant",
    # expressive
    "thinking": "think", "surprised": "surprise", "flustered": "fluster",
    "scratching": "scratch", "recalling": "recall",
    "relax": "relax_neck",
}


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

        # Normalize: lowercase + collapse spaces/hyphens to underscores
        action_name = data.get("action", "").lower().strip().replace(" ", "_").replace("-", "_")
        # Resolve common LLM aliases before validation
        action_name = _ACTION_ALIASES.get(action_name, action_name)
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
