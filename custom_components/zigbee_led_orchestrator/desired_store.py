"""Store only the latest desired state for every configured light."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from .models import DesiredState, IntentPatch
from .protocols import Clock


class DesiredStateStore:
    """Keep immutable snapshots and assign one generation per changed operation.

    Methods are synchronous and must run in the same event loop: no observer can
    see a partly applied multi-light update between method calls.
    """

    def __init__(self, light_ids: Iterable[str], *, clock: Clock) -> None:
        ids = tuple(light_ids)
        if any(not isinstance(item, str) or not item for item in ids):
            raise ValueError("light_ids must contain nonempty strings")
        if len(set(ids)) != len(ids):
            raise ValueError("light_ids contains duplicates")
        self._clock = clock
        self._generation = 0
        now = clock.now()
        self._states = {
            light_id: DesiredState(
                on=False,
                brightness=None,
                color_temp_kelvin=None,
                generation=0,
                source="initial",
                updated_at=now,
            )
            for light_id in ids
        }

    def get(self, light_id: str) -> DesiredState:
        """Return the current immutable snapshot or raise KeyError."""
        return self._states[light_id]

    def apply(
        self, light_ids: tuple[str, ...], patch: IntentPatch, *, source: str
    ) -> dict[str, DesiredState]:
        """Atomically apply the patch to all requested configured lights."""
        if not isinstance(patch, IntentPatch):
            raise TypeError("patch must be an IntentPatch")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be a nonempty string")
        if not light_ids or len(set(light_ids)) != len(light_ids):
            raise ValueError("light_ids must be nonempty and unique")

        # Resolve the complete batch before making any change, including changing
        # the counter or querying the clock.
        previous = {light_id: self._states[light_id] for light_id in light_ids}
        candidates: dict[str, DesiredState] = {}
        for light_id, state in previous.items():
            candidate = replace(
                state,
                on=state.on if patch.on is None else patch.on,
                brightness=state.brightness
                if patch.brightness is None
                else patch.brightness,
                color_temp_kelvin=(
                    state.color_temp_kelvin
                    if patch.color_temp_kelvin is None
                    else patch.color_temp_kelvin
                ),
            )
            if candidate != state:
                candidates[light_id] = candidate

        if candidates:
            now = self._clock.now()
            generation = self._generation + 1
            updated = {
                light_id: replace(
                    state, generation=generation, source=source, updated_at=now
                )
                for light_id, state in candidates.items()
            }
            self._states.update(updated)
            self._generation = generation
        return {light_id: self._states[light_id] for light_id in light_ids}
