"""List-queue command for CLI-33: displays all prompts in the processing queue."""

import datetime
from typing import List

from cecli.commands.utils.base_command import BaseCommand
from cecli.commands.utils.helpers import format_command_result


class ListQueueCommand(BaseCommand):
    NORM_NAME = "list-queue"
    DESCRIPTION = "List all prompts currently in the queue"

    @classmethod
    async def execute(cls, io, coder, args, **kwargs):
        """Execute the list-queue command with given parameters.

        Args:
            io: InputOutput instance
            coder: Coder instance (may be None for some commands)
            args: Command arguments (unused for list-queue)
            **kwargs: Additional context

        Returns:
            Formatted result string
        """
        # Sad path: coder.commands is None
        if not coder.commands:
            return format_command_result(
                io, cls.NORM_NAME, "", error="Command system not available. Cannot list queue."
            )

        queue = coder.commands.prompt_queue

        # Sad path: empty queue
        if not queue:
            io.tool_output("Queue is empty.")
            return f"Successfully executed {cls.NORM_NAME}."

        # Happy path: display numbered list
        lines = []
        for i, item in enumerate(queue, start=1):
            text = item["text"]
            display_text = text[:80] + "..." if len(text) > 80 else text
            ts = datetime.datetime.fromtimestamp(item["timestamp"]).strftime("%H:%M:%S")
            lines.append(f"[{i}] {display_text} ({ts})")

        io.tool_output("\n".join(lines))
        return f"Successfully executed {cls.NORM_NAME}."

    @classmethod
    def get_completions(cls, io, coder, args) -> List[str]:
        """Get completion options for list-queue command."""
        return []

    @classmethod
    def get_help(cls) -> str:
        """Get help text for the list-queue command."""
        help_text = super().get_help()
        help_text += "\nUsage:\n"
        help_text += "  /list-queue  # Display all queued prompts\n"
        help_text += "\nDescription:\n"
        help_text += "  Displays a numbered list of all prompts currently in the queue,\n"
        help_text += "  showing each prompt's position, text (truncated to 80 chars),\n"
        help_text += "  and the time it was queued.\n"
        help_text += "\nExamples:\n"
        help_text += "  /list-queue  # Shows all queued prompts\n"
        help_text += "\nSee also: /queue, /remove-queue\n"
        return help_text
