---
nav_order: 40
has_children: true
description: Information on all of cecli's settings and how to use them.
---

# Configuration

Cecli has many options which can be set with command line switches. Most options can also be set in an `.cecli.conf.yml` file which can be placed in your home directory or at the root of your git repo. Or by setting environment variables like `CECLI_xxx` either in your shell or a `.env` file.

Here are 4 equivalent ways of setting an option. 

With a command line switch:

```
$ cecli --tui
```

Using a `.cecli.conf.yml` file:

```yaml
tui: true
```

By setting an environment variable:

```
export CECLI_TUI=true
```

Using an `.env` file:

```
CECLI_TUI=true
```

## Default File Locations

Cecli also checks several default locations inside `~/.cecli/` for configuration, environment variables, and agent resources. These are always included with lower precedence than project-level equivalents, so a setting in a project `.cecli.conf.yml` or `.env` file will override the `~/.cecli/` default. The agent resource registries below each also look for a **local** default under an agent's working root, which takes precedence over the global default.

### `~/.cecli/conf.yml`

A YAML configuration file read after all other config file sources, making it the lowest-precedence config option. Useful for setting machine-wide defaults that individual projects can override. See the [configuration section](config/conf.html) above for supported options.

### `~/.cecli/.env`

An environment file loaded before any other `.env` file, so project-level `.env` files can override its values. A convenient place to store API keys or other environment variables used across multiple projects.

### `~/.cecli/skills/`

`Local: .cecli/skills/`

A directory containing skill packages (each a sub-directory with a `SKILL.md` file).

### `~/.cecli/subagents/`

`Local: .cecli/subagents/`

A directory containing sub-agent definition files (`.md` files with YAML front matter).

### `~/.cecli/tools/`

`Local: .cecli/tools/`

A directory containing custom tool packages (`.py` files exposing a `Tool` class).

> **Tip:**
> See the [API key configuration docs](config/api-keys.html) for information on how to configure and store your API keys.

## Prompt Queue Configuration

- `max_queue_size`: Maximum number of prompts that can be queued in a single session (default: 100, range: 1-1000). Environment variable: `CECLI_MAX_QUEUE_SIZE`.
- `max_prompt_length`: Maximum length of a single queued prompt (default: 10,000, range: 100-50,000). Environment variable: `CECLI_MAX_PROMPT_LENGTH`.

## Prompt Queue Configuration

- `max_queue_size`: Maximum number of prompts that can be queued in a single session (default: 100, range: 1-1000). Environment variable: `CECLI_MAX_QUEUE_SIZE`.
- `max_prompt_length`: Maximum length of a single queued prompt (default: 10,000, range: 100-50,000). Environment variable: `CECLI_MAX_PROMPT_LENGTH`.

{% include keys.md %}
