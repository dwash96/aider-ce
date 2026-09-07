"""Queue command for CLI-33: adds a prompt to the processing queue."""

from typing import List

from cecli.commands.utils.base_command import BaseCommand
from cecli.commands.utils.helpers import format_command_result


class QueueCommand(BaseCommand):
    NORM_NAME = "queue"
    DESCRIPTION = "Queue a prompt for processing after current tasks complete"

    @classmethod
    async def execute(cls, io, coder, args, **kwargs):
        """Execute the queue command with given parameters.

        Args:
            io: InputOutput instance
            coder: Coder instance (may be None for some commands)
            args: Command arguments as string (the prompt text to queue)
            **kwargs: Additional context

        Returns:
            Formatted result string
        """
        # Sad path: coder.commands is None
        if not coder.commands:
            return format_command_result(
                io, cls.NORM_NAME, "", error="Command system not available. Cannot queue prompts."
            )

        # Sad path: no args (empty prompt text)
        if not args or not args.strip():
            return format_command_result(
                io,
                cls.NORM_NAME,
                "",
                error=(
                    "Usage: /queue <prompt text> - Add a prompt to the queue "
                    "for processing after current tasks complete."
                ),
            )

        prompt_text = args.strip()

        # Sad path: prompt exceeds 10000 characters
        if len(prompt_text) > 10000:
            return format_command_result(
                io,
                cls.NORM_NAME,
                "",
                error=f"Prompt exceeds maximum length of 10000 characters "
                f"(got {len(prompt_text)}).",
            )

        # Happy path: enqueue the prompt
        try:
            item = coder.commands._enqueue_prompt(prompt_text)
            position = len(coder.commands.prompt_queue)
            io.tool_output(f"Prompt queued at position {position} (id: {item['id']})")
            return f"Successfully executed {cls.NORM_NAME}."
        except ValueError as e:
            return format_command_result(io, cls.NORM_NAME, "", error=str(e))
        except RuntimeError as e:
            return format_command_result(io, cls.NORM_NAME, "", error=str(e))

    @classmethod
    def get_completions(cls, io, coder, args) -> List[str]:
        """Get completion options for queue command."""
        return []

    @classmethod
    def get_help(cls) -> str:
        """Get help text for the queue command."""
        help_text = super().get_help()
        help_text += "\nUsage:\n"
        help_text += "  /queue <prompt text>  # Queue a prompt for later processing\n"
        help_text += "\nDescription:\n"
        help_text += "  Adds a prompt to an in-memory FIFO queue. Queued prompts are\n"
        help_text += "  processed sequentially after the current command completes.\n"
        help_text += "\nConstraints:\n"
        help_text += "  - Maximum prompt length: 10,000 characters\n"
        help_text += "  - Maximum queue size: 100 items\n"
        help_text += "  - Queue is in-memory only (lost on session restart)\n"
        help_text += "\nExamples:\n"
        help_text += "  /queue Review the changes in src/main.py\n"
        help_text += "  /queue Write unit tests for the new feature\n"
        help_text += "\nSee also: /list-queue, /remove-queue\n"
        return help_text
