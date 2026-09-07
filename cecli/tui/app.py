"""Main Textual application for cecli TUI."""

import concurrent.futures
import json
import platform
import queue
import time
from functools import lru_cache
from pathlib import Path

import textual.strip
import xxhash
from rich.color import ColorSystem
from rich.style import Style
from textual import events
from textual.app import App, ComposeResult
from textual.theme import Theme

from cecli import __version__
from cecli.decoding import safe_open
from cecli.editor import pipe_editor
from cecli.helpers import queues
from cecli.helpers.agents.service import AgentService
from cecli.helpers.coroutines import is_active
from cecli.helpers.file_system import FileSystemService
from cecli.io import CommandCompletionException

from .widgets import (
    CompletionBar,
    FileList,
    InputArea,
    InputContainer,
    KeyHints,
    MainFooter,
    OutputContainer,
    StatusBar,
)
from .widgets.output import CostUpdate

IS_WINDOWS = False
if platform.system() == "Windows":
    IS_WINDOWS = True


class TUI(App):
    """Main Textual application for cecli TUI."""

    CSS_PATH = "styles.tcss"

    # Enable Textual's global container text selection (Textual 8.2.0+)
    # This allows users to click-and-drag to select text across mounted widget
    # boundaries, which is essential since we use individual widget blocks
    # instead of a monolithic RichLog.
    ENABLE_SELECT_AUTO_SCROLL = True
    SELECT_AUTO_SCROLL_SPEED = 20

    BINDINGS = [
        # Binding("ctrl+c", "quit", "Quit", show=True),
        # Binding("ctrl+l", "clear_output", "Clear", show=True),
        # Binding("escape", "interrupt", "Interrupt", show=True),
    ]

    def __init__(self, coder_worker, output_queue, input_queue, args):
        """Initialize the cecli TUI app."""
        super().__init__()
        self.worker = coder_worker
        self.output_queue = output_queue
        self.input_queue = input_queue
        self.args = args  # Store args for _get_config

        # Cache for code symbols (functions, classes, variables)
        self._symbols_cache = None
        self._symbols_files_hash = None
        self._mouse_hold_timer = None
        self._git_branch_fp = None
        self._currently_generating = False

        # Sub-agent tracking
        self._sub_agent_containers = {}  # uuid -> OutputContainer
        self._primary_coder_uuid = self.worker.coder.uuid

        # Confirmation lock and pending queue — ensures one confirmation at a time
        self._confirmation_lock = False
        self._confirmation_coder_uuid = None
        self._confirmations_pending: list[tuple[dict, str | None]] = []

        self.tui_config = self._get_config()

        # Register and set cecli theme using config colors
        colors = self.tui_config.get("colors", {})
        other = self.tui_config.get("other", {})
        BASE_THEME = Theme(
            name="cecli",
            primary=colors.get("primary", "#00ff5f"),
            secondary=colors.get("secondary", "#888888"),
            accent=colors.get("accent", "#00ff87"),  # Cecli green
            foreground=colors.get("foreground", "#ffffff"),
            background=colors.get("background", "#1e1e1e"),
            success=colors.get("success", "#00aa00"),
            warning=colors.get("warning", "#ffd700"),
            error=colors.get("error", "#ff3333"),
            surface=colors.get("surface", "transparent"),  # Slightly lighter than background
            panel=colors.get("panel", "transparent"),
            dark=other.get("dark", True),
            variables={
                "input-cursor-foreground": colors.get("input-cursor-foreground", "#00ff87"),
                "input-cursor-text-style": other.get("input-cursor-text-style", "underline"),
                "screen-selection-background": colors.get("background", "#1e1e1e"),
                "screen-selection-foreground": colors.get("success", "#00aa00"),
            },
        )

        if other.get("use_terminal_background", True):
            patch_textual_strip_render_with_cache()

        self.bind(
            self._encode_keys(self.get_keys_for("newline")),
            "noop",
            description="New Line",
            show=True,
        )
        self.bind(
            self._encode_keys(self.get_keys_for("submit")), "noop", description="Submit", show=True
        )
        self.bind(
            self._encode_keys(self.get_keys_for("cycle_forward")),
            "noop",
            description="Cycle Forward",
            show=True,
        )
        self.bind(
            self._encode_keys(self.get_keys_for("cycle_backward")),
            "noop",
            description="Cycle Backward",
            show=True,
        )
        self.bind(
            self._encode_keys(self.get_keys_for("prev_agent")),
            "switch_prev_agent",
            description="Previous Agent",
            show=True,
        )
        self.bind(
            self._encode_keys(self.get_keys_for("next_agent")),
            "switch_next_agent",
            description="Next Agent",
            show=True,
        )
        self.bind(
            self._encode_keys(self.get_keys_for("main_agent")),
            "switch_to_primary",
            description="Main Agent",
            show=True,
        )
        self.bind(
            self._encode_keys(self.get_keys_for("cancel")),
            "interrupt",
            description="Cancel",
            show=True,
        )
        self.bind(
            self._encode_keys(self.get_keys_for("editor")),
            "open_editor",
            description="Editor",
            show=True,
        )
        self.bind(
            self._encode_keys(self.get_keys_for("history")),
            "history_search",
            description="History Search",
            show=True,
        )
        self.bind(
            self._encode_keys(self.get_keys_for("focus")),
            "focus_input",
            description="Focus Input",
            show=True,
        )
        self.bind(
            self._encode_keys(self.get_keys_for("stop")),
            "interrupt",
            description="Interrupt",
            show=True,
        )
        self.bind(
            self._encode_keys(self.get_keys_for("clear")),
            "clear_output",
            description="Clear",
            show=True,
        )
        self.bind(
            self._encode_keys(self.get_keys_for("quit")), "quit", description="Quit", show=True
        )

        self.register_theme(BASE_THEME)
        self.theme = "cecli"

    @property
    def render_markdown(self):
        """Return whether markdown rendering is enabled."""
        return self.tui_config.get("other", {}).get("render_markdown", True)

    def _get_config(self):
        """
        Parse and return TUI configuration from args.tui_config.

        Returns:
            dict: TUI configuration with defaults for missing values
        """
        config = {}

        # Check if tui_config is provided via args
        if (
            hasattr(self, "args")
            and self.args
            and hasattr(self.args, "tui_config")
            and self.args.tui_config
        ):
            try:
                config = json.loads(self.args.tui_config)
            except (json.JSONDecodeError, TypeError) as e:
                # Can't use self.io here since it doesn't exist yet
                # The error will be handled elsewhere if needed
                print(f"Warning: Failed to parse tui-config JSON: {e}")
                # Continue with empty config, will apply defaults below

        # Ensure config has a colors entry with nested structure matching BASE_THEME
        if "banner" not in config:
            config["banner"] = True

        if "colors" not in config:
            config["colors"] = {}

        if "other" not in config:
            config["other"] = {}

        if "key_bindings" not in config:
            config["key_bindings"] = {}

        # Ensure colors dict has all expected keys with default values
        default_colors = {
            "primary": "#00ff5f",
            "secondary": "#888888",
            "accent": "#00ff87",
            "foreground": "#ffffff",
            "background": "#1e1e1e",
            "success": "#00aa00",
            "warning": "#ffd700",
            "error": "#ff3333",
            "surface": "transparent",
            "panel": "transparent",
            "dark": True,
            "variables": {
                "input-cursor-foreground": "#00ff87",
                "input-cursor-text-style": "underline",
                "screen-selection-background": "#1e1e1e",
                "screen-selection-foreground": "#00ff87",
            },
        }

        default_key_bindings = {
            "newline": "shift+enter",
            "submit": "enter",
            "stop": "escape",
            "cycle_forward": "tab",
            "cycle_backward": "shift+tab",
            "input_start": "ctrl+home",
            "input_end": "ctrl+end",
            "output_up": "shift+pageup",
            "output_down": "shift+pagedown",
            "next_agent": "alt+ctrl+right",
            "prev_agent": "alt+ctrl+left",
            "main_agent": "alt+ctrl+up",
            "editor": "ctrl+o",
            "history": "ctrl+r",
            "focus": "ctrl+f",
            "cancel": "ctrl+c",
            "clear": "ctrl+l",
            "quit": "ctrl+q",
        }

        # Default settings for the "other" section
        default_other = {
            "render_markdown": False,
            "use_terminal_background": False,
        }

        # Merge default other settings with user-provided settings
        for key, default_value in default_other.items():
            if key not in config["other"]:
                config["other"][key] = default_value

        # Merge default colors with user-provided colors
        for key, default_value in default_colors.items():
            if key not in config["colors"]:
                config["colors"][key] = default_value
            elif key == "variables" and isinstance(default_value, dict):
                # Handle nested variables dict
                if "variables" not in config["colors"]:
                    config["colors"]["variables"] = {}
                for var_key, var_default in default_value.items():
                    if var_key not in config["colors"]["variables"]:
                        config["colors"]["variables"][var_key] = var_default

        for key, default_value in default_key_bindings.items():
            if key not in config["key_bindings"]:
                config["key_bindings"][key] = self._encode_keys(default_value)

        for key, value in config["key_bindings"].items():
            config["key_bindings"][key] = self._encode_keys(value)

        return config

    def compose(self) -> ComposeResult:
        """Create child widgets."""
        coder = self.worker.coder
        model_name = coder.main_model.name if coder.main_model else "Unknown"
        coder_mode = getattr(coder, "edit_format", "code") or "code"

        # Get project name (just the folder name, not full path)
        home = str(Path.home())
        cwd = str(Path.cwd())
        if cwd.startswith(home):
            project_name = cwd.replace(home, "~", 1)
        else:
            project_name = cwd

        if len(project_name) >= 64:
            project_name = project_name.split("/")[-1]

        if coder.repo:
            root_path = str(coder.repo.root)
            if root_path.startswith(home):
                root_path = root_path.replace(home, "~", 1)

            if len(root_path) <= 64:
                project_name = root_path
            else:
                project_name = root_path.split("/")[-1]
        # Get history file path from coder's io
        history_file = getattr(coder.io, "input_history_file", None)

        # Simple vertical layout - no header, footer has all info
        # Git info loaded in on_mount to avoid blocking startup
        yield OutputContainer(id="output")
        yield StatusBar(id="status-bar")
        yield InputContainer(
            InputArea(history_file=history_file, id="input"),
            FileList(id="file-list", classes="empty"),
            id="input-container",
            coder_mode=coder_mode,
        )
        yield KeyHints(id="key-hints")
        yield MainFooter(
            model_name=model_name,
            project_name=project_name,
            git_branch="",  # Loaded async in on_mount
            coder_mode=coder_mode,
            id="footer",
        )

    BANNER_COLORS = [
        "spring_green2",
        "spring_green1",
        "medium_spring_green",
        "cyan2",
        "cyan1",
        "bright_white",
        "medium_spring_green",
    ]

    E = f"[bold {BANNER_COLORS[6]}]▓▓▓[/bold {BANNER_COLORS[6]}]"
    # ASCII banner for startup
    BANNER = f"""
[bold {BANNER_COLORS[0]}]                       ▒▒╗▒▒╗[/bold {BANNER_COLORS[0]}]
[bold {BANNER_COLORS[1]}]   ▒▒▒▒▒╗ ▒▒▒▒▒╗ ▒▒▒▒▒╗▒▒║╚═╝[/bold {BANNER_COLORS[1]}]
[bold {BANNER_COLORS[2]}]  ▒▒╔═══╝▒▒{E}▒║▒▒╔═══╝▒▒║▒▒╗[/bold {BANNER_COLORS[2]}]
[bold {BANNER_COLORS[3]}]  ▒▒║    ▒▒╔═══╝▒▒║    ▒▒║▒▒║[/bold {BANNER_COLORS[3]}]
[bold {BANNER_COLORS[4]}]  ╚▒▒▒▒▒╗╚▒▒▒▒▒╗╚▒▒▒▒▒╗▒▒║▒▒║[/bold {BANNER_COLORS[4]}]
[bold {BANNER_COLORS[5]}]   ╚════╝ ╚════╝ ╚════╝╚═╝╚═╝ v{__version__}[/bold {BANNER_COLORS[5]}]

"""

    def on_mount(self):
        """Called when app starts."""
        # Show startup banner
        output_container = self.query_one("#output", OutputContainer)
        if self.tui_config["banner"]:
            output_container.add_output(self.BANNER, dim=False)
        else:
            output_container.add_output(
                f"[bold {self.BANNER_COLORS[0]}] [/bold {self.BANNER_COLORS[0]}]", dim=False
            )

        self.begin_capture_print(output_container, stdout=True, stderr=True)

        self.set_interval(0.05, self.check_output_queue)

        # Cheap poll for git branch changes (HEAD) rather than a full git
        # resolve on a fixed cadence; only refresh the footer when it changes.
        self.set_interval(5, self._check_git_branch)

        self.worker.start()
        self.query_one("#input").focus()

        # Initialize key hints
        self.update_key_hints()

        # Load git info in background to avoid blocking startup
        self.call_later(self._load_git_info)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """Handle mouse down events to start the selection hint timer."""
        if self._mouse_hold_timer:
            self._mouse_hold_timer.stop()
        self._mouse_hold_timer = self.set_timer(0.25, self._show_select_hint)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        """Handle mouse up events to clear the selection hint timer."""
        if self._mouse_hold_timer:
            self._mouse_hold_timer.stop()
            self._mouse_hold_timer = None
        self.update_key_hints(generating=self._currently_generating)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        """Handle mouse move events to prevent strange characters on Windows."""
        if IS_WINDOWS:
            event.stop()

    def _show_select_hint(self) -> None:
        """Show the shift+drag to select hint."""
        try:
            hints = self.query_one(KeyHints)
            hints.update_right("shift+drag to select")
        except Exception:
            pass

    def update_key_hints(self, generating=False):
        """Update the key hints below the input area."""
        if self._mouse_hold_timer:
            self._mouse_hold_timer.stop()
            self._mouse_hold_timer = None
        try:
            hints = self.query_one(KeyHints)
            if generating:
                self._currently_generating = True
                stop = self.app.get_keys_for("stop")
                hints.update_right(f"{stop} to cancel")
            else:
                self._currently_generating = False
                submit = self.app.get_keys_for("submit")
                hints.update_right(f"{submit} to submit")
        except Exception:
            pass

    def update_key_hints_left(self, text: str):
        """Update the left sub-panel message."""
        try:
            hints = self.query_one(KeyHints)
            hints.update_left(text)
        except Exception:
            pass

    def update_cost(self, cost_text: str):
        """Update the cost display in the input container's border subtitle."""
        try:
            container = self.query_one(InputContainer)
            container.update_cost(cost_text)
        except Exception:
            pass

    def _update_key_hints_for_commands(self, text: str, is_completion: bool = False):
        """
        Update key hints left area with command description.

        Handles both regular input text and completion suggestions.

        Args:
            text: The text to analyze (input text or completion suggestion)
            is_completion: Whether this is a completion suggestion (default: False)
        """
        # Check if text starts with slash
        if text.startswith("/"):
            # Extract command name
            # For completions, we just need to remove the leading slash
            # For regular input, we need to extract the first word after slash
            if is_completion:
                # Completion suggestion like "/help" - just remove leading slash
                cmd_name = text[1:].strip()
            else:
                # Regular input like "/help arg1 arg2" - extract first word
                parts = text[1:].strip().split()
                cmd_name = parts[0] if parts else ""

            # Get command description if we have a command name
            if cmd_name:
                try:
                    from cecli.commands.utils.registry import CommandRegistry

                    description = CommandRegistry.get_command_description(cmd_name)
                    if description:
                        self.update_key_hints_left(f"{description}")
                        return
                except Exception:
                    pass

        # If not a valid slash command, show default text
        self.update_key_hints_left(KeyHints.DEFAULT_LEFT_TEXT)

    def _load_git_info(self):
        """Load git branch (deferred to avoid blocking startup)."""
        footer = self.query_one(MainFooter)
        if self.worker.coder.repo:
            try:
                branch = self.worker.coder.repo.repo.active_branch.name or "main"
                footer.update_git(branch)
            except Exception:
                if self.worker.coder.repo:
                    footer.update_git("main")
                else:
                    footer.update_git("No Repo")

    def check_output_queue(self):
        """Process messages from coder worker."""
        try:
            while True:
                msg = self.output_queue.get_nowait()
                self.handle_output_message(msg)
        except queue.Empty:
            pass

    def handle_output_message(self, msg):
        msg_type = msg["type"]

        # Resolve agent_name from coder_uuid for agent-specific status messages
        agent_name = self._resolve_agent_name(msg.get("coder_uuid"))
        if msg_type == "output":
            container = self._get_output_container(msg)
            container.add_output(msg["text"], msg.get("task_id"))
        elif msg_type == "tool_call":
            # Render tool call with styled panel
            container = self._get_output_container(msg)
            container.add_tool_call(msg["lines"])
        elif msg_type == "tool_result":
            # Render tool result with connector prefix
            container = self._get_output_container(msg)
            container.add_tool_result(msg["text"])
        elif msg_type == "start_response":
            # Start a new LLM response with streaming
            container = self._get_output_container(msg)
            self.run_worker(self._start_response(container))
        elif msg_type == "stream_chunk":
            # Stream a chunk of LLM response
            container = self._get_output_container(msg)
            self.run_worker(self._stream_chunk(container, msg["text"]))
        elif msg_type == "end_response":
            # End the current LLM response
            container = self._get_output_container(msg)
            self.run_worker(self._end_response(container))
        elif msg_type == "start_task":
            container = self._get_output_container(msg)
            container.start_task(msg["task_id"], msg["title"], msg.get("task_type"))
        elif msg_type == "confirmation":
            self.show_confirmation(msg, agent_name=agent_name)
        elif msg_type == "spinner":
            self.update_spinner(msg, agent_name=agent_name)
        elif msg_type == "ready_for_input":
            self.enable_input(msg)
            footer = self.query_one(MainFooter)
            footer.stop_spinner()
        elif msg_type == "error":
            self.show_error(msg["message"], agent_name=agent_name)
        elif msg_type == "cost_update":
            footer = self.query_one(MainFooter)
            footer.update_cost(msg.get("cost", 0))
        elif msg_type == "exit":
            # Graceful exit requested - let Textual clean up terminal properly
            self.action_quit()
        elif msg_type == "mode_change":
            # Update footer with new chat mode
            container = footer = self.query_one(InputContainer)
            container.update_mode(msg.get("mode", "code"))

            footer = self.query_one(MainFooter)
            footer.update_mode(msg.get("mode", "code"))
        elif msg_type == "switch_agent":
            target_uuid = msg["uuid"]
            # Ensure the target container exists before switching
            primary_uuid = str(self.worker.coder.uuid)
            if target_uuid != primary_uuid and target_uuid not in self._sub_agent_containers:
                self.show_error("Agent container not found. Cannot switch.")
            else:
                self._switch_to_container(target_uuid)

    def _resolve_agent_name(self, coder_uuid: str | None) -> str | None:
        """Resolve an agent display name from a coder_uuid.

        Returns the sub-agent's name if the coder_uuid belongs to a known
        sub-agent. For the primary agent, returns "primary" if sub-agents
        exist, otherwise None.

        If multiple sub-agents share the same name, disambiguates by
        appending the first 3 characters of the UUID in parentheses.
        """
        if not coder_uuid:
            return None
        try:
            if not self.worker or not self.worker.coder:
                return None  # Cannot resolve without a coder
            from cecli.helpers.agents.service import AgentService

            agent_service = AgentService.get_instance(self.worker.coder)
            if not agent_service:
                return None
            primary_uuid = str(agent_service.coder.uuid)
            if coder_uuid == primary_uuid:
                if agent_service.sub_agents:
                    return "primary"
                return None  # Primary agent gets no prefix
            if not agent_service.sub_agents:
                return None
            for info in agent_service.sub_agents.values():
                if not info or not info.coder:
                    continue
                if str(info.coder.uuid) == coder_uuid:
                    # Check for duplicate names among sub-agents
                    name_count = sum(
                        1
                        for i in agent_service.sub_agents.values()
                        if i and hasattr(i, "name") and i.name == info.name
                    )
                    if name_count > 1:
                        # Disambiguate with first 3 UUID characters
                        short_uuid = str(info.coder.uuid)[:3]
                        return f"{info.name} ({short_uuid})"
                    return info.name
        except (AttributeError, ImportError, KeyError):
            # Agent service not available or coder not yet initialized
            pass
        return None

    def add_output(self, text, task_id=None):
        """Add output to the output container."""
        output_container = self.query_one("#output", OutputContainer)
        output_container.add_output(text, task_id)

    async def _start_response(self, container=None):
        """Start a new LLM response (async helper)."""
        if container is None:
            container = self.query_one("#output", OutputContainer)
        await container.start_response()

    async def _stream_chunk(self, container, text: str):
        """Stream a chunk to the current response (async helper).

        Args:
            container: The OutputContainer to stream the chunk to.
            text: Text chunk to stream.
        """
        if container is None:
            container = self.query_one("#output", OutputContainer)
        await container.stream_chunk(text)

    async def _end_response(self, container=None):
        """End the current LLM response (async helper)."""
        if container is None:
            container = self.query_one("#output", OutputContainer)
        await container.end_response()

    def add_user_message(self, text: str):
        """Add a user message to output, routing to the active container."""
        container = self._get_visible_container()
        container.add_user_message(text)

    def start_task(self, task_id, title, task_type="general"):
        """Start a new task section."""
        output_container = self.query_one("#output", OutputContainer)
        output_container.start_task(task_id, title, task_type)

    def show_confirmation(self, msg, agent_name: str | None = None):
        """Show inline confirmation bar."""

        # Safety: clear stale lock if no confirmation bar is active
        if self._confirmation_lock:
            status_bar = self.query_one("#status-bar", StatusBar)
            if status_bar.mode != "confirm":
                self._confirmation_lock = False

        # Check confirmation lock: only one confirmation at a time
        if self._confirmation_lock:
            self._confirmations_pending.append((msg, agent_name))
            return
        self._confirmation_lock = True

        # Disable input while confirm bar is active
        input_area = self.query_one("#input", InputArea)
        input_area.disabled = True

        # Switch to the agent that requested this confirmation
        coder_uuid = msg.get("coder_uuid")
        self._confirmation_coder_uuid = coder_uuid
        if coder_uuid:
            self._switch_to_container(coder_uuid, suppress_input_enable=True)

        # Show confirmation in status bar with all options
        status_bar = self.query_one("#status-bar", StatusBar)
        options = msg.get("options", {})

        # Determine which options to show based on the parameters
        show_all = options.get("group") is not None or options.get("group_response") is not None
        allow_tweak = options.get("allow_tweak", False)
        allow_never = options.get("allow_never", False)

        status_bar.show_confirm(
            msg["question"],
            show_all=show_all,
            allow_tweak=allow_tweak,
            allow_never=allow_never,
            default=options.get("default", "y"),
            explicit_yes_required=options.get("explicit_yes_required", False),
            agent_name=agent_name,
        )

    def enable_input(self, msg, coder=None):
        """Enable input and update autocomplete data for the active coder.

        Always resolves the active (foreground) coder and displays its files,
        commands, and chat files — never relies on *msg* data for those.
        The *msg* parameter is kept for backward compatibility with callers
        that pass it, but its ``files`` / ``commands`` / ``chat_files`` keys
        are ignored in favor of the active coder's state.

        If *coder* is passed explicitly it is used directly; otherwise the
        foreground coder is resolved via ``AgentService``.
        """
        self.update_key_hints(generating=False)
        input_area = self.query_one("#input", InputArea)
        input_area.disabled = False  # Ensure input is enabled

        if coder is None:
            # Always resolve the active/foreground coder
            from cecli.helpers.agents.service import AgentService

            coder = AgentService.get_instance(self.worker.coder).foreground_coder

        files = list(coder.get_addable_relative_files())
        commands = coder.commands.get_commands() if getattr(coder, "commands", None) else []
        input_area.update_autocomplete_data(files, commands)

        # Update file list
        file_list = self.query_one("#file-list", FileList)
        file_list.update_files()

        input_area.focus()

    def copy_to_clipboard(self, text: str) -> None:
        import pyperclip

        try:
            pyperclip.copy(text)
            self._clipboard = text
        except Exception:  # pragma: no cover - system clipboard errors
            self.worker.coder.io.tool_error("Failed to copy to system clipboard.")
            self.worker.coder.io.tool_output(
                "You may need to install xclip, xsel, or wl-clipboard on Linux, or pbcopy on macOS."
            )
            super().copy_to_clipboard(text)

    def update_spinner(self, msg, agent_name: str | None = None):
        """Update spinner in footer."""
        footer = self.query_one(MainFooter)
        action = msg.get("action", "start")

        if action == "start":
            footer.start_spinner(msg.get("text", ""), agent_name=agent_name or "")
        elif action == "update":
            footer.spinner_text = msg.get("text", "")
        elif action == "update_suffix":
            footer.spinner_suffix = msg.get("text", "")
        elif action == "stop":
            footer.stop_spinner()

    def show_error(self, message, agent_name: str | None = None):
        """Show an error message in the status bar."""
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.show_notification(message, severity="error", timeout=5, agent_name=agent_name)

    def on_resize(self) -> None:
        file_list = self.query_one("#file-list", FileList)
        file_list.update_files()

    def on_input_area_text_changed(self, message: InputArea.TextChanged):
        """Handle text changes in input area."""
        self._update_key_hints_for_commands(message.text, is_completion=False)

    def on_input_area_submit(self, message: InputArea.Submit):
        """Handle input submission."""
        from cecli.helpers.agents.service import AgentService

        user_input = message.value

        if not user_input.strip():
            return

        # Intercept /editor and /edit commands to handle with TUI suspension
        stripped = user_input.strip()
        if (
            stripped in ("/editor", "/edit")
            or stripped.startswith("/editor ")
            or stripped.startswith("/edit ")
        ):
            # Extract initial content if provided (e.g., "/editor some text")
            initial_content = ""
            if stripped.startswith("/editor "):
                initial_content = stripped[8:]
            elif stripped.startswith("/edit "):
                initial_content = stripped[6:]

            # Clear input and open editor with suspend
            input_area = self.query_one("#input", InputArea)
            input_area.value = ""
            self._open_editor_suspended(initial_content)
            return

        # Intercept /switch-agent command to handle immediately without LLM processing
        if stripped.startswith("/switch-agent"):
            parts = stripped.split(maxsplit=1)
            agent_name = parts[1].strip() if len(parts) > 1 else ""

            input_area = self.query_one("#input", InputArea)
            input_area.value = ""

            if not agent_name:
                self.show_error("Usage: /switch-agent <agent-name>")
                return

            # Resolve agent name to UUID
            agent_service = AgentService.get_instance(self.worker.coder)
            primary_uuid = str(self.worker.coder.uuid)

            target_uuid = None
            if agent_name == "primary":
                target_uuid = primary_uuid
            else:
                # Try parsing "name (uuid)" format
                if agent_name.endswith(")") and " (" in agent_name:
                    try:
                        # Extract uuid prefix from "name (prefix)"
                        uuid_prefix = agent_name.rsplit(" (", 1)[1][:-1]
                        for uuid, info in agent_service.sub_agents.items():
                            if uuid.startswith(uuid_prefix):
                                target_uuid = uuid
                                break
                    except IndexError:
                        pass  # Not the format we expected

                # If not found via "name (uuid)", try matching by name directly
                if target_uuid is None:
                    for uuid, info in agent_service.sub_agents.items():
                        if info.name == agent_name:
                            target_uuid = uuid
                            break

                # If still not found, try matching by uuid prefix directly
                if target_uuid is None:
                    for uuid, info in agent_service.sub_agents.items():
                        if uuid.startswith(agent_name):
                            target_uuid = uuid
                            break

            if target_uuid is None:
                self.show_error(f"Agent '{agent_name}' not found.")
                return

            if target_uuid != primary_uuid and target_uuid not in self._sub_agent_containers:
                self.show_error(f"Agent container for '{agent_name}' not found.")
                return

            self._switch_to_container(target_uuid)
            return

        # Intercept /spawn-agent command to handle immediately without LLM
        # processing so a new agent can be spawned even while the primary
        # coder is generating.
        if stripped == "/spawn-agent" or stripped.startswith("/spawn-agent "):
            self._handle_spawn_agent_command(user_input, stripped)
            return

        # Intercept /workspace <ws:name> to open an already-registered workspace
        # sub-agent immediately, mirroring /spawn-agent.
        if stripped == "/workspace" or stripped.startswith("/workspace "):
            parts = stripped.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip().startswith("ws:"):
                self._handle_workspace_command(user_input, stripped)
                return

        # Intercept queue management commands (/queue, /list-queue, /remove-queue,
        # /insert-queue) to dispatch immediately without a full generation cycle -
        # they only modify the active coder's prompt_queue.
        if (
            stripped == "/queue"
            or stripped.startswith("/queue ")
            or stripped == "/list-queue"
            or stripped == "/remove-queue"
            or stripped.startswith("/remove-queue ")
            or stripped.startswith("/insert-queue ")
        ):
            self._handle_queue_command(stripped)
            return

        # Save to history before clearing
        input_area = self.query_one("#input", InputArea)
        input_area.save_to_history(user_input)

        input_area.value = ""

        # Show user's message in output
        self.add_user_message(user_input)

        # Update footer to show processing
        footer = self.query_one(MainFooter)

        coder = self.worker.coder
        # Determine which coder is in the foreground for input routing
        foreground_coder = AgentService.get_instance(coder).foreground_coder
        coder_uuid = (
            str(foreground_coder.uuid)
            if foreground_coder and hasattr(foreground_coder, "uuid")
            else None
        )
        agent_name = self._resolve_agent_name(coder_uuid)

        footer.start_spinner("Processing...", agent_name=agent_name or "")

        if coder:
            coder.io.start_spinner("Processing...", coder_uuid=coder_uuid)

        if coder and is_active(getattr(coder.io, "output_task", None)):
            from cecli.helpers.conversation import ConversationService, MessageTag

            # Check if the foreground coder is the primary coder
            is_primary = foreground_coder is coder
            if not is_primary:
                # Could be a sub-agent
                parent_uuid = getattr(foreground_coder, "parent_uuid", None)
                if parent_uuid:
                    # It's a sub-agent — check if it's idle
                    agent_service = AgentService.get_instance(coder)
                    for info in agent_service.sub_agents.values():
                        if info.coder.uuid == foreground_coder.uuid:
                            if not is_active(info.generate_task):
                                # Idle sub-agent: start a new generate task via worker loop
                                if self.worker.loop is not None:
                                    self.worker.loop.call_soon_threadsafe(
                                        lambda: agent_service.start_generate_task(info, user_input)
                                    )
                                return
                            break

            # Default (primary coder, actively generating sub-agent,
            # or sub-agent not found in tracking): append to conversation
            ConversationService.get_manager(foreground_coder).queue_message(
                message_dict=dict(
                    role="user", content=foreground_coder.wrap_user_input(user_input)
                ),
                tag=MessageTag.CUR,
                hash_key=(
                    "user_message",
                    xxhash.xxh3_128_hexdigest(user_input.encode("utf-8", errors="replace")),
                    str(time.monotonic_ns()),
                ),
            )
        else:
            self.update_key_hints(generating=True)
            coder_uuid = (
                str(foreground_coder.uuid)
                if foreground_coder and hasattr(foreground_coder, "uuid")
                else None
            )
            # Route to per-coder queue when available
            if coder_uuid and coder_uuid in queues._per_coder_queues:
                queues.push_coder_input(coder_uuid, {"text": user_input, "coder_uuid": coder_uuid})
            else:
                self.input_queue.put({"text": user_input, "coder_uuid": coder_uuid})
                queues.wake_input_waiters()

    def _handle_queue_command(self, stripped: str) -> None:
        """Dispatch /queue, /list-queue and /remove-queue immediately.

        These commands only mutate the active coder's ``prompt_queue``, so they
        are handled here without running a full generation cycle.
        """
        from cecli.helpers import command_queue
        from cecli.helpers.agents.service import AgentService

        input_area = self.query_one("#input", InputArea)
        input_area.save_to_history(stripped)
        input_area.value = ""
        self.add_user_message(stripped)

        active_coder = (
            AgentService.get_instance(self.worker.coder).foreground_coder or self.worker.coder
        )
        cmd, _, args = stripped.partition(" ")
        args = args.strip()

        if cmd == "/queue":
            if not args:
                self.show_error("Usage: /queue <prompt text>")
                return

            try:
                item = command_queue.enqueue_prompt(active_coder, args)
            except (ValueError, RuntimeError) as e:
                self.show_error(str(e))
                return

            position = command_queue.get_queue_length(active_coder)
            self._get_visible_container().add_output(
                f"Prompt queued at position {position} (id: {item['id']})"
            )

            # Process the queued prompt immediately when the coder is idle (no
            # active output task). The TUI dispatches queue commands directly
            # here instead of through run_one(), so without this the queued
            # prompt would only run after the user submits yet another prompt.
            worker_loop = getattr(self.worker, "loop", None)
            if (
                worker_loop is not None
                and not is_active(getattr(active_coder.io, "output_task", None))
                and not getattr(active_coder, "_processing_queue", False)
            ):
                worker_loop.call_soon_threadsafe(
                    lambda: worker_loop.create_task(active_coder._drain_prompt_queue(True))
                )

        elif cmd == "/list-queue":
            items = command_queue.list_queue(active_coder)
            if not items:
                self._get_visible_container().add_output("Queue is empty.")
                return

            lines = []
            for index, item in enumerate(items):
                text = item["text"]
                preview = text[:80] + ("..." if len(text) > 80 else "")
                stamp = time.strftime("%H:%M:%S", time.localtime(item["timestamp"]))
                lines.append(f"[{index + 1}] {preview} ({stamp})")

            self._get_visible_container().add_output("\n".join(lines))

        elif cmd == "/insert-queue":
            parts = args.split(maxsplit=1)
            if len(parts) != 2:
                self.show_error("Usage: /insert-queue <index> <prompt text>")
                return

            try:
                index = int(parts[0])
            except ValueError:
                self.show_error(f"Invalid index: '{parts[0]}'. Please provide a number.")
                return

            prompt_text = parts[1].strip()
            try:
                item = command_queue.insert_prompt(active_coder, prompt_text, index)
            except (ValueError, RuntimeError) as e:
                self.show_error(str(e))
                return

            self._get_visible_container().add_output(
                f"Prompt inserted at position {index + 1} (id: {item['id']})"
            )

            # Process the queued prompt immediately when the coder is idle,
            # same as /queue.
            worker_loop = getattr(self.worker, "loop", None)
            if (
                worker_loop is not None
                and not is_active(getattr(active_coder.io, "output_task", None))
                and not getattr(active_coder, "_processing_queue", False)
            ):
                worker_loop.call_soon_threadsafe(
                    lambda: worker_loop.create_task(active_coder._drain_prompt_queue(True))
                )

        elif cmd == "/remove-queue":
            if not args:
                self.show_error("Usage: /remove-queue <index|*>")
                return

            if args == "*":
                removed = command_queue.clear_queue(active_coder)
                self._get_visible_container().add_output(
                    f"Removed all {len(removed)} queued prompt(s)."
                )
                return

            try:
                index = int(args)
            except ValueError:
                self.show_error("Usage: /remove-queue <index|*>")
                return

            removed = command_queue.remove_from_queue(active_coder, index - 1)
            if removed is None:
                self.show_error(f"No queued prompt at index {index}.")
                return

            self._get_visible_container().add_output(
                f"Removed queued prompt {index}: {removed['text'][:80]}"
            )

    def _handle_spawn_agent_command(self, user_input: str, stripped: str) -> None:
        """Dispatch /spawn-agent immediately without a full generation cycle.

        The spawn is scheduled on the worker's event loop so a new agent can
        be started even while the primary coder is generating.
        """
        from cecli.commands.spawn_agent import SpawnAgentCommand

        parts = stripped.split(maxsplit=1)
        spawn_args = parts[1].strip() if len(parts) > 1 else ""

        input_area = self.query_one("#input", InputArea)
        input_area.value = ""

        if not spawn_args:
            self.show_error("Usage: /spawn-agent <name> [<prompt>]")
            return

        # Save to history and echo the command before dispatching
        input_area.save_to_history(user_input)
        self.add_user_message(user_input)

        coder = self.worker.coder
        if self.worker.loop is None:
            self.show_error("Worker loop not available. Cannot spawn sub-agent.")
            return

        async def _run_spawn():
            await SpawnAgentCommand.execute(coder.io, coder, spawn_args)

        self.worker.loop.call_soon_threadsafe(lambda: self.worker.loop.create_task(_run_spawn()))

    def _handle_workspace_command(self, user_input: str, stripped: str) -> None:
        """Dispatch /workspace <ws:name> immediately without a full generation cycle.

        Opening an already-registered workspace sub-agent mirrors /spawn-agent, so
        it is scheduled on the worker's event loop to allow spawning while the
        primary coder is generating.
        """
        from cecli.commands.workspace import WorkspaceCommand

        parts = stripped.split(maxsplit=1)
        spawn_args = parts[1].strip() if len(parts) > 1 else ""

        input_area = self.query_one("#input", InputArea)
        input_area.value = ""

        if not spawn_args:
            self.show_error("Usage: /workspace <ws:name>")
            return

        # Save to history and echo the command before dispatching
        input_area.save_to_history(user_input)
        self.add_user_message(user_input)

        coder = self.worker.coder
        if self.worker.loop is None:
            self.show_error("Worker loop not available. Cannot open workspace sub-agent.")
            return

        async def _run_workspace():
            await WorkspaceCommand.execute(coder.io, coder, spawn_args)

        self.worker.loop.call_soon_threadsafe(
            lambda: self.worker.loop.create_task(_run_workspace())
        )

    def set_input_value(self, text) -> None:
        """Find the input widget and set focus to it."""
        input_area = self.query_one("#input", InputArea)
        input_area.value = text
        input_area.cursor_position = len(input_area.value)

    def action_focus_input(self) -> None:
        """Find the input widget and set focus to it."""
        input_area = self.query_one("#input", InputArea)
        input_area.focus()

    def action_clear_output(self):
        """Clear all output."""
        output_container = self._get_visible_container()
        output_container.clear_output()
        if self.tui_config["banner"]:
            output_container.add_output(self.BANNER, dim=False)
        else:
            output_container.add_output(
                f"[bold {self.BANNER_COLORS[0]}] [/bold {self.BANNER_COLORS[0]}]", dim=False
            )

        self._get_visible_coder().show_announcements()

    def action_output_up(self):
        """Scroll the output area up one page."""
        output_container = self._get_visible_container()
        output_container.action_page_up()

    def action_output_down(self):
        """Scroll the output area down one page."""
        output_container = self._get_visible_container()
        output_container.action_page_down()

    def action_interrupt(self):
        """
        Interrupt the current task, or copy selected text to clipboard.
        """

        # Determine which coder is in the foreground
        coder = self.worker.coder if self.worker else None
        if coder:
            try:
                agent_service = AgentService.get_instance(coder)
                foreground = agent_service.foreground_coder
                if foreground is not None and foreground is not coder:
                    # Sub-agent is in the foreground — interrupt it directly
                    foreground.keyboard_interrupt()
                elif self.worker:
                    # Primary coder is in the foreground — use worker
                    self.worker.interrupt()
            except Exception:
                if self.worker:
                    self.worker.interrupt()
        elif self.worker:
            self.worker.interrupt()

        # Notify user
        try:
            status_bar = self.query_one("#status-bar", StatusBar)
            status_bar.show_notification("Interrupting...", severity="warning", timeout=3)
        except Exception:
            pass

    def action_quit(self):
        """Quit the application."""
        # Prevent multiple quit attempts
        if hasattr(self, "_quitting") and self._quitting:
            return
        self._quitting = True

        # Show shutdown message
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.show_notification("Shutting down...", severity="warning", timeout=None)

        # Delay exit to allow status bar to render
        self.set_timer(0.3, self._do_quit)

    def action_noop(self):
        pass

    def action_history_search(self):
        """Open an external editor to compose a prompt (keyboard shortcut)."""
        # Get current input text to use as initial content
        input_area = self.query_one("#input", InputArea)
        input_area.post_message(input_area.Submit("/history-search"))

    def action_open_editor(self):
        """Open an external editor to compose a prompt (keyboard shortcut)."""
        # Get current input text to use as initial content
        input_area = self.query_one("#input", InputArea)
        current_text = input_area.value
        self._open_editor_suspended(current_text)

    def _open_editor_suspended(self, initial_content=""):
        """Open an external editor with proper TUI suspension.

        Args:
            initial_content: Initial text to populate the editor with
        """
        # Get editor from coder's commands or default
        editor = getattr(self.worker.coder.commands, "editor", None)

        # Suspend TUI and open editor
        with self.suspend():
            edited_text = pipe_editor(initial_content, suffix="md", editor=editor)

        # Set the edited text back to input
        input_area = self.query_one("#input", InputArea)
        if edited_text and edited_text.strip():
            input_area.value = edited_text.rstrip()
            input_area.focus()

            # Show notification
            try:
                status_bar = self.query_one("#status-bar", StatusBar)
                status_bar.show_notification(
                    "Editor content loaded", severity="information", timeout=2
                )
            except Exception:
                pass
        else:
            input_area.focus()

        return edited_text

    def get_response_from_editor(self, initial_content=""):
        """Open an external editor with proper TUI suspension.

        Args:
            initial_content: Initial text to populate the editor with

        Returns:
            Edited text
        """
        # Get editor from coder's commands or default
        editor = getattr(self.worker.coder.commands, "editor", None)

        # Suspend TUI and open editor
        input_area = self.query_one("#input", InputArea)
        edited_text = ""
        edited_text = self.run_obstructive(pipe_editor, initial_content, suffix="md", editor=editor)
        input_area.focus()

        return edited_text.rstrip()

    def action_switch_to_primary(self) -> None:
        """Switch to the primary (parent) agent container."""
        # primary_uuid = str(self.worker.coder.uuid)
        agent_service = AgentService.get_instance(self.worker.coder)
        if agent_service.foreground_uuid is None:
            return
        # Update foreground agent in AgentService
        agent_service.foreground_uuid = None  # None = primary coder
        # Show primary container, hide sub-agent containers
        primary = self.query_one("#output", OutputContainer)
        primary.display = True

        for uuid_key, container in self._sub_agent_containers.items():
            container.display = False

        # Update border title with mode and sub-agent info
        self._sync_sub_agent_display()

        # Update input autocomplete data for the primary agent
        self.enable_input({}, coder=self.worker.coder)

        self._refresh_footer()

    def action_switch_prev_agent(self) -> None:
        """Switch to the previous agent (primary or sub-agent), wrapping around."""
        if not self._sub_agent_containers:
            return
        primary_uuid = str(self.worker.coder.uuid)
        uuids = [primary_uuid] + list(self._sub_agent_containers.keys())
        current = str(self._get_visible_coder().uuid)
        try:
            idx = uuids.index(current)
            next_uuid = uuids[(idx - 1) % len(uuids)]
        except ValueError:
            next_uuid = uuids[0]
        self._switch_to_container(next_uuid)

    def action_switch_next_agent(self) -> None:
        """Switch to the next agent (primary or sub-agent), wrapping around."""
        if not self._sub_agent_containers:
            return
        primary_uuid = str(self.worker.coder.uuid)
        uuids = [primary_uuid] + list(self._sub_agent_containers.keys())
        current = str(self._get_visible_coder().uuid)
        try:
            idx = uuids.index(current)
            next_uuid = uuids[(idx + 1) % len(uuids)]
        except ValueError:
            next_uuid = uuids[0]
        self._switch_to_container(next_uuid)

    def _switch_to_container(self, uuid: str, suppress_input_enable: bool = False) -> None:
        """Internal helper to switch active container.

        Args:
            uuid: The container UUID to switch to.
            suppress_input_enable: If True, skip re-enabling the input area.
                Used during confirmations to avoid undoing input disabling.
        """
        # Update foreground agent in AgentService
        agent_service = AgentService.get_instance(self.worker.coder)
        primary_uuid = str(self.worker.coder.uuid)

        # Check if the target container exists
        if uuid != primary_uuid and uuid not in self._sub_agent_containers:
            # Sub-agent container not found, fall back to primary
            self.show_error(f"Agent container for UUID {uuid} not found. Switching to primary.")
            uuid = primary_uuid

        if uuid == primary_uuid:
            # Switch to primary agent
            agent_service.foreground_uuid = None
            primary = self.query_one("#output", OutputContainer)
            primary.display = True
            for container in self._sub_agent_containers.values():
                container.display = False
        else:
            # Switch to a sub-agent
            agent_service.foreground_uuid = uuid
            primary = self.query_one("#output", OutputContainer)
            primary.display = False
            for cid, container in self._sub_agent_containers.items():
                container.display = cid == uuid

        # Update border title with mode and sub-agent info
        self._sync_sub_agent_display()

        # Update input autocomplete data for the active agent
        if (
            not suppress_input_enable
            and not self._confirmation_lock
            and not self._confirmations_pending
        ):
            coder = agent_service.foreground_coder
            self.enable_input({}, coder=coder)

        self._refresh_footer()

    def create_sub_agent_container(self, uuid: str, name: str) -> None:
        """Create an OutputContainer for a sub-agent."""
        from cecli.helpers.agents.service import AgentService

        if uuid in self._sub_agent_containers:
            agent_service = AgentService.get_instance(self.worker.coder)
            sub_agent_info = agent_service.sub_agents.get(uuid)
            if sub_agent_info:
                sub_agent_info.coder.show_announcements()

            return

        container = OutputContainer(id=f"output-{uuid}", classes="subagent-output")
        container.display = False  # Hidden initially
        self._sub_agent_containers[uuid] = container
        self.mount(container, before="#status-bar")

        # Display the banner on the new sub-agent container
        if self.tui_config["banner"]:
            container.add_output(self.BANNER, dim=False)
        else:
            container.add_output(
                f"[bold {self.BANNER_COLORS[0]}] [/bold {self.BANNER_COLORS[0]}]", dim=False
            )

        # Show announcements from the sub-agent's coder
        try:
            agent_service = AgentService.get_instance(self.worker.coder)
            sub_agent_info = agent_service.sub_agents.get(uuid)
            if sub_agent_info:
                sub_agent_info.coder.show_announcements()
        except Exception:
            pass

        # Sync border title with mode and sub-agent info
        self._sync_sub_agent_display()

    def remove_sub_agent_container(self, uuid: str) -> None:
        """Remove a sub-agent's container and pill."""
        container = self._sub_agent_containers.pop(uuid, None)
        was_visible = False
        if container is not None:
            was_visible = container.display
            try:
                container.remove()
            except Exception:
                pass

        if was_visible:
            # The removed container was visible — reset foreground tracking
            # and show the primary container.  We check the container's
            # display state directly rather than _get_visible_coder() because
            # _cleanup_sub_agent() on the worker thread may have already
            # reset foreground_uuid by the time we run here.
            agent_service = AgentService.get_instance(self.worker.coder)
            agent_service.foreground_uuid = None
            primary = self.query_one("#output", OutputContainer)
            primary.display = True
            self._refresh_footer()

        # Sync border title with mode and sub-agent info
        self._sync_sub_agent_display()

    def _project_name_for_coder(self, coder) -> str:
        """Compute a display project name for a coder's root (home-shortened)."""
        home = str(Path.home())
        repo = getattr(coder, "repo", None)
        root = str(getattr(coder, "root", "") or "")
        if not root and repo:
            root = str(repo.root)
        if not root:
            root = str(Path.cwd())
        if root.startswith(home):
            project_name = root.replace(home, "~", 1)
        else:
            project_name = root
        if len(project_name) >= 64:
            project_name = project_name.split("/")[-1]
        return project_name

    def _refresh_footer(self):
        """Refresh the footer with the active coder's project/root and git branch."""
        try:
            footer = self.query_one(MainFooter)
            coder = self._get_visible_coder()
            project_name = self._project_name_for_coder(coder)
            branch = ""
            repo = getattr(coder, "repo", None)
            if repo:
                try:
                    branch = repo.repo.active_branch.name or "main"
                except Exception:
                    branch = branch or "main"
            footer.update_info(project_name, branch)
        except Exception:
            pass

    def _git_branch_fingerprint(self):
        """Return a cheap signature for the active repo's checked-out branch.

        The ``.git/HEAD`` file holds the branch reference (``ref: refs/heads/<name>``)
        and is rewritten on every checkout/switch and on rename of the current
        branch, so its content alone is enough to detect a displayed-branch-name
        change without running git.  Returns ``None`` when there is no usable repo.
        """
        repo = getattr(self._get_visible_coder(), "repo", None)
        if not repo:
            return None

        try:
            git_dir = Path(repo.repo.git_dir)
        except Exception:
            return None

        try:
            return (git_dir / "HEAD").read_text(errors="replace").strip()
        except OSError:
            return None

    def _check_git_branch(self):
        """Cheap poll: refresh the footer only when the branch fingerprint changed."""
        fingerprint = self._git_branch_fingerprint()
        if fingerprint is None or fingerprint == self._git_branch_fp:
            return

        self._git_branch_fp = fingerprint
        self._refresh_footer()

    def _sync_sub_agent_display(self) -> None:
        """Update the InputContainer border title with mode and sub-agent pills.

        Delegates to the InputContainer itself, which queries AgentService
        via self.app to build the pill indicators.
        """
        input_container = self.query_one("#input-container", InputContainer)
        coder = self.worker.coder
        mode = getattr(coder, "edit_format", "code") or "code"
        input_container.update_mode(mode)

    def _get_output_container(self, msg):
        """Get the output container for a message, routing by coder_uuid.

        If the message has a coder_uuid matching a sub-agent container,
        route to that container. Otherwise, route to the primary container.
        """
        coder_uuid = msg.get("coder_uuid")

        if coder_uuid and coder_uuid in self._sub_agent_containers:
            return self._sub_agent_containers[coder_uuid]

        return self.query_one("#output", OutputContainer)

    def get_selected_log_text(self) -> str | None:
        """Get selected text from the visible output container or screen."""
        output_container = self._get_visible_container()
        return output_container.get_selected_text()

    def copy_selected_log_text(self):
        output_container = self._get_visible_container()
        output_container.get_selected_text(copy=True)

    def clear_selected_log_text(self):
        output_container = self._get_visible_container()
        output_container.clear_selection()

    def _get_visible_coder(self):
        """Return the currently visible coder (foreground or primary)."""
        from cecli.helpers.agents.service import AgentService

        return AgentService.get_instance(self.worker.coder).foreground_coder or self.worker.coder

    def _get_visible_container(self):
        """Return the currently visible output container.

        If a sub-agent container is active, return that container.
        Otherwise, return the primary output container.
        """
        coder = self._get_visible_coder()
        coder_uuid = str(coder.uuid)
        primary_uuid = str(self.worker.coder.uuid)

        if coder_uuid != primary_uuid and coder_uuid in self._sub_agent_containers:
            return self._sub_agent_containers[coder_uuid]

        return self.query_one("#output", OutputContainer)

    def _encode_keys(self, key):
        key = key.replace("shift+enter", "ctrl+j")

        return key

    def _decode_keys(self, key):
        key = key.replace("ctrl+j", "shift+enter")

        return key

    def is_key_for(self, type, key):
        allowed_keys = self.tui_config["key_bindings"][type].split(",")
        if key in allowed_keys:
            return True

        return False

    def get_keys_for(self, type):
        allowed_keys = self.tui_config["key_bindings"][type]
        return self._decode_keys(allowed_keys)

    def _do_quit(self):
        """Perform the actual quit after UI updates."""
        self.worker.stop()
        self.exit()

    def run_obstructive(self, func, *args, **kwargs):
        """Run a function with the TUI suspended, called from a worker thread."""
        future = concurrent.futures.Future()

        def wrapper():
            try:
                with self.suspend():
                    result = func(*args, **kwargs)
                    future.set_result(result)
            except Exception as e:
                future.set_exception(e)

        self.call_from_thread(wrapper)
        return future.result()

    def on_cost_update(self, message: CostUpdate):
        """Handle cost update from output."""
        footer = self.query_one(MainFooter)
        footer.cost = message.cost
        footer.refresh()

    def on_status_bar_confirm_response(self, message: StatusBar.ConfirmResponse):
        """Handle confirmation response from status bar."""
        # Re-enable input
        input_area = self.query_one("#input", InputArea)
        input_area.disabled = False
        input_area.focus()

        coder_uuid = self._confirmation_coder_uuid
        # Route to per-coder queue when available
        if coder_uuid and coder_uuid in queues._per_coder_queues:
            queues.push_coder_input(
                coder_uuid, {"confirmed": message.result, "coder_uuid": coder_uuid}
            )
        else:
            self.input_queue.put({"confirmed": message.result, "coder_uuid": coder_uuid})
            queues.wake_input_waiters()
        # Release the confirmation lock and process any pending confirmations
        self._confirmation_lock = False
        self._process_pending_confirmation()

    def _process_pending_confirmation(self) -> None:
        """Process the next pending confirmation from the queue, if any."""
        if self._confirmations_pending:
            next_msg, next_agent_name = self._confirmations_pending.pop(0)
            self.show_confirmation(next_msg, agent_name=next_agent_name)

    # Commands that use path-based completion
    PATH_COMPLETION_COMMANDS = {"/add", "/read-only", "/read-only-stub", "/rules", "/load", "/save"}

    def _extract_symbols(self) -> set[str]:
        """Extract code symbols from files in chat using Pygments."""
        coder = self.worker.coder

        # Get current files in chat
        inchat_files = []
        if hasattr(coder, "abs_fnames"):
            inchat_files.extend(coder.abs_fnames)
        if hasattr(coder, "abs_read_only_fnames"):
            inchat_files.extend(coder.abs_read_only_fnames)

        # Check if cache is still valid
        files_hash = hash(tuple(sorted(inchat_files)))
        if self._symbols_cache is not None and self._symbols_files_hash == files_hash:
            return self._symbols_cache

        symbols = set()

        # Also add filenames as completable symbols
        if hasattr(coder, "get_inchat_relative_files"):
            symbols.update(coder.get_inchat_relative_files())

        # Limit files to tokenize for performance
        files_to_process = inchat_files[:30]

        try:
            from pygments.lexers import guess_lexer_for_filename
            from pygments.token import Token
        except ImportError:
            # Pygments not available, just return filenames
            self._symbols_cache = symbols
            self._symbols_files_hash = files_hash
            return symbols

        for fname in files_to_process:
            try:
                with safe_open(fname, "r", errors="ignore") as f:
                    content = f.read()

                lexer = guess_lexer_for_filename(fname, content)
                tokens = lexer.get_tokens(content)

                for token_type, token_value in tokens:
                    # Extract identifiers (function names, class names, variables)
                    if token_type in Token.Name and len(token_value) > 1:
                        symbols.add(token_value)
            except Exception:
                continue

        self._symbols_cache = symbols
        self._symbols_files_hash = files_hash
        return symbols

    def _get_symbol_completions(self, prefix: str) -> list[str]:
        """Get symbol completions for @ mentions."""
        symbols = self._extract_symbols()
        prefix_lower = prefix.lower()
        should_sort = True

        if prefix:
            matches, matches_set = self._get_path_completions(prefix)
            # Use a set to efficiently filter out symbols already in matches
            for s in symbols:
                if prefix_lower in s.lower() and s not in matches_set:
                    matches.append(s)
                    matches_set.add(s)
            should_sort = False
        else:
            matches = list(symbols)

        return matches[:50] if not should_sort else sorted(matches)[:50]

    def _get_path_completions(self, prefix: str) -> tuple[list[str], set[str]]:
        """Get filesystem path completions relative to coder root.

        Uses FileSystemService when available for efficient trie/trigram
        lookups, with fallback to legacy filesystem iteration.

        Returns:
            tuple[list[str], set[str]]: A tuple of (ordered_list, fast_lookup_set)
                containing the matched path completions.
        """
        coder = AgentService.get_instance(self.worker.coder).foreground_coder
        root = Path(coder.root) if hasattr(coder, "root") else Path.cwd()

        # Try FileSystemService first for efficient lookups
        try:
            fs = getattr(coder, "fs", None) or FileSystemService.for_root(
                str(root), repo=getattr(coder, "repo", None)
            )
            if prefix:
                if fs.trie:
                    is_fuzzy = False
                    if fs.trie:
                        matches = fs.list_prefix(prefix)

                    if not matches:
                        is_fuzzy = True
                        matches = fs.search(prefix, threshold=0.1)

                    if matches:
                        result = sorted(matches) if not is_fuzzy else matches
                        return result, set(result)
        except Exception:
            pass

        # Fallback: iterate filesystem directory
        if "/" in prefix:
            # Has directory component
            dir_part, file_part = prefix.rsplit("/", 1)
            search_dir = root / dir_part
            search_prefix = file_part.lower()
            path_prefix = dir_part + "/"
        else:
            search_dir = root
            search_prefix = prefix.lower()
            path_prefix = ""

        completions = []
        try:
            if search_dir.exists() and search_dir.is_dir():
                for entry in search_dir.iterdir():
                    name = entry.name
                    if search_prefix and search_prefix not in name.lower():
                        continue
                    # Add trailing slash for directories
                    if entry.is_dir():
                        completions.append(path_prefix + name + "/")
                    else:
                        completions.append(path_prefix + name)
        except (PermissionError, OSError):
            pass

        result = sorted(completions)
        return result, set(result)

    def _get_suggestions(self, text: str) -> list[str]:
        """Get completion suggestions for given text."""
        suggestions = []
        commands = self.worker.coder.commands
        active_coder = AgentService.get_instance(self.worker.coder).foreground_coder

        # Only return early for non-commands ending with space
        # For commands, we want to allow completion with empty string partial
        if len(text) and text[-1] == " " and not text.startswith("/"):
            return

        if "@" in text:
            # Symbol completion triggered by @
            # Find the @ and get the prefix after it
            at_index = text.rfind("@")
            prefix = text[at_index + 1 :]
            suggestions = self._get_symbol_completions(prefix)
        elif text.startswith("/"):
            # Command completion
            parts = text.split(maxsplit=1)
            cmd_part = parts[0]

            if len(parts) == 1 and not text.endswith(" "):
                # Complete command name
                all_commands = commands.get_commands()
                if cmd_part == "/":
                    suggestions = all_commands
                else:
                    # First get commands that start with the prefix
                    starts_with = [c for c in all_commands if c.startswith(cmd_part)]
                    # Then get commands that contain the prefix anywhere (excluding those already matched)
                    contains = [
                        c for c in all_commands if cmd_part[1:] in c and not c.startswith(cmd_part)
                    ]

                    suggestions = starts_with + contains
            else:
                # Complete command argument
                # This handles both:
                # 1. len(parts) > 1: command with arguments
                # 2. len(parts) == 1 and text.endswith(" "): command with trailing space
                cmd_name = cmd_part

                if text.endswith(" "):
                    # Command with trailing space, empty argument prefix
                    arg_prefix = ""
                else:
                    # Get the last word as argument prefix
                    end_lookup = text.rsplit(maxsplit=1)
                    arg_prefix = end_lookup[-1]

                arg_prefix_lower = arg_prefix.lower()

                # Check if this command needs path-based completion
                if cmd_name in self.PATH_COMPLETION_COMMANDS:
                    suggestions, suggestions_set = self._get_path_completions(arg_prefix)
                    # For /read-only and /read-only-stub, also include add completions
                    if cmd_name in {"/add", "/read-only", "/read-only-stub"}:
                        try:
                            add_completions = (
                                commands.get_completions(cmd_name, coder=active_coder) or []
                            )
                            for c in add_completions:
                                c_str = str(c)
                                if (
                                    arg_prefix_lower in c_str.lower()
                                    and c_str not in suggestions_set
                                ):
                                    suggestions.append(c_str)
                                    suggestions_set.add(c_str)
                        except Exception:
                            pass
                else:
                    # Use standard command completions (no file fallback)
                    try:
                        cmd_completions = commands.get_completions(
                            cmd_name, args=arg_prefix, coder=active_coder
                        )
                        if cmd_completions:
                            exempt_from_substring_matching = {
                                "/model",
                                "/models",
                                "/agent-model",
                                "/editor-model",
                                "/weak-model",
                            }
                            if arg_prefix and cmd_name not in exempt_from_substring_matching:
                                suggestions = [
                                    c for c in cmd_completions if arg_prefix_lower in str(c).lower()
                                ]
                            else:
                                suggestions = list(cmd_completions)
                    except Exception:
                        pass
        else:
            # Check if last contiguous, no-space separated string contains a forward slash
            # This allows path completions even without a leading slash
            words = text.rsplit(maxsplit=1)

            if words:
                last_word = words[-1]
                if "/" in last_word:
                    # Provide path completions for the partial path
                    suggestions = self._get_symbol_completions(last_word)

        return [str(s) for s in suggestions[:50]]

    def _get_completed_text(self, current_text: str, completion: str) -> str:
        """Calculate the new text after applying completion."""
        if current_text.startswith("/"):
            parts = current_text.rsplit(maxsplit=1)

            # Check if we have a command with trailing space
            # This is when we want to insert argument completions after the space
            if len(parts) == 1 and current_text.endswith(" "):
                # Command with trailing space, insert completion after space
                return current_text + completion
            elif len(parts) == 1:
                # Replace entire command (command name completion)
                # Only add space if command takes arguments
                commands = self.worker.coder.commands
                try:
                    cmd_completions = commands.get_completions(completion)
                    has_completions = cmd_completions is not None
                except Exception as e:
                    # Check if it's a CommandCompletionException
                    if isinstance(e, CommandCompletionException):
                        # For CommandCompletionException, treat it as having completions
                        # so we add a space after the command
                        has_completions = True
                    else:
                        # For other exceptions, assume no completions
                        has_completions = False

                if has_completions:
                    return completion + " "
                else:
                    return completion
            else:
                # Replace argument
                return parts[0] + " " + completion
        elif "@" in current_text:
            # Replace from @ onwards with the symbol
            at_index = current_text.rfind("@")
            return current_text[:at_index] + completion + " "
        else:
            # Replace last word with completion
            words = current_text.rsplit(maxsplit=1)
            if len(words) > 1:
                return words[0] + " " + completion
            else:
                return completion

    def on_input_area_completion_requested(self, message: InputArea.CompletionRequested):
        """Handle completion request - show or update completion bar."""
        input_area = self.query_one("#input", InputArea)
        text = message.text
        suggestions = self._get_suggestions(text)

        # Check if completion bar already exists
        existing_bar = None
        try:
            existing_bar = self.query_one("#completion-bar", CompletionBar)
        except Exception:
            pass

        if suggestions:
            input_area.completion_active = True
            if existing_bar:
                # Update existing bar in place
                existing_bar.update_suggestions(suggestions, text)
            else:
                # Create new completion bar
                completion_bar = CompletionBar(
                    suggestions=suggestions, prefix=text, id="completion-bar"
                )
                self.mount(completion_bar, before=input_area)

            # Update key hints with description for first suggestion
            if suggestions:
                first_suggestion = suggestions[0]
                self._update_key_hints_for_commands(first_suggestion, is_completion=True)
        else:
            # No suggestions - dismiss if active
            input_area.completion_active = False
            if existing_bar:
                existing_bar.remove()

    def on_input_area_completion_cycle(self, message: InputArea.CompletionCycle):
        """Handle Tab to cycle through completions."""
        try:
            completion_bar = self.query_one("#completion-bar", CompletionBar)
            completion_bar.cycle_next()
            selected = completion_bar.current_selection
            if selected:
                input_area = self.query_one("#input", InputArea)
                # Use completion_prefix as base
                base_text = input_area.completion_prefix
                new_text = self._get_completed_text(base_text, selected)
                input_area.set_completion_preview(new_text)
                # Update key hints with command description for selected completion
                self._update_key_hints_for_commands(selected, is_completion=True)
        except Exception:
            pass

    def on_input_area_completion_cycle_previous(self, message: InputArea.CompletionCyclePrevious):
        """Handle Tab to cycle through completions."""
        try:
            completion_bar = self.query_one("#completion-bar", CompletionBar)
            completion_bar.cycle_previous()
            selected = completion_bar.current_selection
            if selected:
                input_area = self.query_one("#input", InputArea)
                # Use completion_prefix as base
                base_text = input_area.completion_prefix
                new_text = self._get_completed_text(base_text, selected)
                input_area.set_completion_preview(new_text)
                # Update key hints with command description for selected completion
                self._update_key_hints_for_commands(selected, is_completion=True)
        except Exception:
            pass

    def on_input_area_completion_accept(self, message: InputArea.CompletionAccept):
        """Handle Enter to accept current completion."""
        try:
            completion_bar = self.query_one("#completion-bar", CompletionBar)
            completion_bar.select_current()
        except Exception:
            pass
        # Update key hints based on accepted completion
        input_area = self.query_one("#input", InputArea)
        self._update_key_hints_for_commands(input_area.text, is_completion=False)

    def on_input_area_completion_dismiss(self, message: InputArea.CompletionDismiss):
        """Handle Escape to dismiss completions."""
        input_area = self.query_one("#input", InputArea)
        input_area.completion_active = False
        try:
            completion_bar = self.query_one("#completion-bar", CompletionBar)
            completion_bar.dismiss()
        except Exception:
            pass
        # Update key hints back to normal based on current input
        self._update_key_hints_for_commands(input_area.text, is_completion=False)

    def on_completion_bar_selected(self, message: CompletionBar.Selected):
        """Handle completion selection."""
        input_area = self.query_one("#input", InputArea)

        # Use stored prefix as base for completion
        current = input_area.completion_prefix
        selected = message.value

        new_text = self._get_completed_text(current, selected)

        # Reset cycling state so the new value is registered as the new prefix
        input_area._cycling = False
        input_area.value = new_text
        input_area.completion_active = False

        input_area.focus()
        input_area.cursor_position = len(input_area.value)

    def on_completion_bar_dismissed(self, message: CompletionBar.Dismissed):
        """Handle completion bar dismissal."""
        input_area = self.query_one("#input", InputArea)

        # Restore original text if we were cycling
        if input_area._cycling:
            input_area.value = input_area.completion_prefix
            input_area._cycling = False

        input_area.completion_active = False
        input_area.focus()


def patch_color_name_to_rgb():
    """Inject Rich 256-color names into Textual's COLOR_NAME_TO_RGB dict.

    Textual's COLOR_NAME_TO_RGB only knows ANSI-16 + CSS named colors.
    Rich's 256-color names (spring_green2, bright_cyan, etc.) are parsed
    correctly by Content.from_markup() but silently dropped at render time
    because Color.parse() doesn't recognize them.

    This patch resolves every Rich color name to its truecolor RGB via
    RichStyle.parse() and adds it to the dict, making all Rich color names
    work natively in Textual's Static widgets.
    """
    from rich.color import ANSI_COLOR_NAMES
    from rich.style import Style as RichStyle
    from textual._color_constants import COLOR_NAME_TO_RGB

    added = 0
    for name in ANSI_COLOR_NAMES:
        if name in COLOR_NAME_TO_RGB:
            continue
        try:
            style = RichStyle.parse(name)
        except Exception:
            continue
        if style.color is None or style.color.type is None:
            continue
        triplet = style.color.get_truecolor()
        if triplet is not None:
            COLOR_NAME_TO_RGB[name] = (triplet.red, triplet.green, triplet.blue)
            added += 1

    return added


def patch_textual_strip_render_with_cache():
    # 1. Define the logic
    def modified_render_ansi(cls, style: Style, color_system: ColorSystem) -> str:
        """Modified ANSI generator that ignores background colors."""
        sgr: list[str]
        # Handle Attributes
        if attributes := style._attributes & style._set_attributes:
            _style_map = textual.strip.SGR_STYLES
            sgr = [
                _style_map[bit_offset]
                for bit_offset in range(attributes.bit_length())
                if attributes & (1 << bit_offset)
            ]
        else:
            sgr = []

        # Handle Foreground Color
        if (color := style._color) is not None:
            sgr.extend(color.downgrade(color_system).get_ansi_codes())

        # BACKGROUND OVERRIDE: Skip the bgcolor block entirely

        ansi = style._ansi = ";".join(sgr)
        return ansi

    # 2. Re-apply the EXACT cache settings from the original source
    cached_version = lru_cache(maxsize=16384)(modified_render_ansi)

    # 3. Convert to classmethod and inject
    textual.strip.Strip.render_ansi = classmethod(cached_version)


# Execute the patches
patch_color_name_to_rgb()
# patch_textual_strip_render_with_cache()
