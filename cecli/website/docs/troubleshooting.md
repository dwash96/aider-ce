---
nav_order: 60
has_children: true
description: How to troubleshoot problems with cecli and get help.
---

# Troubleshooting

Below are some approaches for troubleshooting problems with cecli:

- Reproduce the problem with the smallest possible command and set of files.
- Check the terminal output for the first error, rather than only the final message.
- Confirm that cecli and its dependencies are up to date, then retry if the problem may be version-related.
- Try `--verbose` or `--debug` when more diagnostic information is needed, and save the resulting logs for an issue report.
- If the problem involves a model or provider, record the model name, provider, and relevant configuration without sharing credentials.

## Create logs for an issue report

When asking for help in [Discord](https://discord.gg/g4bF53fSWF) or opening a [GitHub issue](https://github.com/cecli-dev/cecli/issues), include the relevant command, cecli version, operating system, model/provider, and a log captured while reproducing the problem. Do not include API keys, tokens, passwords, or other sensitive information.

Use `--verbose` and `--debug` for additional diagnostic output:

```console
cecli --verbose --debug
```

If the problem involves an LLM request, `--debug` may also create request logs under `.cecli/logs/`. Review and redact those files before sharing them, since request logs can contain prompts or other private project content. Attach the redacted log files to your GitHub issue or upload them in Discord, and briefly explain what you expected to happen and what happened instead.

> **Tip:**
> Use `/help <question>` to [ask for help about using cecli](troubleshooting/support.html), customizing settings, using LLMs, etc.

## Queue Commands

- **Queue not processing**: If queued prompts don't execute, ensure the system is idle (no active command running). Use `/list-queue` to verify prompts are in the queue.
- **Prompt not queued**: If `/queue` fails, check that the prompt is not empty, does not exceed 10,000 characters, and the queue is not full (100 items).
- **Cannot remove from queue**: Ensure you are using a valid positive integer index. Use `/list-queue` to verify current queue contents and valid indices.
- **Queue seems corrupted**: The queue is in-memory and session-specific. Restarting the CLI session will clear the queue.

## Queue Commands

- **Queue not processing**: Ensure the system is idle (no active command running). Use `/list-queue` to verify prompts are in the queue.
- **Prompt not queued**: Check if the prompt is empty, exceeds the 10,000 character limit, or if the queue is full (100 items).
- **Cannot remove from queue**: Ensure the index is a valid positive integer. Use `/list-queue` to verify current queue contents and valid indices.
- **Queue seems corrupted**: Restart the `cecli` session to clear the in-memory queue.

{% include help.md %}
