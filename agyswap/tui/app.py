from __future__ import annotations

from textual.app import App
from textual.reactive import reactive
from textual.worker import WorkerState

from agyswap.tui.dashboard import DashboardScreen
from agyswap.tui.theme import AGY_SWAP_DARK
from agyswap.tui.watch import WatchScreen


class AgySwapApp(App):
    TITLE = "agy-swap"
    CSS_PATH = "agy.tcss"
    ENABLE_COMMAND_PALETTE = False

    POLL_INTERVAL_S = 3.0

    snapshot: reactive[list[dict] | None] = reactive(None)
    busy: reactive[bool] = reactive(False)

    def __init__(self, sw, start_watch: bool = False):
        super().__init__()
        self._sw = sw
        self._start = "watch" if start_watch else "dashboard"
        self._refreshing = False
        self._action_busy = False
        self.threshold_pct = None

    def on_mount(self) -> None:
        self.register_theme(AGY_SWAP_DARK)
        self.theme = "agy-swap-dark"
        self.push_screen(DashboardScreen(self._sw))
        if self._start == "watch":
            self.push_screen(WatchScreen(self._sw))
        self.set_interval(self.POLL_INTERVAL_S, self._tick)
        self._tick()

    # -- refresh loop -----------------------------------------------------

    def _tick(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self.run_worker(
            self._refresh_blocking,
            thread=True,
            group="refresh",
            exit_on_error=False,
            name="snapshot-refresh",
        )

    def _refresh_blocking(self) -> None:
        """Runs off the UI thread: reading credentials can touch the keyring CLI.

        Quota comes from the status-line cache, so this poll is purely local --
        no network, nothing to rate-limit.
        """
        try:
            snapshot = self._sw.list_accounts()
        except Exception:
            self.call_from_thread(self._refresh_done)
            return
        self.call_from_thread(self._apply_snapshot, snapshot)

    def _apply_snapshot(self, snapshot: list[dict]) -> None:
        self._refreshing = False
        self.snapshot = snapshot

    def _refresh_done(self) -> None:
        self._refreshing = False

    def on_worker_state_changed(self, event) -> None:
        if event.state is WorkerState.ERROR and event.worker.group in ("refresh", "action"):
            if event.worker.group == "refresh":
                self._refreshing = False
            self.notify(
                f"Background task failed: {event.worker.error}",
                severity="warning",
                timeout=6,
            )

    def request_refresh(self) -> None:
        self._tick()

    # -- actions (all off the UI thread) ----------------------------------

    def _run_action(self, action) -> None:
        if self._action_busy:
            self.notify("Still finishing the previous action…", timeout=2)
            return
        self._action_busy = True

        def work() -> None:
            try:
                message = action()
            except Exception as exc:
                self.call_from_thread(self._finish_action, str(exc), "error")
            else:
                self.call_from_thread(self._finish_action, message, "information")

        self.run_worker(work, thread=True, group="action", exit_on_error=False)

    def _finish_action(self, message: str | None, severity: str) -> None:
        self._action_busy = False
        if message:
            self.notify(message, severity=severity, timeout=4)
        self.request_refresh()

    def do_switch(self, number) -> None:
        def action() -> str:
            result = self._sw.switch(str(number))
            return f"Activated {result['active']} (new agy sessions only)"

        self._run_action(action)

    def do_toggle_disabled(self, number) -> None:
        snapshot = self.snapshot or []
        target_state = None
        for row in snapshot:
            if str(row["number"]) == str(number):
                target_state = not row.get("disabled", False)
                break
        if target_state is None:
            return

        def action() -> str:
            account = self._sw.set_disabled(str(number), target_state)
            return f"{'Disabled' if target_state else 'Enabled'} #{account.number}"

        self._run_action(action)

    def do_remove(self, number) -> None:
        def action() -> str:
            account = self._sw.remove_account(str(number))
            return f"Removed #{account.number} {account.email}"

        self._run_action(action)

    def do_add_current(self) -> None:
        def action() -> str:
            account = self._sw.add_current()
            return f"Added #{account.number} {account.email}"

        self._run_action(action)

    def action_refresh_full(self) -> None:
        self.request_refresh()
        self.notify("Refreshing…", timeout=2)

    def action_open_watch(self) -> None:
        if isinstance(self.screen, WatchScreen):
            return
        self.push_screen(WatchScreen(self._sw))


def run_tui(sw, start_watch: bool = False) -> None:
    AgySwapApp(sw, start_watch=start_watch).run()
