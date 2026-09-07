import json
import re
import sys
import weakref
from pathlib import Path

from cecli.commands.utils.registry import CommandRegistry
from cecli.helpers import nested, plugin_manager
from cecli.helpers.file_searcher import handle_core_files
from cecli.helpers.threading import ThreadSafeEvent
from cecli.signals import SwitchCoderSignal


class Commands:
    scraper = None

    def _get_coder(self):
        """Return coder via weak reference, or None if collected."""
        if self._coder_ref is not None:
            return self._coder_ref()
        return None

    def _set_coder(self, value):
        """Store coder as weakref to break circular reference chains."""
        self._coder_ref = weakref.ref(value) if value is not None else None

    coder = property(_get_coder, _set_coder)

    def clone(self):
        cloned = Commands(
            self.io,
            None,
            voice_language=self.voice_language,
            voice_input_device=self.voice_input_device,
            voice_format=self.voice_format,
            verify_ssl=self.verify_ssl,
            args=self.args,
            parser=self.parser,
            verbose=self.verbose,
            editor=self.editor,
            original_read_only_fnames=self.original_read_only_fnames,
        )
        cloned.last_command_show_notification = self.last_command_show_notification
        return cloned

    def __init__(
        self,
        io,
        coder,
        voice_language=None,
        voice_input_device=None,
        voice_format=None,
        verify_ssl=True,
        args=None,
        parser=None,
        verbose=False,
        editor=None,
        original_read_only_fnames=None,
    ):
        self.io = io
        # Use weak ref to avoid circular reference chains
        self._coder_ref = weakref.ref(coder) if coder else None
        self.parser = parser
        self.args = args
        self.verbose = verbose
        self.verify_ssl = verify_ssl
        if voice_language == "auto":
            voice_language = None
        self.voice_language = voice_language
        self.voice_format = voice_format
        self.voice_input_device = voice_input_device
        self.help = None
        self.editor = editor
        self.original_read_only_fnames = set(original_read_only_fnames or [])

        customizations = dict()
        try:
            if self.args:
                customizations = nested.getter(self.args, "custom", "{}")
                customizations = json.loads(customizations)
        except (json.JSONDecodeError, TypeError):
            customizations = dict()
            pass

        self.custom_commands = nested.getter(customizations, "command-paths", [])
        self._load_custom_commands(self.custom_commands)

        self.cmd_running_event = ThreadSafeEvent()
        self.cmd_running_event.set()
        self.last_command_show_notification = True
        self.prompt_queue = []
        self._queue_counter = 0
        self._queue_lock = ThreadSafeEvent()
        self._processing_queue = False
        self.prompt_queue = []
        self._queue_counter = 0
        self._queue_lock = ThreadSafeEvent()
        self._processing_queue = False

        # Commands that should NOT trigger auto-processing of the queue
        self._MANAGEMENT_COMMANDS = {"queue", "list-queue", "remove-queue", "insert-queue"}

    # ── Queue Management Methods (CLI-33) ────────────────────────────── #
    #
    # The prompt queue itself lives on the coder (Coder.prompt_queue) and
    # is managed by cecli.helpers.command_queue. These thin wrappers keep
    # the /queue, /list-queue and /remove-queue command implementations
    # stable while operating on the coder that owns this Commands
    # instance, so each sub-agent's commands manage that sub-agent's own
    # queue.

    def _active_coder(self):
        """Resolve the coder queue commands should target.

        Prefers the foreground (sub-agent) coder via
        ``command_queue.get_active_coder``, falling back to ``self.coder``
        when no active coder can be resolved (e.g. no AgentService, or the
        Commands instance is constructed without a coder in tests).
        """
        from cecli.helpers import command_queue

        return command_queue.get_active_coder(self.coder) or self.coder

    @property
    def prompt_queue(self):
        """Proxy to the active (foreground) coder's prompt queue.

        Resolves through ``command_queue.get_active_coder`` so that when a
        sub-agent is in the foreground, ``/list-queue`` and friends target
        that sub-agent's queue rather than the primary coder's queue.
        """
        target = self._active_coder()
        return target.prompt_queue if target is not None else []

    def _enqueue_prompt(self, text: str) -> dict:
        """Add a prompt to the active (foreground) coder's queue."""
        from cecli.helpers import command_queue

        return command_queue.enqueue_prompt(self._active_coder(), text)

    async def _process_queued_prompts(self, preproc):
        """Process all queued prompts (FIFO) once the current message completes.

        Runs each queued prompt through ``run_one`` so it is sent to the LLM
        exactly like a user-typed prompt. A single failing queued prompt is
        logged and does not stop the remaining ones (``SwitchCoderSignal`` and
        ``ReloadProgramSignal`` are BaseExceptions and propagate unchanged).
        """
        if self._processing_queue:
            return
        self._processing_queue = True
        try:
            while True:
                item = self._dequeue_prompt()
                if item is None:
                    break
                text = item["text"]
                preview = text if len(text) <= 80 else text[:80] + "..."
                self.io.tool_output(f"Processing queued prompt: {preview}")
                try:
                    await self.coder.run_one(text, preproc)
                except Exception as e:
                    self.io.tool_error(f"Error processing queued prompt: {e}")
        finally:
            self._processing_queue = False

    def _insert_prompt(self, text: str, index: int) -> dict:
        """Insert a prompt at the given index in the active coder's queue."""
        from cecli.helpers import command_queue

        return command_queue.insert_prompt(self._active_coder(), text, index)

    def _dequeue_prompt(self) -> dict | None:
        """Remove and return the first item from the active coder's queue."""
        from cecli.helpers import command_queue

        return command_queue.dequeue_prompt(self._active_coder())

    def _get_queue_length(self) -> int:
        """Return the current number of items in the active coder's queue."""
        from cecli.helpers import command_queue

        return command_queue.get_queue_length(self._active_coder())

    def _remove_from_queue(self, index: int) -> dict | None:
        """Remove and return the item at the given index from the active coder's queue."""
        from cecli.helpers import command_queue

        return command_queue.remove_from_queue(self._active_coder(), index)

    def _clear_queue(self) -> list:
        """Remove all items from the active coder's queue and return them."""
        from cecli.helpers import command_queue

        return command_queue.clear_queue(self._active_coder())

    def _load_custom_commands(self, custom_commands):
        """
        Load custom commands from plugin paths.

        Args:
            custom_commands: List of file or directory paths to load custom commands from.
                             If None or empty, no custom commands are loaded.
        """
        if not custom_commands:
            return

        for path_str in custom_commands:
            path = Path(path_str)
            try:
                if path.is_dir():
                    # Find all Python files in the directory
                    for py_file in path.glob("*.py"):
                        self._load_command_from_file(py_file)
                else:
                    # If it's a file, try to load it directly
                    if path.exists() and path.suffix == ".py":
                        self._load_command_from_file(path)
            except Exception as e:
                # Log error but continue with other paths
                if self.io:
                    self.io.tool_error(f"Error loading custom commands from {path}: {e}")

    def _load_command_from_file(self, file_path):
        """
        Load a command class from a Python file.

        Args:
            file_path: Path to the Python file to load.
        """
        try:
            # Load the module using plugin_manager
            module = plugin_manager.load_module(str(file_path))

            # Look for a class named exactly "CustomCommand" in the module
            if hasattr(module, "CustomCommand"):
                command_class = getattr(module, "CustomCommand")
                if isinstance(command_class, type):
                    # Register the command class
                    CommandRegistry.register(command_class)
                    if self.io and self.verbose:
                        self.io.tool_output(f"Registered custom command: {command_class.NORM_NAME}")

        except Exception as e:
            # Log error but continue with other files
            if self.io:
                self.io.tool_error(f"Error loading command from {file_path}: {e}")

    def is_command(self, inp):
        return inp[0] in "/!"

    def is_run_command(self, inp):
        return inp and (
            inp[0] in "!" or inp[:5] == "/lint" or inp[:5] == "/test" or inp[:4] == "/run"
        )

    def is_test_command(self, inp):
        return inp and (inp[:5] == "/lint" or inp[:5] == "/test")

    def get_raw_completions(self, cmd):
        assert cmd.startswith("/")
        cmd = cmd[1:]
        cmd = cmd.replace("-", "_")
        raw_completer = getattr(self, f"completions_raw_{cmd}", None)
        return raw_completer

    def get_completions(self, cmd, args="", coder=None):
        assert cmd.startswith("/")
        cmd = cmd[1:]
        command_class = CommandRegistry.get_command(cmd)
        if command_class:
            return command_class.get_completions(self.io, coder or self.coder, args)
        return []

    def get_commands(self):
        registry_commands = CommandRegistry.list_commands()
        commands = [f"/{cmd}" for cmd in registry_commands]
        return sorted(commands)

    async def execute(self, cmd_name, args, coder=None, **kwargs):
        from cecli.repo import ANY_GIT_ERROR

        active_coder = coder or self.coder
        command_class = CommandRegistry.get_command(cmd_name)

        if not command_class:
            active_coder.io.tool_output(f"Error: Command {cmd_name} not found.")
            return

        self.last_command_show_notification = command_class.show_completion_notification
        if cmd_name not in self._MANAGEMENT_COMMANDS:
            self.cmd_running_event.clear()

        try:
            kwargs.update(
                {
                    "original_read_only_fnames": self.original_read_only_fnames,
                    "voice_language": self.voice_language,
                    "voice_format": self.voice_format,
                    "voice_input_device": self.voice_input_device,
                    "verify_ssl": self.verify_ssl,
                    "parser": self.parser,
                    "verbose": self.verbose,
                    "editor": self.editor,
                    "system_args": self.args,
                }
            )
            return await CommandRegistry.execute(
                cmd_name, active_coder.io, active_coder, args, **kwargs
            )
        except ANY_GIT_ERROR as err:
            active_coder.io.tool_error(f"Unable to complete {cmd_name}: {err}")
            return
        except SwitchCoderSignal as e:
            raise e
        except Exception as e:
            active_coder.io.tool_error(f"Error executing command {cmd_name}: {str(e)}")
            return
        finally:
            self.cmd_running_event.set()
            if self.coder.tui and self.coder.tui():
                self.coder.tui().refresh()
            # NEW: Queue processing integration
            if (
                self.prompt_queue
                and cmd_name not in self._MANAGEMENT_COMMANDS
                and not self._processing_queue
            ):
                await self._process_queued_prompts(self.coder.args.preproc)
                self.coder.tui().refresh()

    def matching_commands(self, inp):
        words = inp.strip().split()
        if not words:
            return
        first_word = words[0]
        rest_inp = inp[len(words[0]) :].strip()
        all_commands = self.get_commands()
        matching_commands = [cmd for cmd in all_commands if cmd.startswith(first_word)]
        return matching_commands, first_word, rest_inp

    async def run(self, inp, coder=None, **kwargs):
        if inp.startswith("/"):
            words = inp.strip().split()
            cmd_name = words[0][1:]
            rest_inp = inp[len(words[0]) :].strip()
            return await self.execute(cmd_name, rest_inp, coder=coder, **kwargs)

        if inp.startswith("!!!"):
            return await self.execute(
                "run", inp[3:], coder=coder, background=True, suppress_add=True
            )
        if inp.startswith("!!"):
            return await self.execute("run", inp[2:], coder=coder, suppress_add=True)
        if inp.startswith("!"):
            return await self.execute("run", inp[1:], coder=coder)
        res = self.matching_commands(inp)
        if res is None:
            return
        matching_commands, first_word, rest_inp = res
        if len(matching_commands) == 1:
            command = matching_commands[0][1:]
            return await self.execute(command, rest_inp, coder=coder, **kwargs)
        elif first_word in matching_commands:
            command = first_word[1:]
            return await self.execute(command, rest_inp, coder=coder, **kwargs)
        elif len(matching_commands) > 1:
            self.io.tool_error(f"Ambiguous command: {', '.join(matching_commands)}")
        else:
            self.io.tool_error(f"Invalid command: {first_word}")

    def get_help_md(self):
        """Show help about all commands in markdown"""
        res = "\n|Command|Description|\n|:------|:----------|\n"
        commands = sorted(self.get_commands())
        for cmd in commands:
            cmd_name = cmd[1:]
            command_class = CommandRegistry.get_command(cmd_name)
            if command_class:
                description = command_class.DESCRIPTION
                res += f"| **{cmd}** | {description} |\n"
            else:
                res += f"| **{cmd}** | |\n"
        res += "\n"
        return res

    def _get_session_directory(self):
        """Get the session storage directory, creating it if needed"""
        session_dir = handle_core_files(Path(self.coder.root) / ".cecli" / "sessions")
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir

    def _get_session_file_path(self, session_name):
        """Get the full path for a session file"""
        session_dir = self._get_session_directory()
        safe_name = re.sub("[^a-zA-Z0-9_.-]", "_", session_name)
        ext = "" if safe_name[-5:] == ".json" else ".json"
        return session_dir / f"{safe_name}{ext}"


def get_help_md():
    md = Commands(None, None).get_help_md()
    return md


def main():
    md = get_help_md()
    print(md)


if __name__ == "__main__":
    status = main()
    sys.exit(status)
