---
nav_order: 55
parent: Usage
description: Developer documentation for the prompt queue management feature
---

# Prompt Queue Management (Developer Guide)

This document provides comprehensive developer documentation for the prompt queue management feature (`CLI-33`). The feature allows users to queue prompts for deferred processing, view the queue, and selectively remove items.

## Architecture Overview

### Queue Location and Data Structure

The prompt queue is implemented as an instance variable on the `Commands` class in `cecli/commands/core.py`:

```python
# In Commands.__init__()
self.prompt_queue = []  # List[Dict[str, Union[str, float]]]
self._queue_counter = 0
self._queue_lock = asyncio.Lock()
self._processing_queue = False

# Commands that should NOT trigger auto-processing of the queue
self._MANAGEMENT_COMMANDS = {"queue", "list-queue", "remove-queue"}
```

Each queue item is a dictionary with the following structure:

```python
{
    "id": str,           # Unique identifier (incrementing counter)
    "text": str,         # The prompt text
    "timestamp": float   # Unix timestamp when enqueued
}
```

### Lifecycle

- **Session-bound**: The queue is tied to the user's CLI session and does not persist across restarts
- **In-memory only**: Stored as a Python list on the `Commands` instance
- **FIFO ordering**: Prompts are processed in first-in-first-out order
- **Auto-processing**: Triggered after the current command completes and the system is idle

### Thread Safety

The implementation uses a single-threaded async event loop architecture:

- **`asyncio.Lock` (`_queue_lock`)**: Protects all read and write operations on `prompt_queue`
- **Lock acquisition pattern**: `async with self._queue_lock:` for all queue modifications
- **CPython GIL + async model**: Makes list operations naturally safe within the async loop
- **No multi-threading**: The lock is a precaution for future concurrent access patterns

## Commands Class Queue Management Methods

### `_enqueue_prompt(self, text: str) -> dict`

Adds a prompt to the end of the queue.

**Parameters:**
- `text`: The prompt text to enqueue

**Returns:**
- `dict` with keys: `id` (str), `text` (str), `timestamp` (float)

**Raises:**
- `ValueError`: If text is empty, None, or exceeds 10,000 characters
- `RuntimeError`: If the queue is at max capacity (100 items)

**Implementation:**
```python
async with self._queue_lock:
    if not text or not text.strip():
        raise ValueError("Cannot enqueue empty prompt")
    if len(text) > 10000:
        raise ValueError("Prompt exceeds maximum length of 10000 characters")
    if len(self.prompt_queue) >= 100:
        raise RuntimeError("Queue is full (max 100 items)")

    self._queue_counter += 1
    item = {
        "id": str(self._queue_counter),
        "text": text,
        "timestamp": time.time(),
    }
    self.prompt_queue.append(item)
    return item
```

### `_dequeue_prompt(self) -> dict | None`

Removes and returns the first item from the queue (FIFO).

**Returns:**
- The dequeued item dict, or `None` if the queue is empty

### `_get_queue_length(self) -> int`

Returns the current number of items in the queue.

**Returns:**
- `int`: Current queue size

### `_remove_from_queue(self, index: int) -> dict | None`

Removes and returns the item at the given 0-based index.

**Parameters:**
- `index`: 0-based index of the item to remove

**Returns:**
- The removed item dict, or `None` if the index is out of bounds

### `_clear_queue(self) -> list`

Removes all items from the queue and returns them.

**Returns:**
- List of all items that were in the queue

### `_process_queued_prompts(self)`

Internal method that processes all prompts currently in the queue sequentially. Called from the `finally` block of `Commands.execute()` after `cmd_running_event.set()`.

**Processing Logic:**
1. Sets `self._processing_queue = True` (guard against re-entrant processing)
2. While queue is non-empty:
   a. Dequeues next item
   b. Logs: `"Processing queued prompt (id: {id})..."`
   c. Calls `await self.run(item["text"])`
   d. Catches `SwitchCoderSignal` / `ReloadProgramSignal` → re-raises
   e. Catches generic `Exception` → logs error, continues to next item
3. Sets `self._processing_queue = False`

## Queue Processing Integration

### Integration Point

The queue processing is triggered in the `finally` block of `Commands.execute()`:

```python
finally:
    self.cmd_running_event.set()  # System is now idle
    if self.coder.tui and self.coder.tui():
        self.coder.tui().refresh()
    # Queue processing integration
    if (
        self.prompt_queue
        and cmd_name not in self._MANAGEMENT_COMMANDS
        and not self._processing_queue
    ):
        await self._process_queued_prompts()
```

### Guard Conditions

Queue processing only occurs when ALL of the following are true:
1. `self.prompt_queue` is non-empty
2. The command that just completed is NOT a management command (`queue`, `list-queue`, `remove-queue`)
3. Not already processing the queue (`_processing_queue` flag is False)

### Management Command Non-Interference

Management commands (`/queue`, `/list-queue`, `/remove-queue`) are designed to execute immediately without interrupting ongoing prompt processing:

- They do NOT clear `cmd_running_event`
- They do NOT trigger auto-processing of queued items
- Their execution is isolated so the current prompt continues uninterrupted
- In `Commands.run()`, management commands starting with `/` are intercepted and executed immediately via `self.execute()`

### Error Handling

- **Signal propagation**: `SwitchCoderSignal` and `ReloadProgramSignal` from queued prompts are re-raised (not swallowed)
- **Generic exceptions**: Caught, logged via `io.tool_error()`, and processing continues to the next item
- **One bad prompt doesn't block the rest**: Error resilience is built into the processing loop

## Command Registration Pattern

Each queue command is implemented in a separate file following the `BaseCommand` pattern:

### File Structure

```
cecli/commands/
├── queue.py           # QueueCommand
├── list_queue.py      # ListQueueCommand
├── remove_queue.py    # RemoveQueueCommand
└── __init__.py        # Registration
```

### Import Pattern (in `cecli/commands/__init__.py`)

```python
from .queue import QueueCommand
from .list_queue import ListQueueCommand
from .remove_queue import RemoveQueueCommand
```

### Registration

```python
CommandRegistry.register(QueueCommand)
CommandRegistry.register(ListQueueCommand)
CommandRegistry.register(RemoveQueueCommand)
```

### Module Exports (`__all__`)

```python
__all__ = [
    # ... other commands ...
    "QueueCommand",
    "ListQueueCommand",
    "RemoveQueueCommand",
]
```

## BaseCommand Implementation for Queue Commands

All three commands follow the `BaseCommand` interface:

### Required Attributes

- `NORM_NAME`: Normalized command name (e.g., `"queue"`, `"list-queue"`, `"remove-queue"`)
- `DESCRIPTION`: Human-readable description for help output

### Required Methods

- `async execute(cls, io, coder, args, **kwargs)`: Main command logic
- `get_help(cls) -> str`: Returns usage and examples
- `get_completions(cls, io, coder, args) -> List[str]`: Tab completion (only `RemoveQueueCommand`)

### QueueCommand

```python
class QueueCommand(BaseCommand):
    NORM_NAME = "queue"
    DESCRIPTION = "Queue a prompt for processing after current tasks complete"
    
    async def execute(cls, io, coder, args, **kwargs):
        # Validates args, calls coder.commands._enqueue_prompt()
        # Returns confirmation with queue position
    
    def get_help(cls) -> str:
        # Returns usage and examples
```

### ListQueueCommand

```python
class ListQueueCommand(BaseCommand):
    NORM_NAME = "list-queue"
    DESCRIPTION = "List all prompts currently in the queue"
    
    async def execute(cls, io, coder, args, **kwargs):
        # Accesses queue, displays numbered list, handles empty
    
    def get_help(cls) -> str:
        # Returns usage and examples
```

### RemoveQueueCommand

```python
class RemoveQueueCommand(BaseCommand):
    NORM_NAME = "remove-queue"
    DESCRIPTION = "Remove a prompt from the queue by index, or '*' to clear all"
    
    async def execute(cls, io, coder, args, **kwargs):
        # Handles '*' wildcard, numbered index, interactive mode
    
    def get_completions(cls, io, coder, args) -> List[str]:
        # Returns index numbers + wildcard based on queue length
    
    def get_help(cls) -> str:
        # Returns usage and examples
```

## Error Handling Patterns

### ValueError

Raised for:
- Empty prompts or None values in `/queue`
- Prompts exceeding 10,000 character limit

### IndexError

Raised for:
- Out-of-bounds indices in `/remove-queue`

### Usage Errors

- Non-integer indices show user-friendly messages
- Invalid arguments show usage/help

### Null Checks

All commands handle `coder.commands is None` gracefully with error messages instead of crashing.

## Queue Limits

| Limit | Value | Behavior |
|-------|-------|----------|
| Max Queue Size | 100 items | Rejects new prompts with warning when full |
| Max Prompt Length | 10,000 characters | Rejects prompts exceeding this limit |
| In-Memory Only | Session-bound | Lost on CLI restart |

## Configuration (Future)

The following configuration options are planned but not yet implemented:

- `--max-queue-size` / `max_queue_size` (default: 100, range: 1-1000)
- `--max-prompt-length` / `max_prompt_length` (default: 10000, range: 100-50000)
- `--no-queue-auto-process` to disable auto-processing
- `--queue-verbose` for verbose queue logging
- Environment variables: `CECLI_MAX_QUEUE_SIZE`, `CECLI_MAX_PROMPT_LENGTH`

## Testing

See `cecli/tests/test_queue_commands.py` for:
- Unit tests for queue logic in `core.py`
- Integration tests for command classes
- E2E tests for full queue lifecycle
- Regression tests for existing command integrity
- Test fixtures and data builders

## Related Files

- `cecli/commands/core.py` - Queue data structure and processing logic
- `cecli/commands/queue.py` - `/queue` command implementation
- `cecli/commands/list_queue.py` - `/list-queue` command implementation
- `cecli/commands/remove_queue.py` - `/remove-queue` command implementation
- `cecli/commands/__init__.py` - Command registration
- `cecli/commands/utils/base_command.py` - BaseCommand interface
- `cecli/tests/test_queue_commands.py` - Test suite
- `cecli/website/docs/usage/commands.md` - User-facing command reference
- `cecli/website/docs/troubleshooting.md` - Troubleshooting guide
- `CHANGELOG.md` - Release notes