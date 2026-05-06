"""Convert released marbles to a binary-based cannon routing sequence.

Flow:
1. Count user-released marbles from starter notifications (1..15).
2. End counting after inactivity or once 15 are reached.
3. Compute popcount of the released amount.
4. Fire marbles via RED cannon channel with state-safe switch control:
	- keep marbles are confirmed by trigger feedback,
	- missing feedback causes an automatic switch toggle and retry,
	- discard marbles are only sent after all keep confirmations are complete.
5. Sort the binary marbles directly in-flight (no lever staging):
	- confirm keep-path exit with red trigger,
	- confirm binary-sequence entry with green trigger,
	- wait travel time before changing switches for the next marble.
"""

import argparse
import asyncio
import importlib
import sys
import time
from pathlib import Path
from typing import Any, Optional, Tuple


def load_gravitrax_modules() -> Tuple[Any, Any]:
	"""Load gravitrax modules, including a local workspace fallback path."""
	try:
		bridge_module = importlib.import_module("gravitraxconnect.gravitrax_bridge")
		constants_module = importlib.import_module("gravitraxconnect.gravitrax_constants")
		return bridge_module, constants_module
	except ModuleNotFoundError:
		local_library_path = (
			Path(__file__).resolve().parent
			/ "GraviTrax-Connect"
			/ "GraviTrax-Connect-Python-Library"
		)
		if str(local_library_path) not in sys.path:
			sys.path.insert(0, str(local_library_path))

		bridge_module = importlib.import_module("gravitraxconnect.gravitrax_bridge")
		constants_module = importlib.import_module("gravitraxconnect.gravitrax_constants")
		return bridge_module, constants_module


gb, gc = load_gravitrax_modules()


class ReleaseState:
	"""Mutable state used while counting starter releases."""

	def __init__(self, max_marbles: int):
		self.max_marbles = max_marbles
		self.count = 0
		self.last_release_ts: Optional[float] = None
		self.last_red_trigger_ts: Optional[float] = None
		self.last_green_trigger_ts: Optional[float] = None
		self.last_blue_trigger_ts: Optional[float] = None
		self.finished = asyncio.Event()
		self.mode = "count_releases"
		self.required_passes = 0
		self.passed_count = 0
		self.passes_finished = asyncio.Event()
		self.pass_event = asyncio.Event()
		self.red_trigger_event = asyncio.Event()
		self.green_trigger_event = asyncio.Event()
		self.blue_trigger_event = asyncio.Event()
		self.binary_entry_count = 0
		self.binary_entry_event = asyncio.Event()

	def add_release(self) -> None:
		"""Register one release if the limit has not been reached."""
		if self.count >= self.max_marbles:
			return
		self.count += 1
		self.last_release_ts = time.monotonic()
		if self.count >= self.max_marbles:
			self.finished.set()

	def reset_release_counting(self) -> None:
		"""Prepare state for counting a new release batch."""
		self.mode = "count_releases"
		self.count = 0
		self.last_release_ts = None
		self.finished.clear()

	def start_waiting_for_passes(self, required_passes: int) -> None:
		"""Switch callback mode to counting trigger passes on keep path."""
		self.mode = "wait_keep_passes"
		self.required_passes = max(0, required_passes)
		self.passed_count = 0
		self.passes_finished.clear()
		self.pass_event.clear()
		if self.required_passes == 0:
			self.passes_finished.set()

	def stop_waiting_for_passes(self) -> None:
		"""Disable pass counting in the notification callback."""
		self.mode = "done"

	def add_pass(self) -> None:
		"""Register a single keep-path trigger confirmation."""
		if self.mode != "wait_keep_passes":
			return
		if self.passed_count >= self.required_passes:
			return
		self.passed_count += 1
		self.pass_event.set()
		if self.passed_count >= self.required_passes:
			self.passes_finished.set()

	def reset_binary_entry_counter(self) -> None:
		"""Reset in-flight binary-entry counter for a new cycle."""
		self.binary_entry_count = 0
		self.binary_entry_event.clear()

	def add_binary_entry(self) -> None:
		"""Register one binary-entry confirmation from trigger feedback."""
		if self.mode != "wait_binary_entry":
			return
		self.binary_entry_count += 1
		self.binary_entry_event.set()


class SwitchState:
	"""Track switch parity relative to the assumed default positions.

	Important: red switch signals affect both the discard red switch and
	the binary red switch in the described hardware setup.
	"""

	def __init__(self):
		self.red_toggled = False
		self.green_toggled = False
		self.blue_toggled = False

	def on_toggle(self, color_channel: int) -> None:
		"""Update internal state after a successful switch toggle signal."""
		if color_channel == gc.COLOR_RED:
			self.red_toggled = not self.red_toggled
		elif color_channel == gc.COLOR_GREEN:
			self.green_toggled = not self.green_toggled
		elif color_channel == gc.COLOR_BLUE:
			self.blue_toggled = not self.blue_toggled

	def binary_tuple(self) -> tuple[bool, bool, bool]:
		"""Return (red, green, blue) binary-routing switch states."""
		return (self.red_toggled, self.green_toggled, self.blue_toggled)


def parse_color_arg(value: str) -> int:
	"""Map color names used on CLI to gravitrax color constants."""
	mapping = {
		"red": gc.COLOR_RED,
		"green": gc.COLOR_GREEN,
		"blue": gc.COLOR_BLUE,
	}
	try:
		return mapping[value.lower()]
	except KeyError as exc:
		raise argparse.ArgumentTypeError(
			"Invalid color. Use one of: red, green, blue."
		) from exc


async def send_switch_toggle(
	bridge: Any,
	switch_state: SwitchState,
	color_channel: int,
	resends: int,
	resend_gap: float,
	switch_settle: float,
) -> None:
	"""Send a switch toggle signal and update local switch state parity."""
	await bridge.send_signal(
		status=gc.STATUS_SWITCH,
		color_channel=color_channel,
		resends=resends,
		resend_gap=resend_gap,
	)
	switch_state.on_toggle(color_channel)
	if switch_settle > 0:
		await asyncio.sleep(switch_settle)


async def reset_switches_to_default(
	bridge: Any,
	switch_state: SwitchState,
	resends: int,
	resend_gap: float,
	switch_settle: float,
	reset_wait: float,
) -> None:
	"""Return tracked switch states to default before the next loop iteration."""
	if switch_state.blue_toggled:
		await send_switch_toggle(
			bridge,
			switch_state,
			gc.COLOR_BLUE,
			resends,
			resend_gap,
			switch_settle,
		)
	if switch_state.green_toggled:
		await send_switch_toggle(
			bridge,
			switch_state,
			gc.COLOR_GREEN,
			resends,
			resend_gap,
			switch_settle,
		)
	if switch_state.red_toggled:
		await send_switch_toggle(
			bridge,
			switch_state,
			gc.COLOR_RED,
			resends,
			resend_gap,
			switch_settle,
		)

	if reset_wait > 0:
		await asyncio.sleep(reset_wait)


def disconnect_callback(bridge: Any, disconnected_event: asyncio.Event, **kwargs) -> None:
	"""Log how the connection ended and release waiting tasks."""
	if kwargs.get("user_disconnected"):
		gb.log_print("Disconnected from bridge.", bridge=bridge)
	else:
		gb.log_print("Bridge disconnected unexpectedly.", bridge=bridge, level="WARNING")
	disconnected_event.set()


def is_user_release_signal(signal: dict[str, Any], starter_color: int) -> bool:
	"""Decide whether a notification represents a user marble release."""
	if signal.get("Header") != gc.MSG_DEFAULT_HEADER:
		return False

	stone = signal.get("Stone")
	status = signal.get("Status")
	color = signal.get("Color")

	if status == gc.STATUS_STARTER_PRESS:
		return True

	if stone == gc.STONE_STARTER and color == starter_color:
		return True

	return False


def is_keep_path_pass_signal(signal: dict[str, Any], pass_trigger_color: int) -> bool:
	"""Decide whether a notification confirms one marble passed the keep path."""
	if signal.get("Header") != gc.MSG_DEFAULT_HEADER:
		return False

	return (
		signal.get("Stone") == gc.STONE_TRIGGER
		and signal.get("Color") == pass_trigger_color
	)


def is_binary_entry_signal(signal: dict[str, Any], binary_entry_trigger_color: int) -> bool:
	"""Decide whether a notification confirms marble entered binary sequence."""
	if signal.get("Header") != gc.MSG_DEFAULT_HEADER:
		return False

	return (
		signal.get("Stone") == gc.STONE_TRIGGER
		and signal.get("Color") == binary_entry_trigger_color
	)


def make_notification_callback(
	state: ReleaseState,
	starter_color: int,
	pass_trigger_color: int,
	binary_entry_trigger_color: int,
	blue_trigger_color: int,
):
	"""Create callback that counts starter release notifications."""

	async def notification_callback(bridge: Any, **signal) -> None:
		# Record trigger timestamps independent of current mode.
		if (
			signal.get("Header") == gc.MSG_DEFAULT_HEADER
			and signal.get("Stone") == gc.STONE_TRIGGER
		):
			now = time.monotonic()
			trigger_color = signal.get("Color")
			if trigger_color == pass_trigger_color:
				state.last_red_trigger_ts = now
				state.red_trigger_event.set()
			if trigger_color == binary_entry_trigger_color:
				state.last_green_trigger_ts = now
				state.green_trigger_event.set()
			if trigger_color == blue_trigger_color:
				state.last_blue_trigger_ts = now
				state.blue_trigger_event.set()

		if state.mode == "count_releases":
			if not is_user_release_signal(signal, starter_color):
				return

			prev_count = state.count
			state.add_release()
			if state.count != prev_count:
				gb.log_print(
					f"Release detected -> {state.count} marble(s)",
					bridge=bridge,
					level="INFO",
				)
			return

		if state.mode == "wait_keep_passes":
			if not is_keep_path_pass_signal(signal, pass_trigger_color):
				return

			prev_passed = state.passed_count
			state.add_pass()
			if state.passed_count != prev_passed:
				gb.log_print(
					f"Keep-path pass detected -> {state.passed_count}/{state.required_passes}",
					bridge=bridge,
					level="INFO",
				)
			return

		if state.mode == "wait_binary_entry":
			if not is_binary_entry_signal(signal, binary_entry_trigger_color):
				return

			prev_entry_count = state.binary_entry_count
			state.add_binary_entry()
			if state.binary_entry_count != prev_entry_count:
				gb.log_print(
					f"Binary entry confirmed -> {state.binary_entry_count}",
					bridge=bridge,
					level="INFO",
				)

	return notification_callback


async def resolve_target_address(mac: Optional[str], scan_timeout: int) -> Optional[str]:
	"""Return a bridge MAC address, either from argument or scan result."""
	if mac:
		return mac

	gb.log_print(f"Scanning for bridges (timeout={scan_timeout}s)...")
	found = await gb.scan_bridges(timeout=scan_timeout, do_print=True)
	if not found:
		return None
	return found[0]


async def wait_for_release_window(state: ReleaseState, inactivity_seconds: float) -> None:
	"""Finish once at least one marble was counted and input became idle."""
	while not state.finished.is_set():
		if state.count > 0 and state.last_release_ts is not None:
			idle_for = time.monotonic() - state.last_release_ts
			if idle_for >= inactivity_seconds:
				state.finished.set()
				return
		await asyncio.sleep(0.05)


async def wait_for_pass_target(state: ReleaseState, target_passes: int) -> None:
	"""Wait until the keep-path pass counter reaches target_passes."""
	while state.passed_count < target_passes:
		await state.pass_event.wait()
		state.pass_event.clear()


async def wait_for_binary_entry_target(state: ReleaseState, target_count: int) -> None:
	"""Wait until binary-entry counter reaches target_count."""
	while state.binary_entry_count < target_count:
		await state.binary_entry_event.wait()
		state.binary_entry_event.clear()


async def count_from_starter(
	state: ReleaseState,
	max_marbles: int,
	release_inactivity: float,
	disconnected_event: asyncio.Event,
) -> int | None:
	"""Count released marbles from starter notifications for one cycle."""
	state.max_marbles = max_marbles
	state.reset_release_counting()

	gb.log_print(
		f"Release between 1 and {max_marbles} marbles from starter now.",
		level="INFO",
	)
	gb.log_print(
		f"Counting ends after {release_inactivity}s of no new releases.",
		level="INFO",
	)

	release_wait_task = asyncio.create_task(
		wait_for_release_window(state, release_inactivity)
	)
	disconnect_wait_task = asyncio.create_task(disconnected_event.wait())

	done, pending = await asyncio.wait(
		{release_wait_task, disconnect_wait_task},
		return_when=asyncio.FIRST_COMPLETED,
	)
	for task in pending:
		task.cancel()
	await asyncio.gather(*pending, return_exceptions=True)

	if disconnect_wait_task in done and disconnected_event.is_set():
		gb.log_print("Bridge disconnected before release phase finished.", level="ERROR")
		return None

	if state.count <= 0:
		gb.log_print("No marbles were released in this cycle.", level="WARNING")
		return 0

	return state.count


async def send_cannon_sequence(
	bridge: Any,
	state: ReleaseState,
	switch_state: SwitchState,
	released_value: int,
	total_count: int,
	keep_count: int,
	switch_toggle_color: int,
	binary_entry_timeout: float,
	pass_timeout: float,
	cannon_gap: float,
	selection_travel_time: float,
	binary_cycle_gap: float,
	switch_settle: float,
	resends: int,
	resend_gap: float,
	) -> bool:
	"""Fire marbles through cannon with feedback-based, state-safe switch routing."""
	# Red channel controls both the exit/discard switch and the binary red switch.
	# We therefore enforce explicit red parity for exit routing instead of blindly
	# toggling once at phase boundaries.
	keep_exit_red_toggled = False
	discard_exit_red_toggled = True

	shots_sent = 0
	auto_toggles = 0
	timing_shot_to_red: list[float] = []
	timing_red_to_green: list[float] = []
	timing_green_to_blue: list[float] = []
	bit_targets = binary_bits_desc(released_value)
	if len(bit_targets) != keep_count:
		gb.log_print(
			"Mismatch between keep count and binary target list.",
			level="ERROR",
		)
		return False

	if keep_count > 0:
		state.start_waiting_for_passes(keep_count)
		state.reset_binary_entry_counter()
		gb.log_print(
			"State-safe keep phase started: route each binary marble directly in-flight"
		)

		for index, bit_value in enumerate(bit_targets, start=1):
			target_red, target_green, target_blue = target_switch_state_for_bit(bit_value)

			# Pre-position downstream binary switches early; leave red unchanged until
			# the marble has passed the red trigger.
			await set_binary_switch_state(
				bridge=bridge,
				switch_state=switch_state,
				target_state=(switch_state.red_toggled, target_green, target_blue),
				switch_settle=switch_settle,
				resends=resends,
				resend_gap=resend_gap,
			)

			while True:
				if shots_sent >= total_count:
					gb.log_print(
						"Not enough marbles left to satisfy keep confirmations.",
						level="ERROR",
					)
					return False

				# Ensure marbles leave through keep path before each keep shot.
				if switch_state.red_toggled != keep_exit_red_toggled:
					await send_switch_toggle(
						bridge=bridge,
						switch_state=switch_state,
						color_channel=switch_toggle_color,
						resends=resends,
						resend_gap=resend_gap,
						switch_settle=switch_settle,
					)
					auto_toggles += 1

				next_pass_target = state.passed_count + 1
				state.pass_event.clear()
				state.red_trigger_event.clear()
				state.mode = "wait_keep_passes"
				shot_ts = time.monotonic()
				await bridge.send_signal(
					status=gc.STATUS_CANNON,
					color_channel=gc.COLOR_RED,
					resends=resends,
					resend_gap=resend_gap,
				)
				shots_sent += 1
				if cannon_gap > 0:
					await asyncio.sleep(cannon_gap)

				try:
					await asyncio.wait_for(
						wait_for_pass_target(state, next_pass_target),
						timeout=pass_timeout,
					)
				except asyncio.TimeoutError:
					gb.log_print(
						"No keep-path red trigger confirmation. Auto-toggling exit switch and retrying this bit.",
						level="WARNING",
					)
					await send_switch_toggle(
						bridge=bridge,
						switch_state=switch_state,
						color_channel=switch_toggle_color,
						resends=resends,
						resend_gap=resend_gap,
						switch_settle=switch_settle,
					)
					auto_toggles += 1
					continue

				break

			red_ts = state.last_red_trigger_ts
			if red_ts is not None and red_ts >= shot_ts:
				timing_shot_to_red.append(red_ts - shot_ts)

			# Finalize only red routing after red trigger confirmation.
			gb.log_print(
				f"Red trigger confirmed. Finalizing red route for binary marble {index}/{keep_count} to bit {bit_value}",
				level="INFO",
			)
			if switch_state.red_toggled != target_red:
				await send_switch_toggle(
					bridge=bridge,
					switch_state=switch_state,
					color_channel=gc.COLOR_RED,
					resends=resends,
					resend_gap=resend_gap,
					switch_settle=switch_settle,
				)

			next_entry_target = state.binary_entry_count + 1
			state.binary_entry_event.clear()
			state.green_trigger_event.clear()
			state.mode = "wait_binary_entry"
			try:
				await asyncio.wait_for(
					wait_for_binary_entry_target(state, next_entry_target),
					timeout=binary_entry_timeout,
				)
			except asyncio.TimeoutError:
				gb.log_print(
					"Timed out waiting for green binary-entry trigger confirmation.",
					level="ERROR",
				)
				return False

			green_ts = state.last_green_trigger_ts
			if red_ts is not None and green_ts is not None and green_ts >= red_ts:
				timing_red_to_green.append(green_ts - red_ts)

			blue_ts = None
			state.blue_trigger_event.clear()
			if selection_travel_time > 0:
				try:
					await asyncio.wait_for(
						state.blue_trigger_event.wait(),
						timeout=selection_travel_time,
					)
					blue_ts = state.last_blue_trigger_ts
				except asyncio.TimeoutError:
					gb.log_print(
						"Blue trigger not received within selection-travel-time timeout; continuing with fallback.",
						level="WARNING",
					)
			else:
				blue_ts = state.last_blue_trigger_ts

			if green_ts is not None and blue_ts is not None and blue_ts >= green_ts:
				timing_green_to_blue.append(blue_ts - green_ts)

			shot_red_text = "n/a"
			if red_ts is not None and red_ts >= shot_ts:
				shot_red_text = f"{(red_ts - shot_ts):.3f}s"
			red_green_text = "n/a"
			if red_ts is not None and green_ts is not None and green_ts >= red_ts:
				red_green_text = f"{(green_ts - red_ts):.3f}s"
			green_blue_text = "n/a"
			if green_ts is not None and blue_ts is not None and blue_ts >= green_ts:
				green_blue_text = f"{(blue_ts - green_ts):.3f}s"

			gb.log_print(
				f"Timing marble {index}/{keep_count}: shot->red={shot_red_text}, "
				f"red->green={red_green_text}, green->blue={green_blue_text}",
				level="INFO",
			)
			if binary_cycle_gap > 0:
				await asyncio.sleep(binary_cycle_gap)

		state.stop_waiting_for_passes()
		gb.log_print(
			f"Keep phase complete with {state.passed_count}/{keep_count} red-pass confirmations "
			f"after {shots_sent} shot(s)."
		)
		if timing_shot_to_red or timing_red_to_green or timing_green_to_blue:
			avg_shot_red = "n/a"
			if timing_shot_to_red:
				avg_shot_red = f"{(sum(timing_shot_to_red) / len(timing_shot_to_red)):.3f}s"
			avg_red_green = "n/a"
			if timing_red_to_green:
				avg_red_green = f"{(sum(timing_red_to_green) / len(timing_red_to_green)):.3f}s"
			avg_green_blue = "n/a"
			if timing_green_to_blue:
				avg_green_blue = (
					f"{(sum(timing_green_to_blue) / len(timing_green_to_blue)):.3f}s"
				)

			gb.log_print(
				"Timing average: "
				f"shot->red={avg_shot_red}, red->green={avg_red_green}, "
				f"green->blue={avg_green_blue}",
				level="INFO",
			)

	remaining = total_count - shots_sent
	if remaining > 0:
		gb.log_print("All keep marbles confirmed. Ensuring DISCARD path")
		if switch_state.red_toggled != discard_exit_red_toggled:
			await send_switch_toggle(
				bridge=bridge,
				switch_state=switch_state,
				color_channel=switch_toggle_color,
				resends=resends,
				resend_gap=resend_gap,
				switch_settle=switch_settle,
			)
			auto_toggles += 1

		gb.log_print(f"Firing {remaining} marble(s) to DISCARD path via cannon")
		await bridge.send_periodic(
			status=gc.STATUS_CANNON,
			color_channel=gc.COLOR_RED,
			count=remaining,
			gap=cannon_gap,
			resends=resends,
			resend_gap=resend_gap,
		)

		# Prevent reset toggles from interfering with marbles that are still
		# traversing the discard/binary section.
		if selection_travel_time > 0:
			gb.log_print(
				f"Waiting {selection_travel_time:.2f}s for discard marble(s) to clear before reset",
				level="INFO",
			)
			await asyncio.sleep(selection_travel_time)

	if auto_toggles > 0:
		gb.log_print(f"Switch auto-toggles performed: {auto_toggles}", level="INFO")

	return True


def binary_bits_desc(value: int) -> list[int]:
	"""Return set bit values in descending order for 4-bit range: 8,4,2,1."""
	return [bit for bit in (8, 4, 2, 1) if value & bit]


def target_switch_state_for_bit(bit_value: int) -> tuple[bool, bool, bool]:
	"""Return desired (red, green, blue) toggle states for a target bit lane.

	Topology used here:
	- red switch: default -> green branch, toggled -> bit 1
	- green switch: default -> blue branch, toggled -> bit 2
	- blue switch: default -> bit 8, toggled -> bit 4
	"""
	mapping = {
		8: (False, False, False),
		4: (False, False, True),
		2: (False, True, False),
		1: (True, False, False),
	}
	if bit_value not in mapping:
		raise ValueError(f"Unsupported bit value for routing: {bit_value}")
	return mapping[bit_value]


async def set_binary_switch_state(
	bridge: Any,
	switch_state: SwitchState,
	target_state: tuple[bool, bool, bool],
	switch_settle: float,
	resends: int,
	resend_gap: float,
) -> tuple[bool, bool, bool]:
	"""Toggle colored switch channels until current state matches target state."""
	red_state, green_state, blue_state = switch_state.binary_tuple()
	target_red, target_green, target_blue = target_state

	# Toggle only changed channels to keep red-channel side effects as low as possible.
	if blue_state != target_blue:
		await send_switch_toggle(
			bridge=bridge,
			switch_state=switch_state,
			color_channel=gc.COLOR_BLUE,
			resends=resends,
			resend_gap=resend_gap,
			switch_settle=switch_settle,
		)
		blue_state = switch_state.blue_toggled

	if green_state != target_green:
		await send_switch_toggle(
			bridge=bridge,
			switch_state=switch_state,
			color_channel=gc.COLOR_GREEN,
			resends=resends,
			resend_gap=resend_gap,
			switch_settle=switch_settle,
		)
		green_state = switch_state.green_toggled

	if red_state != target_red:
		await send_switch_toggle(
			bridge=bridge,
			switch_state=switch_state,
			color_channel=gc.COLOR_RED,
			resends=resends,
			resend_gap=resend_gap,
			switch_settle=switch_settle,
		)
		red_state = switch_state.red_toggled

	return red_state, green_state, blue_state


async def run(args: argparse.Namespace) -> int:
	"""Connect, count released marbles, then execute binary routing sequence."""
	bridge = gb.Bridge()
	disconnected_event = asyncio.Event()
	state = ReleaseState(max_marbles=args.max_marbles)
	switch_state = SwitchState()
	bridge_mode_active = False

	gb.logger.disabled = False
	gb.log_set_level(args.log_level)
	gb.log_print("Starting marbles2binary controller", level="INFO")

	target_addr = await resolve_target_address(args.mac, args.scan_timeout)
	if not target_addr:
		gb.log_print("No Gravitrax bridge found. Aborting.", level="ERROR")
		return 1

	gb.log_print(f"Connecting to bridge {target_addr}...")
	connected = await bridge.connect(
		target_addr,
		by_name=False,
		dc_callback=lambda b, **kw: disconnect_callback(
			b, disconnected_event=disconnected_event, **kw
		),
	)
	if not connected:
		gb.log_print("Could not connect to bridge.", level="ERROR")
		return 1

	starter_color = parse_color_arg(args.starter_color)
	switch_toggle_color = parse_color_arg(args.switch_toggle_color)
	pass_trigger_color = parse_color_arg(args.pass_trigger_color)
	binary_entry_trigger_color = parse_color_arg(args.binary_entry_trigger_color)
	blue_trigger_color = parse_color_arg(args.blue_trigger_color)

	try:
		await bridge.start_bridge_mode()
		bridge_mode_active = True
		gb.log_print("Bridge-only mode enabled", level="INFO")

		await bridge.notification_enable(
			make_notification_callback(
				state,
				starter_color=starter_color,
				pass_trigger_color=pass_trigger_color,
				binary_entry_trigger_color=binary_entry_trigger_color,
				blue_trigger_color=blue_trigger_color,
			)
		)
		gb.log_print(
			f"Release between 1 and {args.max_marbles} marbles from starter now.",
			level="INFO",
		)
		gb.log_print(
			f"Counting ends after {args.release_inactivity}s of no new releases.",
			level="INFO",
		)
		gb.log_print(
			"Loop mode: waiting for starter releases each cycle.",
			level="INFO",
		)

		while True:
			if disconnected_event.is_set():
				gb.log_print("Bridge disconnected. Stopping loop.", level="ERROR")
				return 1

			counted = await count_from_starter(
				state=state,
				max_marbles=args.max_marbles,
				release_inactivity=args.release_inactivity,
				disconnected_event=disconnected_event,
			)
			if counted is None:
				return 1
			if counted == 0:
				continue
			released = counted

			binary_repr = format(released, "b")
			keep_count = released.bit_count()
			discard_count = released - keep_count
			gb.log_print(
				f"Released={released}, Binary={binary_repr}, Keep(popcount)={keep_count}, "
				f"Discard={discard_count}",
				level="INFO",
			)

			sequence_ok = await send_cannon_sequence(
				bridge=bridge,
				state=state,
				switch_state=switch_state,
				released_value=released,
				total_count=released,
				keep_count=keep_count,
				switch_toggle_color=switch_toggle_color,
				binary_entry_timeout=args.binary_entry_timeout,
				pass_timeout=args.pass_timeout,
				cannon_gap=args.cannon_gap,
				selection_travel_time=args.selection_travel_time,
				binary_cycle_gap=args.binary_cycle_gap,
				switch_settle=args.switch_settle,
				resends=args.resends,
				resend_gap=args.resend_gap,
			)
			if not sequence_ok:
				return 1

			gb.log_print("Cycle finished. Resetting switches to default...", level="INFO")
			await reset_switches_to_default(
				bridge=bridge,
				switch_state=switch_state,
				resends=args.resends,
				resend_gap=args.resend_gap,
				switch_settle=args.switch_settle,
				reset_wait=args.reset_wait,
			)
			gb.log_print("Reset done. Ready for next cycle.", level="INFO")

		await bridge.notification_disable()
		return 0
	finally:
		if await bridge.is_connected():
			try:
				gb.log_print(
					"Final cleanup: resetting switches to start state before disconnect.",
					level="INFO",
				)
				await reset_switches_to_default(
					bridge=bridge,
					switch_state=switch_state,
					resends=args.resends,
					resend_gap=args.resend_gap,
					switch_settle=args.switch_settle,
					reset_wait=args.reset_wait,
				)
			except Exception as exc:
				gb.log_print(
					f"Reset before disconnect failed: {type(exc).__name__}: {exc}",
					level="WARNING",
				)

			if bridge_mode_active:
				await bridge.stop_bridge_mode()
				gb.log_print("Bridge-only mode disabled", level="INFO")

			try:
				await bridge.notification_disable()
			except Exception:
				pass

			await bridge.disconnect()
		try:
			await asyncio.wait_for(disconnected_event.wait(), timeout=3)
		except asyncio.TimeoutError:
			gb.log_print("Disconnect callback timeout reached.", level="WARNING")


def build_parser() -> argparse.ArgumentParser:
	"""Create command-line argument parser."""
	parser = argparse.ArgumentParser(
		description="Count starter releases, then route cannon marbles by binary popcount."
	)
	parser.add_argument(
		"--mac",
		default=None,
		help="Bridge MAC address. If omitted, script scans and picks the first bridge.",
	)
	parser.add_argument(
		"--scan-timeout",
		type=int,
		default=10,
		help="Scan timeout in seconds when --mac is not provided.",
	)
	parser.add_argument(
		"--max-marbles",
		type=int,
		default=15,
		help="Upper limit for counted starter releases.",
	)
	parser.add_argument(
		"--release-inactivity",
		type=float,
		default=2.0,
		help="Seconds without new release before counting ends.",
	)
	parser.add_argument(
		"--starter-color",
		default="red",
		choices=["red", "green", "blue"],
		help="Starter color used as additional release detection signal.",
	)
	parser.add_argument(
		"--switch-toggle-color",
		default="red",
		choices=["red", "green", "blue"],
		help="Color channel used to toggle the switch status.",
	)
	parser.add_argument(
		"--pass-trigger-color",
		default="red",
		choices=["red", "green", "blue"],
		help="Trigger color that confirms one keep-path marble passed the switch.",
	)
	parser.add_argument(
		"--binary-entry-trigger-color",
		"--lever-trigger-color",
		dest="binary_entry_trigger_color",
		default="green",
		choices=["red", "green", "blue"],
		help="Trigger color that confirms one marble entered binary selection sequence.",
	)
	parser.add_argument(
		"--blue-trigger-color",
		default="blue",
		choices=["red", "green", "blue"],
		help="Trigger color used for measuring green->blue timing interval.",
	)
	parser.add_argument(
		"--pass-timeout",
		type=float,
		default=2.0,
		help="Seconds to wait per keep shot for trigger confirmation before auto-toggle.",
	)
	parser.add_argument(
		"--cannon-gap",
		type=float,
		default=0.25,
		help="Time gap in seconds between cannon shots.",
	)
	parser.add_argument(
		"--switch-settle",
		type=float,
		default=0.2,
		help="Wait time in seconds after a switch toggle before next shot.",
	)
	parser.add_argument(
		"--binary-cycle-gap",
		"--lever-gap",
		dest="binary_cycle_gap",
		type=float,
		default=0.0,
		help="Additional wait in seconds after each in-flight binary routing cycle.",
	)
	parser.add_argument(
		"--binary-entry-timeout",
		"--lever-release-timeout",
		dest="binary_entry_timeout",
		type=float,
		default=4.0,
		help="Max seconds to wait for a green binary-entry trigger confirmation.",
	)
	parser.add_argument(
		"--selection-travel-time",
		type=float,
		default=2.0,
		help="Max seconds to wait for blue trigger after green (sorting-clear timeout).",
	)
	parser.add_argument(
		"--reset-wait",
		type=float,
		default=0.2,
		help="Wait time in seconds after cycle reset before next loop iteration.",
	)
	parser.add_argument(
		"--resends",
		type=int,
		default=12,
		help="How often each signal package should be resent.",
	)
	parser.add_argument(
		"--resend-gap",
		type=float,
		default=0.0,
		help="Delay in seconds between signal resends.",
	)
	parser.add_argument(
		"--log-level",
		default="INFO",
		choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
		help="Logger verbosity.",
	)
	return parser


def main() -> int:
	"""CLI entrypoint."""
	args = build_parser().parse_args()
	if args.max_marbles < 1:
		raise SystemExit("--max-marbles must be >= 1")
	if args.max_marbles > 15:
		raise SystemExit("--max-marbles must be <= 15")
	if args.release_inactivity <= 0:
		raise SystemExit("--release-inactivity must be > 0")
	if args.cannon_gap < 0:
		raise SystemExit("--cannon-gap must be >= 0")
	if args.switch_settle < 0:
		raise SystemExit("--switch-settle must be >= 0")
	if args.binary_cycle_gap < 0:
		raise SystemExit("--binary-cycle-gap must be >= 0")
	if args.pass_timeout <= 0:
		raise SystemExit("--pass-timeout must be > 0")
	if args.binary_entry_timeout <= 0:
		raise SystemExit("--binary-entry-timeout must be > 0")
	if args.selection_travel_time < 0:
		raise SystemExit("--selection-travel-time must be >= 0")
	if args.reset_wait < 0:
		raise SystemExit("--reset-wait must be >= 0")

	try:
		return asyncio.run(run(args))
	except KeyboardInterrupt:
		gb.log_print("Interrupted by user.", level="WARNING")
		return 130


if __name__ == "__main__":
	raise SystemExit(main())
