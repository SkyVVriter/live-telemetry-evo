"""PTT input binding: keyboard keys and joystick/wheel buttons.

Keyboard capture uses ``pynput`` (cross-platform); joystick capture uses
``pygame`` SDL bindings for DirectInput wheel/pedal buttons.  Both are
optional dependencies — the binding system degrades gracefully.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from ..logbook import log


class BindingType(Enum):
    KEYBOARD = "keyboard"
    JOYSTICK = "joystick"


@dataclass
class PTTBinding:
    """A bound PTT control."""
    type: BindingType
    key_name: str = ""
    joy_id: int = -1
    button_id: int = -1

    @property
    def display_name(self) -> str:
        if self.type == BindingType.KEYBOARD:
            return f"Key: {self.key_name}"
        return f"Joy {self.joy_id} Btn {self.button_id}"

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "key_name": self.key_name,
            "joy_id": self.joy_id,
            "button_id": self.button_id,
        }

    @staticmethod
    def from_dict(d: dict) -> "PTTBinding":
        return PTTBinding(
            type=BindingType(d.get("type", "keyboard")),
            key_name=d.get("key_name", ""),
            joy_id=d.get("joy_id", -1),
            button_id=d.get("button_id", -1),
        )


class InputListener:
    """Listens for keyboard and joystick input for PTT activation.

    Two modes of operation:

    1. **Assign mode**: captures the next key/button press and returns it
       via a callback, then exits assign mode.
    2. **Active mode**: fires ``on_press`` / ``on_release`` callbacks when
       the bound control is pressed/released.
    """

    def __init__(self) -> None:
        self._binding: Optional[PTTBinding] = None
        self._on_press: Optional[Callable[[], None]] = None
        self._on_release: Optional[Callable[[], None]] = None
        self._assign_callback: Optional[Callable[[PTTBinding], None]] = None
        self._keyboard_listener = None
        self._joy_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pressed = False
        self._pygame_inited = False

    @property
    def binding(self) -> Optional[PTTBinding]:
        return self._binding

    def set_binding(self, binding: Optional[PTTBinding]) -> None:
        self._binding = binding

    def start(self, on_press: Callable[[], None],
              on_release: Callable[[], None]) -> None:
        """Start listening for the bound control."""
        self._on_press = on_press
        self._on_release = on_release
        self._stop.clear()
        self._start_keyboard_listener()
        self._start_joystick_listener()

    def stop(self) -> None:
        self._stop.set()
        self._stop_keyboard_listener()
        if self._joy_thread is not None:
            self._joy_thread.join(timeout=2.0)
            self._joy_thread = None

    def start_assign(self, callback: Callable[[PTTBinding], None]) -> None:
        """Enter assign mode: next key/button press is captured and
        passed to ``callback`` as a ``PTTBinding``."""
        self._assign_callback = callback
        self._stop.clear()
        self._start_keyboard_listener()
        self._start_joystick_listener()

    def stop_assign(self) -> None:
        self._assign_callback = None
        self.stop()

    def _start_keyboard_listener(self) -> None:
        self._stop_keyboard_listener()
        try:
            from pynput import keyboard  # type: ignore[import-untyped]
            self._keyboard_listener = keyboard.Listener(
                on_press=self._on_key_press,
                on_release=self._on_key_release,
            )
            self._keyboard_listener.start()
        except (ImportError, Exception) as e:
            log(f"[ptt/input] keyboard listener unavailable: {e}")

    def _stop_keyboard_listener(self) -> None:
        if self._keyboard_listener is not None:
            try:
                self._keyboard_listener.stop()
            except Exception:
                pass
            self._keyboard_listener = None

    def _on_key_press(self, key) -> None:
        try:
            key_name = key.char if hasattr(key, "char") and key.char else str(key)
        except AttributeError:
            key_name = str(key)
        key_name = key_name.replace("Key.", "")

        if self._assign_callback is not None:
            binding = PTTBinding(type=BindingType.KEYBOARD, key_name=key_name)
            cb = self._assign_callback
            self._assign_callback = None
            self.stop()
            cb(binding)
            return

        if (self._binding and self._binding.type == BindingType.KEYBOARD
                and self._binding.key_name == key_name):
            if not self._pressed:
                self._pressed = True
                if self._on_press:
                    self._on_press()

    def _on_key_release(self, key) -> None:
        try:
            key_name = key.char if hasattr(key, "char") and key.char else str(key)
        except AttributeError:
            key_name = str(key)
        key_name = key_name.replace("Key.", "")

        if (self._binding and self._binding.type == BindingType.KEYBOARD
                and self._binding.key_name == key_name):
            if self._pressed:
                self._pressed = False
                if self._on_release:
                    self._on_release()

    def _start_joystick_listener(self) -> None:
        if self._joy_thread is not None:
            return
        self._joy_thread = threading.Thread(
            target=self._joystick_loop, name="ptt-joystick", daemon=True)
        self._joy_thread.start()

    def _joystick_loop(self) -> None:
        try:
            import pygame  # type: ignore[import-untyped]
        except ImportError:
            log("[ptt/input] pygame not available for joystick input")
            return
        try:
            if not self._pygame_inited:
                pygame.init()
                pygame.joystick.init()
                self._pygame_inited = True
        except Exception as e:
            log(f"[ptt/input] pygame init failed: {e}")
            return

        prev_states: dict[tuple[int, int], bool] = {}
        while not self._stop.is_set():
            try:
                pygame.event.pump()
                for joy_idx in range(pygame.joystick.get_count()):
                    joy = pygame.joystick.Joystick(joy_idx)
                    joy.init()
                    for btn_idx in range(joy.get_numbuttons()):
                        pressed = joy.get_button(btn_idx)
                        key = (joy_idx, btn_idx)
                        was_pressed = prev_states.get(key, False)

                        if pressed and not was_pressed:
                            if self._assign_callback is not None:
                                binding = PTTBinding(
                                    type=BindingType.JOYSTICK,
                                    joy_id=joy_idx,
                                    button_id=btn_idx,
                                )
                                cb = self._assign_callback
                                self._assign_callback = None
                                self.stop()
                                cb(binding)
                                return
                            if (self._binding
                                    and self._binding.type == BindingType.JOYSTICK
                                    and self._binding.joy_id == joy_idx
                                    and self._binding.button_id == btn_idx):
                                self._pressed = True
                                if self._on_press:
                                    self._on_press()

                        elif not pressed and was_pressed:
                            if (self._binding
                                    and self._binding.type == BindingType.JOYSTICK
                                    and self._binding.joy_id == joy_idx
                                    and self._binding.button_id == btn_idx):
                                self._pressed = False
                                if self._on_release:
                                    self._on_release()

                        prev_states[key] = pressed
            except Exception:
                pass
            self._stop.wait(0.02)


def list_joysticks() -> list[dict[str, str | int]]:
    """Return available joysticks/wheels."""
    try:
        import pygame  # type: ignore[import-untyped]
        pygame.init()
        pygame.joystick.init()
        joys = []
        for i in range(pygame.joystick.get_count()):
            j = pygame.joystick.Joystick(i)
            j.init()
            joys.append({
                "index": i,
                "name": j.get_name(),
                "buttons": j.get_numbuttons(),
            })
        return joys
    except (ImportError, Exception):
        return []
