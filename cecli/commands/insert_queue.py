"""Insert-queue command for CLI-33: inserts a prompt at a specific queue position."""

from typing import List

from cecli.commands.utils.base_command import BaseCommand
from cecli.commands.utils.helpers import format_command_result


class InsertQueueCommand(BaseCommand):
    NORM_NAME = "insert-queue"
    DESCRIPTION = "Insert a prompt into the queue at a specific position"

    @classmethod
    async def execute(cls, io, coder, args, **kwargs):
        """Execute the insert-queue command with given parameters.

        Args:
            io: InputOutput instance
            coder: Coder instance (may be None for some commands)
            args: Command arguments as string ("<index> <prompt text>")
            **kwargs: Additional context

        Returns:
            Formatted result string
        """
        # Sad path: coder.commands is None
        if not coder.commands:
            return format_command_result(
                io,
                cls.NORM_NAME,
                "",
                error="Command system not available. Cannot insert into queue.",
            )

        # Sad path: missing index or prompt text
        parts = (args or "").strip().split(maxsplit=1)
        if len(parts) != 2:
            return format_command_result(
                io,
                cls.NORM_NAME,
                "",
                error="Usage: /insert-queue <index> <prompt text>",
            )

        # Sad path: non-integer index
        try:
            index = int(parts[0])
        except ValueError:
            return format_command_result(
                io,
                cls.NORM_NAME,
                "",
                error=f"Invalid index: '{parts[0]}'. Please provide a number.",
            )

        prompt_text = parts[1].strip()

        # Happy path: insert the prompt
        try:
            item = coder.commands._insert_prompt(prompt_text, index)
            io.tool_output(f"Prompt inserted at position {index + 1} (id: {item['id']})")
            return f"Successfully executed {cls.NORM_NAME}."
        except ValueError as e:
            return format_command_result(io, cls.NORM_NAME, "", error=str(e))
        except RuntimeError as e:
            return format_command_result(io, cls.NORM_NAME, "", error=str(e))

    @classmethod
    def get_completions(cls, io, coder, args) -> List[str]:
        """Get completion options for insert-queue command."""
        return []

    @classmethod
    def get_help(cls) -> str:
        """Get help text for the insert-queue command."""
        help_text = super().get_help()
        help_text += "\nUsage:\n"
        help_text += "  /insert-queue <index> <prompt text>  # Insert at a specific position\n"
        help_text += "\nDescription:\n"
        help_text += "  Inserts a prompt into the queue at the given 1-based position.\n"
        help_text += "  Existing items shift down. Index is clamped to the queue bounds.\n"
        help_text += "\nExamples:\n"
        help_text += "  /insert-queue 1 Review the changes in src/main.py\n"
        help_text += "  /insert-queue 3 Write unit tests for the new feature\n"
        help_text += "\nSee also: /queue, /list-queue, /remove-queue\n"
        return help_text
