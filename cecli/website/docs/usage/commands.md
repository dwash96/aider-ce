---
parent: Usage
nav_order: 50
description: Control cecli with in-chat commands like /add, /model, etc.
---

# Slash Commands

Cecli supports commands from within the chat, which all start with `/`.

|Command|Description|
|:------|:----------|
| **/add** | Add files to the chat so cecli can edit them or review them in detail |
| **/architect** | Enter architect/editor mode using 2 different models. If no prompt provided, switches to architect/editor mode. |
| **/ask** | Ask questions about the code base without editing any files. If no prompt provided, switches to ask mode. |
| **/chat-mode** | Switch to a new chat mode |
| **/clear** | Clear the chat history |
| **/code** | Ask for changes to your code. If no prompt provided, switches to code mode. |
| **/commit** | Commit edits to the repo made outside the chat (commit message optional) |
| **/context** | Enter context mode to see surrounding code context. If no prompt provided, switches to context mode. |
| **/copy** | Copy the last assistant message to the clipboard |
| **/copy-context** | Copy the current chat context as markdown, suitable to paste into a web UI |
| **/diff** | Display the diff of changes since the last message |
| **/drop** | Remove files from the chat session to free up context space |
| **/edit** | Alias for /editor: Open an editor to write a prompt |
| **/editor** | Open an editor to write a prompt |
| **/editor-model** | Switch the Editor Model to a new LLM |
| **/exit** | Exit the application |
| **/git** | Run a git command (output excluded from chat) |
| **/help** | Ask questions about cecli |
| **/history-search** | Fuzzy search your command history and paste the selected command into the chat. |
| **/lint** | Lint and fix in-chat files or all dirty files if none in chat |
| **/load** | Load and execute commands from a file |
| **/load-mcp** | Load a MCP server by name |
| **/ls** | List all known files and indicate which are included in the chat session |
| **/map** | Print out the current repository map |
| **/map-refresh** | Force a refresh of the repository map |
| **/model** | Switch the Main Model to a new LLM |
| **/models** | Search the list of available models |
| **/multiline-mode** | Toggle multiline mode (swaps behavior of Enter and Meta+Enter) |
| **/paste** | Paste image/text from the clipboard into the chat.        Optionally provide a name for the image. |
| **/quit** | Exit the application |
| **/read-only** | Add files to the chat that are for reference only, or turn added files to read-only |
| **/reasoning-effort** | Set the reasoning effort level (values: number or low/medium/high depending on model) |
| **/report** | Report a problem by opening a GitHub Issue |
| **/remove-mcp** | Remove a MCP server by name |
| **/reset** | Drop all files and clear the chat history |
| **/run** | Run a shell command and optionally add the output to the chat (alias: !) |
| **/save** | Save commands to a file that can reconstruct the current chat session's files |
| **/settings** | Print out the current settings |
| **/switch-agent** | Switch to a specific agent by name |
| **/test** | Run a shell command and add the output to the chat on non-zero exit code |
| **/think-tokens** | Set the thinking token budget, eg: 8096, 8k, 10.5k, 0.5M, or 0 to disable. |
| **/tokens** | Report on the number of tokens used by the current chat context |
| **/undo** | Undo the last git commit if it was done by cecli |
| **/voice** | Record and transcribe voice input |
| **/weak-model** | Switch the Weak Model to a new LLM |
| **/web** | Scrape a webpage, convert to markdown and send in a message |

> **Tip:** You can easily re-send commands or messages. Use the up arrow ⬆ to scroll back or CONTROL-R to search your message history.

## Prompt Queue Management

| Command | Description |
| :--- | :--- |
| **/queue** | Queue a prompt for processing after current tasks complete |
| **/list-queue** | List all prompts currently in the queue |
| **/remove-queue** | Remove a prompt from the queue by index, or '*' to clear all |

{: .tip }

## Prompt Queue Management Commands

The prompt queue management feature (`CLI-33`) adds three new commands for managing a first-in-first-out (FIFO) queue of prompts.

### Queue Commands

| Command | Description |
|---------|-------------|
| **/queue** | Queue a prompt for processing after current tasks complete |
| **/list-queue** | List all prompts currently in the queue |
| **/remove-queue** | Remove a prompt from the queue by index, or '*' to clear all |

#### `/queue` Command

**Usage:** `/queue <prompt text>`

**Description:** Adds a prompt to the queue for processing after the current command completes.

**Arguments:**
- `prompt text`: Required. The prompt text to queue (maximum 10,000 characters)

**Returns:** Confirmation message with the queue position number

**Examples:**
```bash
/queue "refactor database layer"
/queue "add unit tests for user service"
```

**Implementation Details:**
- `NORM_NAME = "queue"`
- `DESCRIPTION = "Queue a prompt for processing after current tasks complete"`
- `execute()`: Validates input, calls `coder.commands._enqueue_prompt()`, returns position confirmation
- `get_help()`: Returns usage and examples

#### `/list-queue` Command

**Usage:** `/list-queue`

**Description:** Displays all prompts currently in the queue with their position numbers and timestamps.

**Arguments:** None

**Returns:** Numbered list of queued prompts (`[index] text (timestamp)`) or "Queue is empty" message

**Examples:**
```bash
/list-queue
# Output: [1] refactor database layer (2026-08-01 10:30:00)
#         [2] add unit tests for user service (2026-08-01 10:30:05)
```

**Implementation Details:**
- `NORM_NAME = "list-queue"`
- `DESCRIPTION = "List all prompts currently in the queue"`
- `execute()`: Accesses queue, formats output with timestamps and truncated text, handles empty queue
- `get_help()`: Returns usage and examples

#### `/remove-queue` Command

**Usage:** `/remove-queue <index>`, `/remove-queue *`, or `/remove-queue` (interactive)

**Description:** Removes a specific prompt from the queue by index, clears the entire queue with `*`, or provides interactive selection when called with no arguments.

**Arguments:**
- `index`: Optional. 0-based index of the prompt to remove, or `*` wildcard to clear all

**Returns:** Confirmation of removal and updated queue state

**Examples:**
```bash
/remove-queue 2          # Remove prompt at index 2
/remove-queue *          # Clear entire queue
/remove-queue            # Interactive selection mode
```

**Implementation Details:**
- `NORM_NAME = "remove-queue"`
- `DESCRIPTION = "Remove a prompt from the queue by index, or '*' to clear all"`
- `execute()`: Handles `*` wildcard, numbered index, and interactive mode
- `get_help()`: Returns usage and examples
- `get_completions()`: Returns index numbers + `*` for tab completion

#### Error Handling

All queue commands follow consistent error handling patterns:
- `ValueError`: Raised for empty prompts or None values in `/queue`
- `IndexError`: Raised for out-of-bounds indices in `/remove-queue`
- Usage errors: Non-integer indices, invalid arguments show user-friendly messages
- Null checks: Handle `coder.commands` is None gracefully with error messages

#### Thread Safety

The queue uses an `asyncio.Lock` (`_queue_lock`) to protect all read and write operations, ensuring atomic updates in the single-threaded async event loop.
You can easily re-send commands or messages.
Use the up arrow ⬆ to scroll back
or CONTROL-R to search your message history.

## Non-TUI Related Notes

### Multi-line Chat Messages

You can send long, multi-line messages in the chat in a few ways:
  - Paste a multi-line message directly into the chat.
  - Enter `{` alone on the first line to start a multiline message and `}` alone on the last line to end it.
    - Or, start with `{tag` (where "tag" is any sequence of letters/numbers) and end with `tag}`. This is useful when you need to include closing braces `}` in your message.
  - Use Meta-ENTER to start a new line without sending the message (Esc+ENTER in some environments).
  - Use `/paste` to paste text from the clipboard into the chat.
  - Use the `/editor` command (or press `Ctrl-X Ctrl-E` if your terminal allows) to open your editor to create the next chat message. See [editor configuration docs](../config/editor.html) for more info.
  - Use multiline-mode, which swaps the function of Meta-Enter and Enter, so that Enter inserts a newline, and Meta-Enter submits your command. To enable multiline mode:
    - Use the `/multiline-mode` command to toggle it during a session.
    - Use the `--multiline` switch.
  
Example with a tag:
```
{python
def hello():
    print("Hello}")  # Note: contains a brace
python}
```

### Key Bindings

The interactive prompt is built with [prompt-toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) which provides emacs and vi keybindings. 

#### Emacs

- `Up Arrow` : Move up one line in the current message.
- `Down Arrow` : Move down one line in the current message.
- `Ctrl-Up` : Scroll back through previously sent messages.
- `Ctrl-Down` : Scroll forward through previously sent messages.
- `Ctrl-A` : Move cursor to the start of the line.
- `Ctrl-B` : Move cursor back one character.
- `Ctrl-D` : Delete the character under the cursor.
- `Ctrl-E` : Move cursor to the end of the line.
- `Ctrl-F` : Move cursor forward one character.
- `Ctrl-K` : Delete from the cursor to the end of the line.
- `Ctrl-L` : Clear the screen.
- `Ctrl-N` : Move down to the next history entry.
- `Ctrl-P` : Move up to the previous history entry.
- `Ctrl-R` : Reverse search in command history.
- `Ctrl-X Ctrl-E` : Open the current input in an external editor
- `Ctrl-Y` : Paste (yank) text that was previously cut.

#### Vi

To use vi/vim keybindings, run cecli with the `--vim` switch.

- `Up Arrow` : Move up one line in the current message.
- `Down Arrow` : Move down one line in the current message.
- `Ctrl-Up` : Scroll back through previously sent messages.
- `Ctrl-Down` : Scroll forward through previously sent messages.
- `Esc` : Switch to command mode.
- `i` : Switch to insert mode.
- `a` : Move cursor one character to the right and switch to insert mode.
- `A` : Move cursor to the end of the line and switch to insert mode.
- `I` : Move cursor to the beginning of the line and switch to insert mode.
- `h` : Move cursor one character to the left.
- `j` : Move cursor down one line.
- `k` : Move cursor up one line.
- `l` : Move cursor one character to the right.
- `w` : Move cursor forward one word.
- `b` : Move cursor backward one word.
- `0` : Move cursor to the beginning of the line.
- `$` : Move cursor to the end of the line.
- `x` : Delete the character under the cursor.
- `dd` : Delete the current line.
- `u` : Undo the last change.
- `Ctrl-R` : Redo the last undone change.
