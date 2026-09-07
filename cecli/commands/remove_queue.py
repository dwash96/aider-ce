"""Remove-queue command for CLI-33: removes prompts from the processing queue."""

from typing import List

from cecli.commands.utils.base_command import BaseCommand
from cecli.commands.utils.helpers import format_command_result


class RemoveQueueCommand(BaseCommand):
    NORM_NAME = "remove-queue"
    DESCRIPTION = "Remove a prompt from the queue by index, or '*' to clear all"

    @classmethod
    async def execute(cls, io, coder, args, **kwargs):
        """Execute the remove-queue command with given parameters.

        Args:
            io: InputOutput instance
            coder: Coder instance (may be None for some commands)
            args: Command arguments (index number, '*', or empty for interactive)
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
                error="Command system not available. Cannot remove from queue.",
            )

        # Sad path: empty queue
        if coder.commands._get_queue_length() == 0:
            return format_command_result(
                io, cls.NORM_NAME, "", error="Queue is empty. Nothing to remove."
            )

        # Handle wildcard: clear entire queue
        if args and args.strip() == "*":
            items = coder.commands._clear_queue()
            count = len(items)
            io.tool_output(f"Removed all {count} queued prompt(s).")
            return f"Successfully executed {cls.NORM_NAME}."

        # Handle specific index
        if args and args.strip():
            try:
                index = int(args.strip()) - 1  # Convert to 0-based
            except ValueError:
                return format_command_result(
                    io,
                    cls.NORM_NAME,
                    "",
                    error=f"Invalid index: '{args.strip()}'. Please provide a number or '*'.",
                )

            item = coder.commands._remove_from_queue(index)
            if item is None:
                queue_len = coder.commands._get_queue_length()
                return format_command_result(
                    io,
                    cls.NORM_NAME,
                    "",
                    error=f"Index {args.strip()} is out of range. Queue has {queue_len} item(s).",
                )

            io.tool_output(f"Removed: {item['text'][:80]}")
            return f"Successfully executed {cls.NORM_NAME}."

        # Interactive mode: no args provided
        queue = coder.commands.prompt_queue
        io.tool_output("Queued prompts:")
        for i, item in enumerate(queue, 1):
            text = item["text"][:80]
            if len(item["text"]) > 80:
                text += "..."
            io.tool_output(f"  [{i}] {text}")

        io.tool_output("\nEnter index to remove, '*' to clear all, or press Enter to cancel:")
        # In non-interactive mode, just show usage
        return format_command_result(
            io, cls.NORM_NAME, "Usage: /remove-queue <index> | /remove-queue *"
        )

    @classmethod
    def get_completions(cls, io, coder, args) -> List[str]:
        """Get completion options for remove-queue command."""
        if not coder.commands:
            return []

        queue_len = coder.commands._get_queue_length()
        completions = [str(i) for i in range(1, queue_len + 1)]
        completions.append("*")
        return completions

    @classmethod
    def get_help(cls) -> str:
        """Get help text for the remove-queue command."""
        help_text = super().get_help()
        help_text += "\nUsage:\n"
        help_text += "  /remove-queue <index>  # Remove prompt at given index\n"
        help_text += "  /remove-queue *        # Clear the entire queue\n"
        help_text += "  /remove-queue          # Interactive mode (shows list, prompts for index)\n"
        help_text += "\nDescription:\n"
        help_text += "  Removes a prompt from the in-memory queue by its index number.\n"
        help_text += "  Use '*' to clear all queued prompts at once.\n"
        help_text += (
            "  Without arguments, displays the queue and prompts for interactive selection.\n"
        )
        help_text += "\nExamples:\n"
        help_text += "  /remove-queue 1   # Remove the first queued prompt\n"
        help_text += "  /remove-queue *   # Clear the entire queue\n"
        help_text += "  /remove-queue     # Interactive mode\n"
        help_text += "\nSee also: /queue, /list-queue\n"
        return help_text
