#!/usr/bin/env python

import asyncio
import base64
import copy
import hashlib
import json
import locale
import math
import mimetypes
import os
import platform
import re
import sys
import threading
import time
import traceback
import weakref
from collections import defaultdict
from datetime import date, datetime

import xxhash

# Optional dependency: used to convert locale codes (eg ``en_US``)
# into human-readable language names (eg ``English``).
try:
    from babel import Locale  # type: ignore
except ImportError:  # Babel not installed – we will fall back to a small mapping
    Locale = None
from json.decoder import JSONDecodeError
from pathlib import Path
from typing import List
from urllib.parse import urlparse
from uuid import uuid4 as generate_unique_id

import cecli.prompts.utils.system as prompts
from cecli import __version__, models, urls, utils
from cecli.commands import Commands, SwitchCoderSignal
from cecli.decoding import safe_open
from cecli.exceptions import LiteLLMExceptions
from cecli.helpers import command_parser, command_queue, coroutines, nested, responses
from cecli.helpers.conversation import ConversationService, MessageTag
from cecli.helpers.file_system import FileSystemService
from cecli.helpers.io_proxy import IOProxy
from cecli.helpers.loop_detect import LoopDetectedError, LoopDetector
from cecli.helpers.memory_control import trim_memory
from cecli.helpers.observations.service import ObservationService
from cecli.helpers.profiler import TokenProfiler
from cecli.helpers.threading import ThreadSafeEvent
from cecli.history import ChatSummary
from cecli.hooks import HookIntegration
from cecli.io import ConfirmGroup, InputOutput
from cecli.linter import Linter
from cecli.llm import litellm
from cecli.mcp import LocalServer
from cecli.models import RETRY_TIMEOUT
from cecli.reasoning_tags import (
    REASONING_TAG,
    format_reasoning_content,
    remove_reasoning_content,
    replace_reasoning_tags,
)
from cecli.repo import ANY_GIT_ERROR, GitRepoProxy
from cecli.repomap import RepoMap
from cecli.report import update_error_prefix
from cecli.run_cmd import run_cmd_async
from cecli.sessions import SessionManager
from cecli.tools.utils.output import print_tool_response
from cecli.tools.utils.registry import ToolRegistry
from cecli.utils import copy_tool_call, format_tokens, is_image_file

from ..dump import dump  # noqa: F401
from ..prompts.utils.registry import PromptObject, PromptRegistry

GLOBAL_DATE = date.today().isoformat()


class UnknownEditFormat(ValueError):
    def __init__(self, edit_format, valid_formats):
        self.edit_format = edit_format
        self.valid_formats = valid_formats
        super().__init__(
            f"Unknown edit format {edit_format}. Valid formats are: {', '.join(valid_formats)}"
        )


class MissingAPIKeyError(ValueError):
    pass


class FinishReasonLength(Exception):
    pass


class EmptyResponseError(Exception):
    pass


def wrap_fence(name):
    return f"<{name}>", f"</{name}>"


all_fences = [
    ("`" * 3, "`" * 3),
    ("`" * 4, "`" * 4),  # LLMs ignore and revert to triple-backtick, causing #2879
    wrap_fence("source"),
    wrap_fence("code"),
    wrap_fence("pre"),
    wrap_fence("codeblock"),
    wrap_fence("sourcecode"),
]


class UsageMeta(type):
    """Metaclass that provides shared accumulator properties across all Coder subclasses.
    Every instance shares the same unified total token and cost amounts."""

    _total_cost = 0
    _total_tokens_sent = 0
    _total_tokens_received = 0
    _total_cached_tokens = 0

    @property
    def total_cost(cls):
        return UsageMeta._total_cost

    @total_cost.setter
    def total_cost(cls, value):
        UsageMeta._total_cost = value

    @property
    def total_tokens_sent(cls):
        return UsageMeta._total_tokens_sent

    @total_tokens_sent.setter
    def total_tokens_sent(cls, value):
        UsageMeta._total_tokens_sent = value

    @property
    def total_tokens_received(cls):
        return UsageMeta._total_tokens_received

    @total_tokens_received.setter
    def total_tokens_received(cls, value):
        UsageMeta._total_tokens_received = value

    @property
    def total_cached_tokens(cls):
        return UsageMeta._total_cached_tokens

    @total_cached_tokens.setter
    def total_cached_tokens(cls, value):
        UsageMeta._total_cached_tokens = value


class Coder(metaclass=UsageMeta):

    # Instance-level properties that delegate to the shared metaclass storage
    @property
    def total_cost(self):
        return type(self).total_cost

    @total_cost.setter
    def total_cost(self, value):
        type(self).total_cost = value

    @property
    def total_tokens_sent(self):
        return type(self).total_tokens_sent

    @total_tokens_sent.setter
    def total_tokens_sent(self, value):
        type(self).total_tokens_sent = value

    @property
    def total_tokens_received(self):
        return type(self).total_tokens_received

    @total_tokens_received.setter
    def total_tokens_received(self, value):
        type(self).total_tokens_received = value

    @property
    def total_cached_tokens(self):
        return type(self).total_cached_tokens

    @total_cached_tokens.setter
    def total_cached_tokens(self, value):
        type(self).total_cached_tokens = value

    abs_fnames = None
    abs_read_only_fnames = None
    abs_read_only_stubs_fnames = None
    abs_rules_fnames = None
    repo = None
    root = "."
    primary_root = None
    last_coder_commit_hash = None
    coder_edited_files = None
    last_asked_for_commit_time = 0
    repo_map = None
    functions = None
    num_exhausted_context_windows = 0
    num_malformed_responses = 0
    last_keyboard_interrupt = None
    num_reflections = 0
    max_reflections = 3
    num_tool_calls = 0
    max_tool_calls = 25
    turn_count = 0
    edit_format = None
    file_diffs = True
    hashlines = False
    yield_stream = False
    temperature = None
    auto_lint = True
    auto_test = False
    auto_memory = True
    _last_memory_invoke_time = 0.0
    test_cmd = None
    lint_outcome = None
    test_outcome = None
    multi_response_content = ""
    partial_response_content = ""
    partial_response_reasoning_content = ""
    partial_response_chunks = []
    partial_response_tool_calls = []
    partial_response_consolidated = None
    commit_before_message = []
    message_cost = 0.0
    message_tokens_sent = 0
    message_tokens_received = 0
    message_cached_tokens = 0
    message_cost_deferred = None
    add_cache_headers = False
    cache_warming_thread = None
    num_cache_warming_pings = 0
    suggest_shell_commands = True
    detect_urls = True
    ignore_mentions = None
    chat_language = None
    commit_language = None
    file_watcher = None
    mcp_manager = None
    run_one_completed = True
    compact_context_completed = True
    suppress_announcements_for_next_prompt = False
    tool_reflection = False
    last_user_message = ""
    uuid: str = ""
    parent_uuid: str = ""
    model_kwargs = {}
    cost_multiplier = 1
    stop_on_empty = True
    error_code = None
    _output_loop_detected = False
    _output_loop_message = ""

    # Task coordination state variables
    input_running = False
    output_running = False

    # Context management settings (for all modes)
    context_management_enabled = False  # Disabled by default except for agent mode
    large_file_token_threshold = (
        25000  # Files larger than this will be truncated when context management is enabled
    )

    ok_to_warm_cache = False

    # Weak reference to TUI app instance (when running in TUI mode)
    tui = None

    _prompt_cache = {}

    @classmethod
    async def create(
        self,
        main_model=None,
        edit_format=None,
        io=None,
        from_coder=None,
        summarize_from_coder=True,
        args=None,
        **kwargs,
    ):
        import cecli.coders as coders

        if not main_model:
            if from_coder:
                main_model = from_coder.main_model
            else:
                main_model = models.Model(models.DEFAULT_MODEL_NAME, io=io)

        if edit_format == "code":
            edit_format = main_model.edit_format
        elif edit_format is None:
            if from_coder:
                edit_format = from_coder.edit_format
            else:
                edit_format = main_model.edit_format

        if not io and from_coder:
            io = from_coder.io

        if from_coder:
            if not args:
                args = from_coder.args

            use_kwargs = dict(from_coder.original_kwargs)  # copy orig kwargs

            update = dict(
                fnames=list(from_coder.abs_fnames),
                read_only_fnames=list(from_coder.abs_read_only_fnames),  # Copy read-only files
                read_only_stubs_fnames=list(
                    from_coder.abs_read_only_stubs_fnames
                ),  # Copy read-only stubs
                rules_fnames=list(from_coder.abs_rules_fnames),  # Copy read-only stubs
                done_messages=[],
                cur_messages=[],
                coder_commit_hashes=from_coder.coder_commit_hashes,
                commands=from_coder.commands.clone(),
                ignore_mentions=from_coder.ignore_mentions,
                file_watcher=from_coder.file_watcher,
                mcp_manager=from_coder.mcp_manager,
                registered_tools=copy.deepcopy(from_coder.registered_tools),
                registered_servers=copy.deepcopy(from_coder.registered_servers),
                auto_memory=from_coder.auto_memory,
                uuid=from_coder.uuid,
                parent_uuid=from_coder.parent_uuid,
                repo=from_coder.repo,
                primary_root=from_coder.primary_root,
                summarizer=from_coder.summarizer,
            )
            use_kwargs.update(update)  # override to complete the switch
            use_kwargs.update(kwargs)  # override passed kwargs

            kwargs = use_kwargs
            from_coder.ok_to_warm_cache = False

            # Preserve the prompt queue across a model switch (same coder
            # identity) but NOT when spawning a distinct sub-agent (which is
            # assigned a fresh uuid in its kwargs). Model switches keep
            # from_coder.uuid; sub-agents override it with a new uuid.
            is_model_switch = from_coder.uuid == kwargs.get("uuid", "")
            if is_model_switch:
                kwargs.setdefault("prompt_queue", from_coder.prompt_queue)
                kwargs.setdefault("_queue_counter", from_coder._queue_counter)

        res = None
        if (
            getattr(main_model, "copy_paste_mode", False)
            and getattr(main_model, "copy_paste_transport", "api") == "clipboard"
        ):
            res = coders.CopyPasteCoder(main_model, io, args=args, **kwargs)

        if not res:
            coder_name = coders.EDIT_FORMAT_MAP.get(edit_format)
            if coder_name:
                coder_cls = getattr(coders, coder_name)
                res = coder_cls(main_model, io, args=args, **kwargs)

        if res is not None:
            if from_coder:
                # Preserve TUI ref in all child coders
                if from_coder.tui:
                    res.tui = from_coder.tui

                # Preserve prompt queue state across model switches (CLI-33).
                # The queue lives on the coder (see command_queue.py) so the
                # new coder instance must inherit the list and counter, while
                # the lock and processing flag are intentionally fresh.
                # Only model switches inherit the queue; sub-agents get their
                # own isolated queue (each agent has its own context/queue).
                if is_model_switch:
                    res.prompt_queue = from_coder.prompt_queue
                    res._queue_counter = from_coder._queue_counter
                res.tui = from_coder.tui

                # Sub-agents get a dedicated, independent MCP manager so they
                # can rebuild a custom tool list (their own LocalServer tools /
                # filters) and be disconnected independently from the parent.
                # The parent's server configs are copied into a fresh manager
                # whose connections are created on this loop; the Local server
                # is left to initialize_mcp_tools() so it is recreated from the
                # sub-agent's filters.
                if (
                    from_coder.mcp_manager
                    and res.uuid
                    and res.parent_uuid
                    and res.parent_uuid != res.uuid
                ):
                    res.mcp_manager = await from_coder.mcp_manager.spawn_child(
                        io=IOProxy.unwrap(res.io)
                    )

                if res.mcp_manager:
                    # When switching to a non-agent coder, disconnect the "Local" MCP server
                    # (which provides agent-only tools like tool calling and file editing)
                    # so it's not available in non-agent modes.
                    if not isinstance(res, coders.AgentCoder):
                        local_server = res.mcp_manager.get_server("Local")
                        if local_server and local_server.is_connected:
                            await res.mcp_manager.disconnect_server("Local")

            await res.initialize_mcp_tools()

            # Store only small/primitive kwargs to avoid retaining large object references.
            # Large objects (repo, mcp_manager, commands, summarizer, file_watcher, etc.)
            # are either overridden during clone() or accessible from instance attributes.
            _LARGE_KWARGS = {"repo", "mcp_manager", "commands", "summarizer", "file_watcher"}
            res.original_kwargs = {k: v for k, v in kwargs.items() if k not in _LARGE_KWARGS}
            return res

        valid_formats = list(coders.EDIT_FORMAT_MAP.keys())
        raise UnknownEditFormat(edit_format, valid_formats)

    async def clone(self, **kwargs):
        new_coder = await Coder.create(from_coder=self, **kwargs)
        return new_coder

    def __init__(
        self,
        main_model,
        io,
        args=None,
        repo=None,
        fnames=None,
        add_gitignore_files=False,
        read_only_fnames=None,
        read_only_stubs_fnames=None,
        rules_fnames=None,
        show_diffs=False,
        auto_commits=True,
        dirty_commits=True,
        dry_run=False,
        map_tokens=1024,
        verbose=False,
        stream=True,
        use_git=True,
        cur_messages=None,
        done_messages=None,
        auto_lint=True,
        auto_test=False,
        auto_memory=True,
        lint_cmds=None,
        test_cmd=None,
        coder_commit_hashes=None,
        map_mul_no_files=8,
        map_max_line_length=100,
        commands=None,
        summarizer=None,
        map_refresh="auto",
        cache_prompts=False,
        num_cache_warming_pings=0,
        suggest_shell_commands=True,
        chat_language=None,
        commit_language=None,
        detect_urls=True,
        ignore_mentions=None,
        file_watcher=None,
        auto_copy_context=False,
        auto_accept_architect=True,
        mcp_manager=None,
        enable_context_compaction=False,
        max_compaction_retries=3,
        context_compaction_max_tokens=None,
        context_compaction_summary_tokens=8192,
        map_cache_dir=".",
        repomap_in_memory=False,
        linear_output=False,
        security_config=None,
        registered_tools=None,
        registered_servers=None,
        uuid: str = "",
        parent_uuid: str = "",
        root=None,
        primary_root=None,
        init_metadata={},
        prompt_queue=None,
        _queue_counter=None,
    ):
        from cecli.helpers.agents.service import AgentService

        # initialize from args.map_cache_dir
        self.coroutines = coroutines
        # Per-instance tool and server filtering dictionaries
        # Each contains "included" and "excluded" sets that filter from the global singletons
        self.registered_tools = {"included": set(), "excluded": set()}
        self.registered_servers = {"included": set(), "excluded": set()}
        self._inherited_tools = False

        if registered_tools is not None or registered_servers is not None:
            self.registered_tools = registered_tools
            self.registered_servers = registered_servers
            self._inherited_tools = True

        self.interrupt_event = ThreadSafeEvent()
        self.uuid = str(generate_unique_id()).split("-")[0]
        self.reflected_message = None

        if uuid:
            self.uuid = str(uuid).split("-")[0]

        if parent_uuid:
            self.parent_uuid = str(parent_uuid).split("-")[0]

        self.map_cache_dir = map_cache_dir

        self.chat_language = chat_language
        self.commit_language = commit_language
        self.commit_before_message = []
        self.coder_commit_hashes = set()
        self.rejected_urls = set()
        self.abs_root_path_cache = {}

        self.auto_copy_context = auto_copy_context
        self.auto_accept_architect = auto_accept_architect

        try:
            self.security_config = json.loads(security_config)
        except (json.JSONDecodeError, TypeError):
            self.security_config = {}

        self.ignore_mentions = ignore_mentions
        if not self.ignore_mentions:
            self.ignore_mentions = set()

        self.file_watcher = file_watcher
        if self.file_watcher:
            self.file_watcher.coder = self

        self.suggest_shell_commands = suggest_shell_commands
        self.detect_urls = detect_urls
        self.args = args

        # Init metadata should not persist between initializations
        self.init_metadata = {}

        self.num_cache_warming_pings = num_cache_warming_pings
        self.mcp_manager = mcp_manager
        self.enable_context_compaction = enable_context_compaction

        self.context_compaction_current_ratio = 0
        self.context_compaction_max_tokens = context_compaction_max_tokens
        self.context_compaction_summary_tokens = context_compaction_summary_tokens
        self.max_compaction_retries = max_compaction_retries

        self.max_reflections = nested.getter(self.args, "max_reflections", 3)
        self.max_tool_calls = nested.getter(self.args, "max_tool_calls", 25)

        if not fnames:
            fnames = []

        if io is None:
            io = InputOutput()

        if coder_commit_hashes:
            self.coder_commit_hashes = coder_commit_hashes
        else:
            self.coder_commit_hashes = set()

        self.chat_completion_call_hashes = []
        self.chat_completion_response_hashes = []
        self.need_commit_before_edits = set()

        self.message_tokens_sent = 0
        self.message_tokens_received = 0
        self.message_cached_tokens = 0

        self.token_profiler = TokenProfiler(
            enable_printing=nested.getter(self.args, "show_speed", False)
        )
        self.verbose = verbose
        self.abs_fnames = set()
        self.abs_read_only_fnames = set()
        self.add_gitignore_files = add_gitignore_files
        self.abs_read_only_stubs_fnames = set()
        self.abs_rules_fnames = set()

        self.io = io

        # Wrap io with IOProxy for coder_uuid injection in output messages
        # Always create a new IOProxy so sub-agents get their own _coder_uuid.
        # Unwrap any existing IOProxy to avoid fragile nested proxy chains.
        raw_io = IOProxy.unwrap(io)
        self.io = IOProxy(raw_io, self)

        if not self.parent_uuid:
            self.io.coder = weakref.ref(self)

        self.manual_copy_paste = (
            nested.getter(main_model, "copy_paste_transport", "api") == "clipboard"
        )
        self.copy_paste_mode = (
            nested.getter(main_model, "copy_paste_mode", False) or auto_copy_context
        )

        self.shell_commands = []
        self.partial_response_tool_calls = []

        if not auto_commits:
            dirty_commits = False

        self.auto_commits = auto_commits
        self.dirty_commits = dirty_commits

        self.dry_run = dry_run
        self.pretty = self.io.pretty
        self.linear_output = linear_output
        self.io.linear = linear_output
        self.main_model = main_model

        # Set the reasoning tag name based on model settings or default
        self.reasoning_tag_name = (
            self.get_active_model().reasoning_tag
            if self.get_active_model().reasoning_tag
            else REASONING_TAG
        )

        self.stream = stream and main_model.streaming and not self.manual_copy_paste

        if cache_prompts and self.get_active_model().cache_control:
            self.add_cache_headers = True

        self.show_diffs = show_diffs

        # Initialize all registry sub systems
        AgentService.get_instance(self)
        ConversationService.get_chunks(self).initialize_conversation_system()
        ObservationService.get_instance(self)

        self.commands = commands or Commands(self.io, self, args=args)
        self.commands.coder = self

        # Prompt queue for CLI-33: in-memory FIFO queue managed by
        # cecli.helpers.command_queue. The queue lives on the Coder so it
        # survives model switches (Coder.create preserves identity) while each
        # sub-agent gets its own isolated queue.
        self.prompt_queue = list(prompt_queue) if prompt_queue else []
        self._queue_counter = _queue_counter if _queue_counter else 0
        self._queue_lock = threading.Lock()
        self._processing_queue = False

        self.data_cache = {
            "repo": {"last_key": "", "read_only_count": None},
        }

        self.repo = repo
        if use_git and self.repo is None:
            try:
                self.repo = GitRepoProxy.for_root(
                    None,
                    self.io,
                    fnames=fnames,
                    git_dname=None,
                    models=main_model.commit_message_models(),
                )
            except FileNotFoundError:
                pass

        if self.repo:
            self.root = self.repo.root

        for fname in fnames:
            fname = Path(fname)
            if self.repo and self.repo.git_ignored_file(fname) and not self.add_gitignore_files:
                self.io.tool_warning(f"Skipping {fname} that matches gitignore spec.")
                continue

            if self.repo and self.repo.ignored_file(fname) and not self.add_gitignore_files:
                self.io.tool_warning(f"Skipping {fname} that matches cecli.ignore spec.")
                continue

            if not fname.exists():
                if utils.touch_file(fname):
                    self.io.tool_output(f"Creating empty file {fname}")
                else:
                    self.io.tool_warning(f"Can not create {fname}, skipping.")
                    continue

            if not fname.is_file():
                self.io.tool_warning(f"Skipping {fname} that is not a normal file.")
                continue

            fname = str(fname.resolve())

            self.abs_fnames.add(fname)
            self.check_added_files()

        if not self.repo:
            self.root = utils.find_common_root(self.abs_fnames)

        # Allow sub-agent classes to override the working root (multi-project workspaces).
        if root is not None:
            self.root = os.path.normpath(os.path.abspath(root))

        # A sub-agent may operate on a different base path than its parent. In
        # that case its repo must be scoped to *this* root (per-base-path),
        # rather than inheriting the parent's repo, so the coder's repo/fs match
        # its own root.
        if use_git and self.repo is not None and os.path.normpath(self.repo.root) != self.root:
            try:
                self.repo = GitRepoProxy.for_root(
                    self.root,
                    self.io,
                    fnames=[self.root],
                    git_dname=None,
                    models=main_model.commit_message_models(),
                )
            except FileNotFoundError:
                self.repo = None

        # Store the root of the primary coder so skills files, custom tools, and
        # sub-agent paths can be resolved relative to the primary workspace even
        # when this coder operates on a different base path.
        self.primary_root = (
            primary_root
            if primary_root is not None
            else getattr(self, "primary_root", None) or self.root
        )

        # Initialize the per-base-path FileSystemService for this coder
        self.fs = FileSystemService.for_root(
            root=self.root if hasattr(self, "root") else ".",
            repo=self.repo if hasattr(self, "repo") else None,
        )

        # Auto-return the per-root service (and its git repo) when the last
        # coder sharing this base path is destroyed.
        _fs_key = FileSystemService._normalize_root(self.root if hasattr(self, "root") else ".")
        FileSystemService._inc_ref(_fs_key)
        weakref.finalize(self, FileSystemService._release, _fs_key)

        if read_only_fnames:
            self.abs_read_only_fnames = set()
            for fname in read_only_fnames:
                abs_fname = self.abs_root_path(fname)
                if os.path.exists(abs_fname):
                    self.abs_read_only_fnames.add(abs_fname)
                else:
                    self.io.tool_warning(f"Error: Read-only file {fname} does not exist. Skipping.")

        if read_only_stubs_fnames:
            self.abs_read_only_stubs_fnames = set()
            for fname in read_only_stubs_fnames:
                abs_fname = self.abs_root_path(fname)
                if os.path.exists(abs_fname):
                    self.abs_read_only_stubs_fnames.add(abs_fname)
                else:
                    self.io.tool_warning(
                        f"Error: Read-only (stub) file {fname} does not exist. Skipping."
                    )

        if rules_fnames:
            self.abs_rules_fnames = set()
            for fname in rules_fnames:
                abs_fname = self.abs_root_path(fname)
                if os.path.exists(abs_fname):
                    self.abs_rules_fnames.add(abs_fname)
                else:
                    self.io.tool_warning(f"Error: Rules file {fname} does not exist. Skipping.")

        if map_tokens is None:
            use_repo_map = main_model.use_repo_map
            map_tokens = 1024
        else:
            use_repo_map = map_tokens > 0

        max_inp_tokens = self.get_active_model().info.get("max_input_tokens") or 0

        has_map_prompt = nested.getter(self, "gpt_prompts.repo_content_prefix")

        if use_repo_map and self.repo and has_map_prompt:
            repo_root = self.root
            self.repo_map = RepoMap(
                map_tokens,
                self.map_cache_dir,
                self.get_active_model(),
                io,
                self.gpt_prompts.repo_content_prefix,
                self.verbose,
                max_inp_tokens,
                map_mul_no_files=map_mul_no_files,
                refresh=map_refresh,
                max_code_line_length=map_max_line_length,
                repo_root=repo_root,
                use_memory_cache=repomap_in_memory,
                use_enhanced_map=getattr(self.args, "use_enhanced_map", False),
            )

        self.summarizer = summarizer or ChatSummary(
            [self.get_active_model().weak_model, self.get_active_model()],
            self.get_active_model().max_chat_history_tokens,
        )

        self.summarizer_thread = None
        self.summarized_done_messages = []
        self.summarizing_messages = None

        self.files_edited_by_tools = set()

        # Linting and testing
        self.linter = Linter(
            root=self.root, encoding=io.encoding, interrupt_event=self.interrupt_event
        )
        self.auto_lint = auto_lint
        self.setup_lint_cmds(lint_cmds)
        self.lint_cmds = lint_cmds
        self.auto_test = auto_test
        self.auto_memory = auto_memory
        self.test_cmd = test_cmd

        # Clean up todo list file on startup; sessions will restore it when needed
        todo_file_path = self.local_agent_folder("todo.txt")
        abs_path = self.abs_root_path(todo_file_path)
        if os.path.isfile(abs_path):
            try:
                os.remove(abs_path)
                if self.verbose:
                    self.io.tool_output(f"Removed existing todo list file: {todo_file_path}")
            except Exception as e:
                self.io.tool_warning(f"Could not remove todo list file {todo_file_path}: {e}")

        customizations = dict()
        try:
            if self.args:
                customizations = nested.getter(self.args, "custom", "{}")
                customizations = json.loads(customizations)
        except (json.JSONDecodeError, TypeError):
            customizations = dict()
            pass

        self.custom = customizations
        self.file_diffs = nested.getter(self.args, "file_diffs", True)

        if nested.getter(self.custom, "prompt_map.all", None):
            prompts = PromptRegistry.get_prompt(nested.getter(self.custom, "prompt_map.all"))
            prompt_obj = PromptObject(prompts)
            Coder._prompt_cache[self.prompt_format] = prompt_obj

        if nested.getter(self.custom, f"prompt_map.{self.prompt_format}", None):
            prompts = PromptRegistry.get_prompt(
                nested.getter(self.custom, f"prompt_map.{self.prompt_format}")
            )
            prompt_obj = PromptObject(prompts)
            Coder._prompt_cache[self.prompt_format] = prompt_obj

        # validate the functions jsonschema
        if self.functions:
            from jsonschema import Draft7Validator

            for function in self.functions:
                Draft7Validator.check_schema(function)

            if self.verbose:
                self.io.tool_output("JSON Schema:")
                self.io.tool_output(json.dumps(self.functions, indent=4))

        self.post_init()

    def post_init(self):
        pass

    @property
    def gpt_prompts(self):
        """Get prompts from the registry based on the coder type."""
        cls = self.__class__

        # Every coder class MUST have a prompt_format attribute
        if not hasattr(cls, "prompt_format"):
            raise AttributeError(
                f"Coder class {cls.__name__} must have a 'prompt_format' attribute. "
                "Add 'prompt_format = \"<format_name>\"' to the class definition."
            )

        if cls.prompt_format is None:
            raise AttributeError(
                f"Coder class {cls.__name__} has prompt_format=None. "
                "It must have a valid prompt format name."
            )

        prompt_name = cls.prompt_format

        # Check cache first
        if prompt_name in Coder._prompt_cache:
            return Coder._prompt_cache[prompt_name]

        # Get prompts from registry
        prompts = PromptRegistry.get_prompt(prompt_name)
        # Cache the prompt object
        prompt_obj = PromptObject(prompts)
        Coder._prompt_cache[prompt_name] = prompt_obj

        return prompt_obj

    @property
    def done_messages(self):
        """Get DONE messages from ConversationManager."""
        return ConversationService.get_manager(self).get_messages_dict(MessageTag.DONE)

    @property
    def cur_messages(self):
        """Get CUR messages from ConversationManager."""
        return ConversationService.get_manager(self).get_messages_dict(MessageTag.CUR)

    @staticmethod
    def _strip_provider(model_name: str) -> str:
        """Remove provider prefix from model name (e.g., 'openai/gpt-4' -> 'gpt-4')."""
        if "/" in model_name:
            return model_name.split("/", 1)[1]
        return model_name

    def get_announcements(self):
        sections = {}

        # --- MODELS ---
        main_model = self.main_model

        models_items = [f"{self._strip_provider(main_model.name)} (main)"]
        agent_model = main_model.agent_model
        weak_model = main_model.weak_model

        if agent_model and agent_model.name != main_model.name:
            models_items.append(f"{self._strip_provider(agent_model.name)} (agent)")

        if weak_model and weak_model.name != main_model.name:
            models_items.append(f"{self._strip_provider(weak_model.name)} (weak)")
        if self.edit_format == "architect":
            models_items.append(f"{self._strip_provider(main_model.editor_model.name)} (editor)")

        sections["Models"] = {"items": models_items}

        # --- SETTINGS ---
        settings_items = []

        # Edit format
        settings_items.append(f"{self.edit_format} (edit format)")

        # Thinking tokens
        thinking_tokens = self.get_active_model().get_thinking_tokens()
        if thinking_tokens:
            settings_items.append(f"{thinking_tokens} think tokens")

        # Reasoning effort
        reasoning_effort = self.get_active_model().get_reasoning_effort()
        if reasoning_effort:
            settings_items.append(f"reasoning {reasoning_effort}")

        # Prompt cache
        if self.add_cache_headers or main_model.caches_by_default:
            settings_items.append("prompt cache")

        # Infinite output
        if main_model.info.get("supports_assistant_prefill"):
            settings_items.append("infinite output")

        # Copy/paste mode
        if self.copy_paste_mode:
            settings_items.append("copy/paste mode")

        if settings_items:
            sections["Settings"] = {"items": settings_items}

        # --- ENVIRONMENT ---
        env_items = []
        repo_map_tokens = None  # Track for later warning check

        if self.repo:
            rel_repo_dir = self.repo.get_rel_repo_dir()
            num_files = len(self.repo.get_tracked_files())
            env_items.append(f"{rel_repo_dir} ({num_files:,} files)")
            if num_files > 1000 and self.verbose:
                env_items.append(
                    "Warning: For large repos, consider using --subtree-only and .cecli.ignore"
                )
        else:
            env_items.append("no git repo")

        if self.repo_map:
            map_tokens = self.repo_map.max_map_tokens
            if map_tokens > 0:
                refresh = self.repo_map.refresh
                env_items.append(f"map ({map_tokens} tokens, {refresh} refresh)")
                repo_map_tokens = map_tokens
            else:
                env_items.append("repo-map disabled")
        else:
            env_items.append("repo-map disabled")

        sections["Environment"] = {"items": env_items}
        # --- CAPABILITIES ---
        capabilities = {}

        # Sub-agents
        try:
            from cecli.helpers.agents.service import AgentService

            registry = AgentService.get_registry()
            if registry:
                capabilities["Subagents"] = sorted(registry.keys())
        except Exception:
            pass

        # Skills
        if hasattr(self, "skills_manager") and self.skills_manager:
            try:
                skills = self.skills_manager.find_skills()
                if skills:
                    capabilities["Skills"] = [s.name for s in skills]
            except Exception:
                pass

        # MCP Servers
        if self.mcp_tools:
            mcp_servers = []
            for server_name, server_tools in self.mcp_tools:
                if (
                    self.registered_servers["included"]
                    and server_name not in self.registered_servers["included"]
                ):
                    continue
                if server_name in self.registered_servers["excluded"]:
                    continue
                mcp_servers.append(server_name)
            if mcp_servers:
                capabilities["Servers"] = mcp_servers

        if capabilities:
            # sections["Extensions"] = {"subsections": capabilities}
            sections["Environment"]["subsections"] = capabilities

        # --- RENDER ---
        lines = []

        # Version line (CLI only; TUI has its own banner)
        if not self.args.tui:
            lines.append(f"cecli v{__version__}")

        for name, section in sections.items():
            if "items" in section:
                lines.append(f"{name:15s}" + " • ".join(section["items"]))
            if "subsections" in section:
                last_key = next(reversed(section["subsections"]))
                # lines.append(name)
                for sub_name, sub_items in section["subsections"].items():
                    connector = "└─" if sub_name == last_key else "├─"
                    lines.append(f" {connector} {sub_name:10} {' • '.join(sub_items)}")

        # Repo-map max_tokens warning
        if repo_map_tokens is not None:
            max_map_tokens = self.get_active_model().get_repo_map_tokens() * 2
            if repo_map_tokens > max_map_tokens:
                lines.append(
                    f"Warning: map-tokens > {max_map_tokens} is not recommended. Too much"
                    " irrelevant code can confuse LLMs."
                )

        # Read-only stubs
        for fname in self.abs_read_only_stubs_fnames:
            rel_fname = self.get_rel_fname(fname)
            lines.append(f"Added {rel_fname} to the chat (read-only stub).")

        # Restored conversation
        if ConversationService.get_manager(self).get_messages_dict(MessageTag.DONE):
            lines.append("Restored previous conversation history.")

        # Multiline mode
        if self.io.multiline_mode and not self.args.tui:
            lines.append("Multiline mode: Enabled. Enter inserts newline, Alt-Enter submits text")

        return lines

    def show_announcements(self):
        bold = True
        for line in self.get_announcements():
            self.io.tool_output(line, bold=bold)
            bold = False

    def setup_lint_cmds(self, lint_cmds):
        if not lint_cmds:
            return
        for lang, cmd in lint_cmds.items():
            self.linter.set_linter(lang, cmd)

    def add_rel_fname(self, rel_fname):
        self.abs_fnames.add(self.abs_root_path(rel_fname))
        self.check_added_files()

    def drop_rel_fname(self, fname):
        abs_fname = self.abs_root_path(fname)
        if abs_fname in self.abs_fnames:
            self.abs_fnames.remove(abs_fname)
            return True

    def abs_root_path(self, path):
        key = path
        if key in self.abs_root_path_cache:
            return self.abs_root_path_cache[key]

        res = Path(self.root) / path
        res = utils.safe_abs_path(res)
        self.abs_root_path_cache[key] = res
        return res

    fences = all_fences
    fence = fences[0]

    def resolve_relative_to_primary_root(self, path: str) -> str:
        """Resolve a path relative to the primary coder's root (falling back to self.root).

        Used for skills files, custom tools, and sub-agent paths that are defined
        relative to the primary workspace even when this coder operates on a
        different base path.
        """
        if not path:
            return path
        if path.startswith("/"):
            # POSIX-style absolute path (e.g. "/tmp/foo"). os.path.isabs()
            # returns False for "/"-rooted paths on Windows, so guard here
            # to avoid re-anchoring them onto the primary root.
            return path
        if os.path.isabs(path):
            return os.path.normpath(path)
        base = self.primary_root or self.root
        return os.path.normpath(os.path.join(base, path))

    def show_pretty(self):
        if not self.pretty:
            return False

        # only show pretty output if fences are the normal triple-backtick
        if self.fence[0][0] != "`":
            return False

        return True

    def get_abs_fnames_content(self):
        # Remove deleted files from abs_fnames
        deleted_fnames = [f for f in self.abs_fnames if not os.path.exists(f)]
        for fname in deleted_fnames:
            relative_fname = self.get_rel_fname(fname)
            self.io.tool_warning(f"Dropping {relative_fname} from the chat (file was deleted).")
            self.abs_fnames.remove(fname)

        # Sort files by last modified time (earliest first, latest last)
        sorted_fnames = sorted(
            list(self.abs_fnames),
            key=lambda fname: os.path.getmtime(fname),
        )

        for fname in sorted_fnames:
            content = self.io.read_text(fname)

            if content is None:
                relative_fname = self.get_rel_fname(fname)
                self.io.tool_warning(f"Dropping {relative_fname} from the chat.")
                self.abs_fnames.remove(fname)
            else:
                yield fname, content

    def choose_fence(self):
        all_content = ""
        for _fname, content in self.get_abs_fnames_content():
            all_content += content + "\n"
        for _fname in self.abs_read_only_fnames:
            content = self.io.read_text(_fname)
            if content is not None:
                all_content += content + "\n"
        for _fname in self.abs_read_only_stubs_fnames:
            content = self.io.read_text(_fname)
            if content is not None:
                all_content += content + "\n"

        lines = all_content.splitlines()
        good = False
        for fence_open, fence_close in self.fences:
            if any(line.startswith(fence_open) or line.startswith(fence_close) for line in lines):
                continue
            good = True
            break

        if good:
            self.fence = (fence_open, fence_close)
        else:
            self.fence = self.fences[0]
            self.io.tool_warning(
                "Unable to find a fencing strategy! Falling back to:"
                f" {self.fence[0]}...{self.fence[1]}"
            )

        return

    def get_files_content(self, fnames=None):
        if not fnames:
            fnames = self.abs_fnames

        # If there are files, return a dictionary with chat_files and edit_files
        if fnames:
            # Get current time for comparison
            current_time = time.time()
            lookback = current_time - 30

            # Get file modification times and sort by most recent first
            file_times = []
            for fname in fnames:
                try:
                    if os.path.exists(fname):
                        mtime = os.path.getmtime(fname)
                        file_times.append((fname, mtime))
                except OSError:
                    # Skip files that can't be accessed
                    continue

            # Sort by modification time (most recent first)
            file_times.sort(key=lambda x: x[1], reverse=True)

            # Determine which files go to edit_files
            edit_files = set()
            if file_times:
                # Always include the most recently edited file
                most_recent_file, most_recent_time = file_times[0]
                edit_files.add(most_recent_file)

                # Include any files edited within the last minute
                for fname, mtime in file_times:
                    if mtime >= lookback:
                        edit_files.add(fname)

            # Build content for chat_files and edit_files
            chat_files_prompt = ""
            edit_files_prompt = ""
            chat_file_names = set()
            edit_file_names = set()

            for fname, content in self.get_abs_fnames_content():
                if not is_image_file(fname):
                    relative_fname = self.get_rel_fname(fname)
                    file_prompt = "\n"
                    file_prompt += relative_fname
                    file_prompt += f"\n{self.fence[0]}\n"

                    # Apply context management if enabled for large files
                    if self.context_management_enabled:
                        # Calculate tokens for this file
                        file_tokens = self.get_active_model().token_count(content)

                        if file_tokens > self.large_file_token_threshold:
                            # Instead of truncating, show the file's definitions/structure
                            file_stub = RepoMap.get_file_stub(fname, self.io)

                            # Add message about showing definitions instead of full content
                            # self.io.tool_output(
                            #    f"⚠ '{relative_fname}' is very large ({file_tokens} tokens). "
                            #    "Use /context-management to toggle truncation off if needed."
                            # )

                            # Add a message in the content itself so the model knows it's truncated
                            truncation_note = (
                                f"\n... [File content truncated due to size ({file_tokens} tokens)."
                                " Showing structure/definitions only.] ...\n\n"
                            )
                            file_prompt += truncation_note + file_stub
                        else:
                            file_prompt += content
                    else:
                        file_prompt += content

                    file_prompt += f"{self.fence[1]}\n"

                    # Add to appropriate prompt based on edit time
                    if fname in edit_files:
                        edit_files_prompt += file_prompt
                        edit_file_names.add(relative_fname)
                    else:
                        chat_files_prompt += file_prompt
                        chat_file_names.add(relative_fname)

            return {
                "chat_files": chat_files_prompt,
                "edit_files": edit_files_prompt,
                "chat_file_names": chat_file_names,
                "edit_file_names": edit_file_names,
            }
        else:
            # Return empty dictionary when no files
            return {
                "chat_files": "",
                "edit_files": "",
                "chat_file_names": set(),
                "edit_file_names": set(),
            }

    def get_read_only_files_content(self):
        prompt = ""
        # Sort read-only files by last modified time (earliest first, latest last)
        sorted_fnames = sorted(
            list(filter(lambda f: os.path.exists(f), self.abs_read_only_fnames)),
            key=lambda fname: os.path.getmtime(fname),
        )

        # Handle regular read-only files
        for fname in sorted_fnames:
            content = self.io.read_text(fname)
            if content is not None and not is_image_file(fname):
                relative_fname = self.get_rel_fname(fname)
                prompt += "\n"
                prompt += relative_fname
                prompt += f"\n{self.fence[0]}\n"

                # Apply context management if enabled for large files (same as get_files_content)
                if self.context_management_enabled:
                    # Calculate tokens for this file
                    file_tokens = self.get_active_model().token_count(content)

                    if file_tokens > self.large_file_token_threshold:
                        # Instead of truncating, show the file's definitions/structure
                        file_stub = RepoMap.get_file_stub(fname, self.io)

                        # Add message about showing definitions instead of full content
                        # self.io.tool_output(
                        #    f"⚠ '{relative_fname}' is very large ({file_tokens} tokens). "
                        #    "Use /context-management to toggle truncation off if needed."
                        # )

                        # Add a message in the content itself so the model knows it's truncated
                        truncation_note = (
                            f"\n... [File content truncated due to size ({file_tokens} tokens)."
                            " Showing structure/definitions only.] ...\n\n"
                        )
                        prompt += truncation_note + file_stub
                    else:
                        prompt += content
                else:
                    prompt += content

                prompt += f"{self.fence[1]}\n"

        # Sort stub files by last modified time (earliest first, latest last)
        sorted_stub_fnames = sorted(
            list(filter(lambda f: os.path.exists(f), self.abs_read_only_stubs_fnames)),
            key=lambda fname: os.path.getmtime(fname),
        )

        # Handle stub files
        for fname in sorted_stub_fnames:
            if not is_image_file(fname):
                relative_fname = self.get_rel_fname(fname)
                prompt += "\n"
                prompt += f"{relative_fname} (stub)"
                prompt += f"\n{self.fence[0]}\n"
                stub = self.get_file_stub(fname)
                prompt += stub
                prompt += f"{self.fence[1]}\n"
        return prompt

    def get_cur_message_text(self):
        text = ""
        # Get CUR messages from ConversationManager
        cur_messages = ConversationService.get_manager(self).get_messages_dict(MessageTag.CUR)
        for msg in cur_messages:
            # For some models the content is None if the message
            # contains tool calls.
            content = msg.get("content") or ""
            text += content + "\n"
        return text

    def get_ident_mentions(self, text):
        # Split the string on any character that is not alphanumeric
        # \W+ matches one or more non-word characters (equivalent to [^a-zA-Z0-9_]+)
        words = set(re.split(r"\W+", text))
        return words

    def get_ident_filename_matches(self, idents):
        all_fnames = defaultdict(set)
        for fname in self.get_all_relative_files():
            # Skip empty paths or just '.'
            if not fname or fname == ".":
                continue

            try:
                # Handle dotfiles properly
                path = Path(fname)
                base = path.stem.lower()  # Use stem instead of with_suffix("").name
                if len(base) >= 5:
                    all_fnames[base].add(fname)
            except ValueError:
                # Skip paths that can't be processed
                continue

        matches = set()
        for ident in idents:
            if len(ident) < 5:
                continue
            matches.update(all_fnames[ident.lower()])

        return matches

    def get_repo_map(self, force_refresh=False):
        if not self.repo_map or not self.repo:
            return

        self.io.update_spinner("Updating repo map")

        cur_msg_text = self.get_cur_message_text()
        try:
            staged_files_hash = hash(
                str([item.a_path for item in self.repo.repo.index.diff("HEAD")])
            )
        except ANY_GIT_ERROR as err:
            # Handle git errors gracefully - use a fallback hash
            if self.verbose:
                self.io.tool_warning(f"Git error while checking staged files for repo map: {err}")
            staged_files_hash = hash(str(time.time()))  # Use timestamp as fallback

        read_only_count = len(set(self.abs_read_only_fnames)) + len(
            set(self.abs_read_only_stubs_fnames)
        )
        self.data_cache["repo"]["mentioned_idents"] = self.get_ident_mentions(cur_msg_text)

        if (
            staged_files_hash != self.data_cache["repo"]["last_key"]
            or read_only_count != self.data_cache["repo"]["read_only_count"]
        ):
            self.data_cache["repo"]["last_key"] = staged_files_hash
            mentioned_idents = self.data_cache["repo"]["mentioned_idents"]
            mentioned_fnames = self.get_file_mentions(cur_msg_text)
            mentioned_fnames.update(self.get_ident_filename_matches(mentioned_idents))

            all_abs_files = set(self.get_all_abs_files())

            # Exclude metadata/docs from repo map inputs to reduce parsing overhead
            def _include_in_map(abs_path):
                try:
                    rel = self.get_rel_fname(abs_path)
                except Exception:
                    rel = str(abs_path)
                parts = Path(rel).parts
                if ".meta" in parts or ".docs" in parts:
                    return False
                if ".min." in parts[-1]:
                    return False
                if self.repo.ignored_file(abs_path):
                    return False
                return True

            all_abs_files = {p for p in all_abs_files if _include_in_map(p)}
            repo_abs_read_only_fnames = set(self.abs_read_only_fnames) & all_abs_files
            repo_abs_read_only_stubs_fnames = set(self.abs_read_only_stubs_fnames) & all_abs_files
            chat_files = (
                set(self.abs_fnames) | repo_abs_read_only_fnames | repo_abs_read_only_stubs_fnames
            )
            other_files = all_abs_files - chat_files

            self.data_cache["repo"].update(
                {
                    "chat_files": chat_files,
                    "other_files": other_files,
                    "mentioned_fnames": mentioned_fnames,
                    "all_abs_files": all_abs_files,
                    "read_only_count": (
                        len(set(self.abs_read_only_fnames))
                        + len(set(self.abs_read_only_stubs_fnames))
                    ),
                }
            )

        repo_result = self.repo_map.get_repo_map(
            self.data_cache["repo"]["chat_files"],
            self.data_cache["repo"]["other_files"],
            mentioned_fnames=self.data_cache["repo"]["mentioned_fnames"],
            mentioned_idents=self.data_cache["repo"]["mentioned_idents"],
            force_refresh=force_refresh,
        )

        # Extract combined_dict and new_dict from result
        combined_dict = {}
        new_dict = {}
        if repo_result:
            combined_dict = repo_result.get("combined_dict", {})
            new_dict = repo_result.get("new_dict", {})

        # fall back to global repo map if files in chat are disjoint from rest of repo
        if not combined_dict and not new_dict:
            repo_result = self.repo_map.get_repo_map(
                set(),
                self.data_cache["repo"]["all_abs_files"],
                mentioned_fnames=self.data_cache["repo"]["mentioned_fnames"],
                mentioned_idents=self.data_cache["repo"]["mentioned_idents"],
            )
            if repo_result:
                combined_dict = repo_result.get("combined_dict", {})
                new_dict = repo_result.get("new_dict", {})

        # fall back to completely unhinted repo
        if not combined_dict and not new_dict:
            repo_result = self.repo_map.get_repo_map(
                set(),
                self.data_cache["repo"]["all_abs_files"],
            )
            if repo_result:
                combined_dict = repo_result.get("combined_dict", {})
                new_dict = repo_result.get("new_dict", {})

        self.io.update_spinner(self.io.last_spinner_text)

        # Build the return dict for backward compatibility
        if combined_dict or new_dict:
            # Use the prefix from repo_result if available
            prefix = repo_result.get("prefix", "")
            has_chat_files = repo_result.get(
                "has_chat_files", bool(self.data_cache["repo"]["chat_files"])
            )

            return {
                "files": combined_dict,  # Use combined_dict for backward compatibility
                "prefix": prefix,
                "has_chat_files": has_chat_files,
                "combined_dict": combined_dict,
                "new_dict": new_dict,
            }
        else:
            return None

    def get_images_message(self, fnames):
        supports_images = self.get_active_model().info.get("supports_vision")
        supports_pdfs = self.get_active_model().info.get(
            "supports_pdf_input"
        ) or self.get_active_model().info.get("max_pdf_size_mb")

        # https://github.com/BerriAI/litellm/pull/6928
        supports_pdfs = (
            supports_pdfs or "claude-3-5-sonnet-20241022" in self.get_active_model().name
        )

        if not (supports_images or supports_pdfs):
            return []

        messages = []
        for fname in fnames:
            if not is_image_file(fname):
                continue

            mime_type, _ = mimetypes.guess_type(fname)
            if not mime_type:
                continue

            with safe_open(fname, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
            image_url = f"data:{mime_type};base64,{encoded_string}"
            rel_fname = self.get_rel_fname(fname)

            content = []
            if mime_type.startswith("image/") and supports_images:
                content = [
                    {"type": "text", "text": f"Image file: {rel_fname}"},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url, "detail": "high", "format": mime_type},
                    },
                ]
            elif mime_type == "application/pdf" and supports_pdfs:
                content = [
                    {"type": "text", "text": f"PDF file: {rel_fname}"},
                    {"type": "image_url", "image_url": image_url},
                ]

            if content:
                # Register image file with ConversationFiles for tracking
                ConversationService.get_files(self).add_image_file(fname)

                messages.append({"role": "user", "content": content, "image_file": fname})

        return messages

    async def run_stream(self, user_message):
        self.io.user_input(user_message)
        self.init_before_message()
        async for chunk in self.send_message(user_message):
            yield chunk

    def init_before_message(self):
        self.coder_edited_files = set()
        self.reflected_message = None
        self.num_reflections = 0
        self.lint_outcome = None
        self.test_outcome = None
        self.shell_commands = []
        self.message_cost = 0

        if self.repo:
            self.commit_before_message.append(self.repo.get_head_commit_sha())

    async def run(self, with_message=None, preproc=True):
        # Wait for confirmation to finish if in progress
        if not self.io.confirmation_in_progress_event.is_set():
            await self.io.confirmation_in_progress_event.wait()

        if self.linear_output:
            return await self._run_linear(with_message, preproc)

        if self.io.prompt_session:
            from prompt_toolkit.patch_stdout import patch_stdout

            with patch_stdout(raw=True):
                return await self._run_parallel(with_message, preproc)
        else:
            return await self._run_parallel(with_message, preproc)

    async def _run_linear(self, with_message=None, preproc=True):
        try:
            if with_message:
                self.io.user_input(with_message)
                await self.run_one(with_message, preproc)
                return self.partial_response_content

            user_message = None
            await self.io.stop_task_streams()

            while True:
                try:
                    # Wait for commands to finish
                    if not self.commands.cmd_running_event.is_set():
                        await self.commands.cmd_running_event.wait()
                        continue

                    if not self.suppress_announcements_for_next_prompt:
                        self.show_announcements()
                    self.suppress_announcements_for_next_prompt = True

                    if self.message_cost_deferred and not self.io.spinner_active:
                        self.io.tool_output(self.message_cost_deferred)
                        self.message_cost_deferred = None

                    await self.io.recreate_input()
                    await self.io.input_task
                    user_message = self.io.input_task.result()
                    if isinstance(user_message, tuple) and len(user_message) == 2:
                        user_message, _ = user_message
                    if (
                        self.args
                        and not self.args.tui
                        and self.show_pretty()
                        and self.args.fancy_input
                    ):
                        self.io.tool_output("Processing...\n")

                    self.io.output_task = asyncio.create_task(self.generate(user_message, preproc))

                    await self.io.output_task

                    if (
                        self.args
                        and not self.args.tui
                        and self.show_pretty()
                        and self.args.fancy_input
                    ):
                        self.io.tool_output("Finished.")

                    self.io.ring_bell()
                    user_message = None
                    await self.auto_save_session()

                except KeyboardInterrupt:
                    self.io.set_placeholder("")
                    self.io.stop_spinner()
                    self.keyboard_interrupt()
                    await self.io.stop_task_streams()
                except (asyncio.CancelledError, IndexError):
                    pass

        except EOFError:
            return
        finally:
            await self.io.stop_task_streams()

    async def _run_parallel(self, with_message=None, preproc=True):
        try:
            if with_message:
                self.io.user_input(with_message)
                await self.run_one(with_message, preproc)
                return self.partial_response_content

            # Initialize state for task coordination
            self.input_running = True
            self.output_running = True
            self.user_message = ""

            # Cancel any existing tasks
            await self.io.stop_task_streams()

            # Start the input and output tasks
            input_task = asyncio.create_task(self.input_task(preproc))
            output_task = asyncio.create_task(self.output_task(preproc))

            try:
                # Wait for both tasks to complete or for one to raise an exception
                done, pending = await asyncio.wait(
                    [input_task, output_task], return_when=asyncio.FIRST_EXCEPTION
                )

                # Check for exceptions
                for task in done:
                    if task.exception():
                        raise task.exception()

            except (SwitchCoderSignal, SystemExit):
                # Re-raise SwitchCoder to be handled by outer try block
                raise
            finally:
                # Signal tasks to stop
                self.input_running = False
                self.output_running = False

                # Cancel tasks
                input_task.cancel()
                output_task.cancel()

                # Wait for tasks to finish
                try:
                    await asyncio.gather(input_task, output_task, return_exceptions=True)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    pass

                # Ensure IO tasks are properly cancelled
                await self.io.stop_task_streams()

            await self.auto_save_session()
        except EOFError:
            return
        finally:
            await self.io.stop_task_streams()

    async def input_task(self, preproc):
        """
        Handles input creation/recreation and user message processing.
        This task manages the input loop and coordinates with output_task.
        """
        while self.input_running:
            try:
                # Wait for commands to finish
                if not self.commands.cmd_running_event.is_set():
                    await self.commands.cmd_running_event.wait()
                    continue

                # Wait for input task completion
                if self.io.input_task and self.io.input_task.done():
                    try:
                        _result = self.io.input_task.result()
                        user_message = (
                            _result[0]
                            if isinstance(_result, tuple) and len(_result) == 2
                            else _result
                        )

                        # Defer to confirmation handler to fix Windows event loop race.
                        if not self.io.confirmation_in_progress_event.is_set():
                            pass
                        # Set user message for output task
                        elif not self.io.acknowledge_confirmation():
                            if user_message:
                                self.user_message = user_message
                                await self.auto_save_session()
                            else:
                                self.user_message = ""
                                await self.io.stop_task_streams()

                    except (asyncio.CancelledError, KeyboardInterrupt):
                        self.user_message = ""
                        await self.io.stop_task_streams()

                # Check if we should show announcements
                if (
                    self.io.confirmation_in_progress_event.is_set()
                    and not self.user_message
                    and not coroutines.is_active(self.io.input_task)
                    and (not coroutines.is_active(self.io.output_task) or not self.io.placeholder)
                ):
                    if not self.suppress_announcements_for_next_prompt:
                        self.show_announcements()
                    self.suppress_announcements_for_next_prompt = True

                    if self.message_cost_deferred and not self.io.spinner_active:
                        self.io.tool_output(self.message_cost_deferred)
                        self.message_cost_deferred = None

                    # Stop spinner before showing announcements or getting input
                    self.io.stop_spinner()
                    self.copy_context()

                # Check if we should recreate input
                if not coroutines.is_active(self.io.input_task):
                    self.io.ring_bell()
                    await self.io.recreate_input()

                await asyncio.sleep(0.1)  # Small yield to prevent tight loop

            except (SwitchCoderSignal, SystemExit):
                raise
            except Exception as e:
                if self.verbose or self.args.debug:
                    print(e)

    async def output_task(self, preproc):
        """
        Handles output task generation and monitoring.
        This task manages the output loop and coordinates with input_task.
        """
        while self.output_running:
            try:
                # Wait for commands to finish
                if not self.commands.cmd_running_event.is_set():
                    await self.commands.cmd_running_event.wait()
                    continue

                # Check if we have a user message to process
                if self.user_message and not self.io.get_confirmation_acknowledgement():
                    user_message = self.user_message
                    self.user_message = ""

                    # Create output task for processing
                    self.io.output_task = asyncio.create_task(self.generate(user_message, preproc))

                    # Start spinner for output task
                    self.io.start_spinner("Processing...", coder_uuid=getattr(self, "uuid", None))
                    await self.io.recreate_input()

                # Monitor output task
                if self.io.output_task:
                    if self.io.output_task.done():
                        exception = self.io.output_task.exception()
                        if exception:
                            if isinstance(exception, SwitchCoderSignal):
                                await self.io.output_task
                                raise exception

                            self.io.tool_error(f"Error during generation: {exception}")
                            if self.verbose:
                                traceback.print_exception(
                                    type(exception), exception, exception.__traceback__
                                )

                        # Stop spinner when processing task completes
                        self.io.stop_spinner()

                        # And stop monitoring the output task
                        await self.io.stop_output_task()

                await self.auto_save_session()
                await asyncio.sleep(0.1)  # Small yield to prevent tight loop

            except KeyboardInterrupt:
                self.io.stop_spinner()
                self.keyboard_interrupt()
                await self.io.stop_task_streams()
            except (SwitchCoderSignal, SystemExit):
                raise
            except Exception as e:
                self.error_code = 1
                traceback_str = traceback.format_exc()
                update_error_prefix(traceback_str)

                if self.verbose or self.args.debug:
                    print(e)

    async def generate(self, user_message, preproc):
        await asyncio.sleep(0.1)
        self.interrupt_event.clear()

        try:
            if self.enable_context_compaction:
                # Skip compaction if the user wants to clear or exit
                # Compacting is wasteful since /clear will clear everything
                # and /exit will exit the application
                stripped = user_message.strip()
                is_command = self.commands.is_command(stripped)
                is_allowed_command = False

                if is_command:
                    res = self.commands.matching_commands(user_message)
                    if res is not None:
                        matching_commands, first_word, rest_inp = res
                        if len(matching_commands) == 1:
                            command = matching_commands[0]
                            splits = (rest_inp or "").split()
                            split_map = {
                                "/agent": 1,
                                "/architect": 1,
                                "/ask": 1,
                                "/code": 1,
                                "/model": 2,
                            }
                            if command in split_map and len(splits) >= split_map.get(command, 0):
                                is_allowed_command = True

                if not is_command or is_allowed_command:
                    self.compact_context_completed = False
                    await self.compact_context_if_needed()
                    self.compact_context_completed = True

            self.run_one_completed = False
            await self.run_one(user_message, preproc)
            self.show_undo_hint()
        except asyncio.CancelledError:
            # Don't show undo hint if cancelled
            raise
        finally:
            self.run_one_completed = True
            self.compact_context_completed = True
            self.io.stop_spinner()
            # Trim memory in the background so it doesn't stall the event loop
            coroutines.fire_and_forget(asyncio.to_thread(trim_memory))

    def copy_context(self):
        if self.auto_copy_context:
            self.commands.execute("copy-context", "")

    async def get_input(self):
        inchat_files = self.get_inchat_relative_files()
        all_read_only_fnames = self.abs_read_only_fnames | self.abs_read_only_stubs_fnames
        all_read_only_files = [self.get_rel_fname(fname) for fname in all_read_only_fnames]
        all_files = sorted(set(inchat_files + all_read_only_files))
        edit_format = (
            "" if self.edit_format == self.get_active_model().edit_format else self.edit_format
        )

        return await self.io.get_input(
            self.root,
            all_files,
            self.get_addable_relative_files(),
            self.commands,
            abs_read_only_fnames=self.abs_read_only_fnames,
            abs_read_only_stubs_fnames=self.abs_read_only_stubs_fnames,
            edit_format=edit_format,
        )

    async def preproc_user_input(self, inp):
        if not inp:
            return

        # Strip whitespace from beginning and end
        inp = inp.strip()

        if self.commands.is_command(inp):
            run_kwargs = {}
            if inp[0] in "!":
                # Count and strip all leading exclamation marks
                # "!command" -> normal execution
                # "!!command" -> suppress adding output to chat
                # "!!!command" -> background/obstructive mode (TUI suspended)
                num_marks = 0
                while num_marks < len(inp) and inp[num_marks] == "!":
                    num_marks += 1
                command_text = inp[num_marks:]
                inp = f"/run {command_text}"
                if num_marks >= 3:
                    run_kwargs["background"] = True
                    run_kwargs["suppress_add"] = True
                elif num_marks == 2:
                    run_kwargs["suppress_add"] = True

            if self.commands.is_run_command(inp):
                self.commands.cmd_running_event.clear()  # Command is running

            return await self.commands.run(inp, coder=self, **run_kwargs)

        await self.check_for_file_mentions(inp)
        inp = await self.check_for_urls(inp)

        return inp

    def wrap_user_input(self, inp):
        return inp

    async def run_one(self, user_message, preproc):
        self.init_before_message()

        if not await HookIntegration.call_start_hooks(self):
            self.io.tool_warning("Execution stopped by start hook")
            return

        if preproc:
            message = await self.preproc_user_input(user_message)
        else:
            message = user_message

        # /list-queue and /remove-queue must not trigger auto-processing of the
        # queue (CLI-33). Every other message/command — including /queue, so a
        # prompt queued while idle processes immediately — drains the queue once
        # it has fully completed, so queued prompts are sent to the LLM after
        # the current prompt finishes.
        is_management_command = self._is_queue_management_command(user_message)

        try:
            if self.commands.is_command(user_message) and not self.commands.is_test_command(
                user_message
            ):
                return

            if not self.commands.is_command(user_message):
                ConversationService.get_chunks(self).flush_removals()
                self.last_user_message = user_message
                self.error_code = None
                self.num_tool_calls = 0
                # Trim memory in the background so it doesn't delay the response
                coroutines.fire_and_forget(asyncio.to_thread(trim_memory))
                # Fire memorizer after each user request
                # if self.auto_memory and self.edit_format not in ["subagent"]:
                #    from cecli.helpers.memory.utils import invoke_memorizer
                #
                #    context = "If the user has stated any preferences, please remember them"
                #    asyncio.create_task(invoke_memorizer(self, additional_context=context))

            while True:
                self.reflected_message = None
                self.empty_response = False
                self.tool_reflection = False

                if float(self.total_cost) > self.cost_multiplier * (
                    nested.getter(self.args, "cost_limit", float("inf")) or float("inf")
                ):
                    if await self.io.confirm_ask(
                        "You have reached your configured cost limit. Continue?",
                        group_response="Cost Limit",
                        explicit_yes_required=True,
                    ):
                        Coder.cost_multiplier += 1
                    else:
                        return

                async for _ in self.send_message(message):
                    pass

                await self.hot_reload()

                if not self.empty_response:
                    if not self.reflected_message:
                        await self.auto_save_session(force=True)
                        break

                    if self.num_reflections >= self.max_reflections:
                        self.io.tool_warning(
                            f"Only {self.max_reflections} reflections allowed, stopping."
                        )
                        break

                    self.num_reflections += 1

                    if self.tool_reflection:
                        self.num_reflections -= 1

                    if self.reflected_message is True:
                        message = None
                    else:
                        message = self.reflected_message
                elif self.stop_on_empty:
                    await self.auto_save_session(force=True)
                    break

                if self.enable_context_compaction:
                    await self.compact_context_if_needed()

                if nested.getter(self, "agent_finished", False):
                    await self.auto_save_session(force=True)
                    break

                await self.auto_save_session(force=True)
        finally:
            # Drain the queue once the current message/command has fully
            # completed, so queued prompts are sent to the LLM (CLI-33).
            if not is_management_command:
                await self._drain_prompt_queue(preproc)

        if not await HookIntegration.call_end_hooks(self):
            self.io.tool_warning("Execution stopped by end hook")
            return

    def _is_queue_management_command(self, user_message):
        """Return True if user_message is a read-only queue-management command.

        Only /list-queue and /remove-queue are excluded from auto-processing:
        they inspect or mutate the queue without the user asking queued prompts
        to run. /queue is deliberately NOT management here, so a prompt queued
        while the coder is idle drains (and processes) immediately afterward.
        """
        if not self.commands or not self.commands.is_command(user_message):
            return False
        words = user_message.strip().split()
        if not words:
            return False
        cmd_name = words[0][1:]
        return cmd_name in ("list-queue", "remove-queue")

    async def _drain_prompt_queue(self, preproc):
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
                item = command_queue.dequeue_prompt(self)
                if item is None:
                    break
                text = item["text"]
                preview = text if len(text) <= 80 else text[:80] + "..."
                self.io.tool_output(f"Processing queued prompt: {preview}")
                try:
                    await self.run_one(text, preproc)
                except Exception as e:
                    self.io.tool_error(f"Error processing queued prompt: {e}")
        finally:
            self._processing_queue = False

    def _is_url_allowed(self, url):
        allowed_domains = self.security_config.get("allowed-domains")
        if not allowed_domains:
            return True

        parsed_url = urlparse(url)
        domain = parsed_url.netloc.lower()
        if not domain:
            return False

        for allowed in allowed_domains:
            allowed = allowed.lower()
            if domain == allowed or domain.endswith("." + allowed):
                return True
        return False

    async def check_and_open_urls(self, exc, friendly_msg=None):
        """Check exception for URLs, offer to open in a browser, with user-friendly error msgs."""
        text = str(exc)

        if friendly_msg:
            self.io.tool_warning(text)
            self.io.tool_error(f"{friendly_msg}")
        else:
            self.io.tool_error(text)

        # Exclude double quotes from the matched URL characters
        url_pattern = re.compile(r'(https?://[^\s/$.?#].[^\s"]*)')
        # Use set to remove duplicates
        urls = list(set(url_pattern.findall(text)))
        for url in urls:
            url = url.rstrip(".',\"}")  # Added } to the characters to strip
            if self._is_url_allowed(url):
                await self.io.offer_url(url)
        return urls

    async def check_for_urls(self, inp: str) -> List[str]:
        """Check input for URLs and offer to add them to the chat."""
        if not self.detect_urls or (self.args and self.args.disable_scraping):
            return inp

        # Exclude double quotes from the matched URL characters
        url_pattern = re.compile(r'(https?://[^\s/$.?#].[^\s"]*[^\s,.])')
        # Use set to remove duplicates
        urls = list(set(url_pattern.findall(inp)))
        group = ConfirmGroup(urls)
        for url in urls:
            if url not in self.rejected_urls and self._is_url_allowed(url):
                url = url.rstrip(".',\"")
                if await self.io.confirm_ask(
                    "Add URL to the chat?",
                    subject=url,
                    group=group,
                    allow_never=True,
                    explicit_yes_required=not self.args.yes_always_commands,
                ):
                    inp += "\n\n"
                    inp += await self.commands.execute("web", url, return_content=True)
                else:
                    self.rejected_urls.add(url)

        return inp

    def keyboard_interrupt(self):
        # Ensure cursor is visible on exit
        if not self.tui:
            from rich.console import Console

            Console().show_cursor(True)

        self.io.tool_warning("^C KeyboardInterrupt")
        self.interrupt_event.set()
        self.last_keyboard_interrupt = time.time()

    # Old summarization system removed - using context compaction logic instead

    async def compact_context_if_needed(self, force=False, message=""):
        if not self.enable_context_compaction:
            return

        # Trigger background observation/reflection check
        await ObservationService.get_instance(self).check_and_trigger()

        manager = ConversationService.get_manager(self)
        done_messages = manager.get_messages_dict(MessageTag.DONE)
        cur_messages = manager.get_messages_dict(MessageTag.CUR)
        diff_messages = manager.get_messages_dict(MessageTag.DIFFS)
        file_context_messages = manager.get_messages_dict(MessageTag.FILE_CONTEXTS)
        all_messages = manager.get_messages_dict()

        # Exclude first cur_message since that's the user's initial input
        done_tokens = self.summarizer.count_tokens(done_messages)
        cur_tokens = self.summarizer.count_tokens(cur_messages[1:] if len(cur_messages) > 1 else [])
        diff_tokens = self.summarizer.count_tokens(diff_messages)
        file_context_tokens = self.summarizer.count_tokens(file_context_messages)
        all_tokens = self.summarizer.count_tokens(all_messages)

        # Determine if compaction is worthwhile
        compactable_tokens = done_tokens + cur_tokens + diff_tokens + file_context_tokens

        # Condition 1: Is min size
        is_min_size = all_tokens >= self.context_compaction_max_tokens

        # Condition 2: Percentage check (is chat history a significant part of the context?)
        is_worth_by_percentage = all_tokens > 0 and (compactable_tokens / all_tokens) >= 0.20

        # Condition 3: Absolute savings check (would compacting save enough tokens to fit?)
        tokens_over_limit = all_tokens - (self.context_compaction_max_tokens or all_tokens)
        potential_savings = compactable_tokens * 0.90
        is_worth_by_absolute_savings = (
            tokens_over_limit > 0 and potential_savings >= tokens_over_limit
        )

        if not force:
            if not is_min_size:
                return

            if not (is_worth_by_percentage or is_worth_by_absolute_savings):
                self.io.tool_output(
                    "Skipping compaction: Not enough chat history to make a difference. Use /drop to remove files."
                )
                return

        message_tokens = done_tokens + cur_tokens
        file_tokens = diff_tokens + file_context_tokens
        combined_tokens = done_tokens + cur_tokens + diff_tokens + file_context_tokens

        self.context_compaction_current_ratio = all_tokens / self.context_compaction_max_tokens

        if force or (
            all_tokens >= self.context_compaction_max_tokens * 0.9
            and ConversationService.get_chunks(self).last_clear_count > 20
            and file_tokens / max(message_tokens, 1) > 2
        ):
            manager.clear_tag(MessageTag.LINT, ratio=0.33)
            manager.clear_tag(MessageTag.DIFFS, ratio=0.33)
            manager.clear_tag(MessageTag.FILE_CONTEXTS, ratio=0.33)
            ConversationService.get_files(self).clear_file_cache()
            ConversationService.get_chunks(self).flush_removals()
            ConversationService.get_chunks(self).reset_clear_count()
            ObservationService.get_instance(self).reset_index()

        if not force and combined_tokens < self.context_compaction_max_tokens:
            return

        if force:
            self.io.tool_output("Forcing compaction of chat history...")
        else:
            self.io.tool_output("Compacting chat history to make room for new messages...")

        self.io.update_spinner("Compacting...")

        try:
            compaction_prompt = self.gpt_prompts.compaction_prompt
            if message:
                compaction_prompt = f"{compaction_prompt}\n\n{message}"

            async def summarize_and_update(messages, tag):
                text = await self.summarizer.summarize_all_as_text(
                    messages,
                    compaction_prompt,
                    self.context_compaction_summary_tokens,
                    coder=self,
                )
                if not text:
                    raise ValueError(f"Summarization of {tag} messages returned empty.")

                if ObservationService.get_instance(self).observations:
                    obs_text = "\n".join(ObservationService.get_instance(self).observations)
                    text = f"HISTORICAL OBSERVATIONS:\n{obs_text}\n\n{text}"

                manager.clear_tag(tag)

                if tag == MessageTag.DONE:
                    manager.queue_message(message_dict={"role": "user", "content": text}, tag=tag)
                    manager.queue_message(
                        message_dict={
                            "role": "assistant",
                            "content": (
                                "Ok, I will use this summary and the observations as context for"
                                " our conversation going forward."
                            ),
                        },
                        tag=tag,
                    )
                else:
                    if self.last_user_message:
                        manager.queue_message(
                            message_dict={"role": "user", "content": self.last_user_message},
                            tag=tag,
                        )

                    manager.queue_message(
                        message_dict={
                            "role": "assistant",
                            "content": "Ok. I am awaiting your summary of our goals to proceed.",
                        },
                        tag=tag,
                        force=True,
                    )

                    manager.queue_message(
                        message_dict={
                            "role": "user",
                            "content": (
                                "Here is a summary of our current goals and historical"
                                f" context:\n{text}"
                            ),
                        },
                        tag=tag,
                    )

                    manager.queue_message(
                        message_dict={
                            "role": "assistant",
                            "content": (
                                "Ok, I will use this summary and proceed with our task. I will"
                                " first apply any changes in the summary and then continue"
                                " exploration as necessary."
                            ),
                        },
                        tag=tag,
                        force=True,
                    )

                    latest_messages = []
                    for msg in reversed(messages):
                        latest_messages.append(msg)
                        if msg["role"] == "assistant":
                            break
                    for msg in reversed(latest_messages):
                        manager.add_message(msg, tag=tag)

                    # Fire memorizer after successful compaction
                    if self.auto_memory and self.edit_format not in ["subagent"]:
                        from cecli.helpers.memory.utils import invoke_memorizer

                        asyncio.create_task(invoke_memorizer(self, additional_context=text))

            if done_tokens > self.context_compaction_max_tokens or done_tokens > cur_tokens:
                await summarize_and_update(done_messages, MessageTag.DONE)

            if cur_tokens > self.context_compaction_max_tokens or cur_tokens > done_tokens:
                await summarize_and_update(cur_messages, MessageTag.CUR)

            manager.clear_tag(MessageTag.DIFFS)
            manager.clear_tag(MessageTag.FILE_CONTEXTS)
            ConversationService.get_files(self).clear_file_cache()
            ConversationService.get_chunks(self).flush_removals()
            ConversationService.get_chunks(self).reset_clear_count()
            ObservationService.get_instance(self).reset_index()
            self.format_chat_chunks()

            # Post-compaction token floor check
            # Recalculate tokens after compaction to prevent infinite loops
            # on already-minimal context
            all_messages = manager.get_messages_dict()
            post_compaction_tokens = self.summarizer.count_tokens(all_messages)
            token_floor = (
                self.context_compaction_max_tokens * 0.25
                if self.context_compaction_max_tokens
                else 0
            )

            if post_compaction_tokens < token_floor:
                self.io.tool_output(
                    "...context is already at minimum size, cannot compact further."
                )
            else:
                self.io.tool_output("...chat history compacted.")

            self.io.update_spinner(self.io.last_spinner_text)

        except Exception as e:
            self.io.tool_warning(f"Context compaction failed: {e}")
            self.io.tool_warning("Proceeding with full history for now.")
            return

    def normalize_language(self, lang_code):
        """
        Convert a locale code such as ``en_US`` or ``fr`` into a readable
        language name (e.g. ``English`` or ``French``).  If Babel is
        available it is used for reliable conversion; otherwise a small
        built-in fallback map handles common languages.
        """
        if not lang_code:
            return None

        if lang_code.upper() in ("C", "POSIX"):
            return None

        # Probably already a language name
        if (
            len(lang_code) > 3
            and "_" not in lang_code
            and "-" not in lang_code
            and lang_code[0].isupper()
        ):
            return lang_code

        # Preferred: Babel
        if Locale is not None:
            try:
                loc = Locale.parse(lang_code.replace("-", "_"))
                return loc.get_display_name("en").capitalize()
            except Exception:
                pass  # Fall back to manual mapping

        # Simple fallback for common languages
        fallback = {
            "en": "English",
            "fr": "French",
            "es": "Spanish",
            "de": "German",
            "it": "Italian",
            "pt": "Portuguese",
            "zh": "Chinese",
            "ja": "Japanese",
            "ko": "Korean",
            "ru": "Russian",
        }
        primary_lang_code = lang_code.replace("-", "_").split("_")[0].lower()
        return fallback.get(primary_lang_code, lang_code)

    def get_user_language(self):
        """
        Detect the user's language preference and return a human-readable
        language name such as ``English``. Detection order:

        1. ``self.chat_language`` if explicitly set
        2. ``locale.getlocale()``
        3. ``LANG`` / ``LANGUAGE`` / ``LC_ALL`` / ``LC_MESSAGES`` environment variables
        """

        # Explicit override
        if self.chat_language:
            return self.normalize_language(self.chat_language)

        # System locale
        try:
            lang = locale.getlocale()[0]
            if lang:
                lang = self.normalize_language(lang)
            if lang:
                return lang
        except Exception:
            pass

        # Environment variables
        for env_var in ("LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES"):
            lang = os.environ.get(env_var)
            if lang:
                lang = lang.split(".")[0]  # Strip encoding if present
                return self.normalize_language(lang)

        return None

    def get_platform_info(self):
        platform_text = ""
        try:
            platform_text = f"- Platform: {platform.platform()}\n"
        except KeyError:
            # Skip platform info if it can't be retrieved
            platform_text = "- Platform information unavailable\n"

        shell_var = "COMSPEC" if os.name == "nt" else "SHELL"
        shell_val = os.getenv(shell_var)
        platform_text += f"- Shell: {shell_var}={shell_val}\n"

        user_lang = self.get_user_language()
        if user_lang:
            platform_text += f"- Language: {user_lang}\n"

        dt = datetime.now().astimezone().strftime("%Y-%m-%d")
        platform_text += f"- Current date: {dt}\n"

        if self.repo:
            platform_text += "- The user is operating inside a git repository\n"

        if self.lint_cmds:
            if self.auto_lint:
                platform_text += (
                    "- The user's pre-commit runs these lint commands, don't suggest running"
                    " them:\n"
                )
            else:
                platform_text += "- The user prefers these lint commands:\n"
            for lang, cmd in self.lint_cmds.items():
                if lang is None:
                    platform_text += f"  - {cmd}\n"
                else:
                    platform_text += f"  - {lang}: {cmd}\n"

        if self.test_cmd:
            if self.auto_test:
                platform_text += (
                    "- The user's pre-commit runs this test command, don't suggest running them: "
                )
            else:
                platform_text += "- The user prefers this test command: "
            platform_text += self.test_cmd + "\n"

        return platform_text

    def fmt_system_prompt(self, prompt):
        final_reminders = []

        lazy_prompt = ""
        if self.get_active_model().lazy:
            lazy_prompt = self.gpt_prompts.lazy_prompt
            final_reminders.append(lazy_prompt)

        overeager_prompt = ""
        if self.get_active_model().overeager:
            overeager_prompt = self.gpt_prompts.overeager_prompt
            final_reminders.append(overeager_prompt)
        user_lang = self.get_user_language()
        if user_lang:
            final_reminders.append(f"Reply in {user_lang}.\n")

        platform_text = self.get_platform_info()

        if self.suggest_shell_commands:
            shell_cmd_prompt = self.gpt_prompts.shell_cmd_prompt.format(platform=platform_text)
            shell_cmd_reminder = self.gpt_prompts.shell_cmd_reminder.format(platform=platform_text)
            rename_with_shell = self.gpt_prompts.rename_with_shell
        else:
            shell_cmd_prompt = self.gpt_prompts.no_shell_cmd_prompt.format(platform=platform_text)
            shell_cmd_reminder = self.gpt_prompts.no_shell_cmd_reminder.format(
                platform=platform_text
            )
            rename_with_shell = ""

        if user_lang:  # user_lang is the result of self.get_user_language()
            language = user_lang
        else:
            # Default if no specific lang detected
            language = "the same language they are using"

        if self.fence[0] == "`" * 4:
            quad_backtick_reminder = (
                "\nIMPORTANT: Use *quadruple* backticks ```` as fences, not triple backticks!\n"
            )
        else:
            quad_backtick_reminder = ""

        if self.mcp_tools and len(self.mcp_tools) > 0:
            final_reminders.append(self.gpt_prompts.tool_prompt)

        final_reminders = "\n\n".join(final_reminders)

        prompt = prompt.format(
            fence=self.fence,
            quad_backtick_reminder=quad_backtick_reminder,
            final_reminders=final_reminders,
            platform=platform_text,
            shell_cmd_prompt=shell_cmd_prompt,
            rename_with_shell=rename_with_shell,
            shell_cmd_reminder=shell_cmd_reminder,
            go_ahead_tip=self.gpt_prompts.go_ahead_tip,
            language=language,
            lazy_prompt=lazy_prompt,
            overeager_prompt=overeager_prompt,
        )

        return prompt

    def format_chat_chunks(self):
        # Choose appropriate fence based on file content
        self.choose_fence()

        ConversationService.get_chunks(self).initialize_conversation_system()

        # Clean up ConversationFiles and remove corresponding messages
        ConversationService.get_chunks(self).cleanup_files()

        # Add reminder message with list of readonly and editable files
        ConversationService.get_chunks(self).add_file_list_reminder()

        # Add system messages (system prompt, examples, reminder)
        ConversationService.get_chunks(self).add_system_messages()

        # Add rules messages
        ConversationService.get_chunks(self).add_rules_messages()

        # Add repository map messages
        ConversationService.get_chunks(self).add_repo_map_messages()

        # Add read-only file messages
        ConversationService.get_chunks(self).add_readonly_files_messages()
        # Add chat and edit file messages
        ConversationService.get_chunks(self).add_chat_files_messages()

        ConversationService.get_manager(self).flush_queue()

        # Return formatted messages for LLM
        return ConversationService.get_manager(self).get_messages_dict()

    def format_messages(self):
        chunks = self.format_chat_chunks()
        return chunks

    def warm_cache(self, chunks):
        if not self.add_cache_headers:
            return
        if not self.num_cache_warming_pings:
            return
        if not self.ok_to_warm_cache:
            return

        delay = 5 * 60 - 5
        delay = float(os.environ.get("CECLI_CACHE_KEEPALIVE_DELAY", delay))
        self.next_cache_warm = time.time() + delay
        self.warming_pings_left = self.num_cache_warming_pings
        self.cache_warming_chunks = chunks

        if self.cache_warming_thread:
            return

        def warm_cache_worker():
            while self.ok_to_warm_cache:
                time.sleep(1)
                if self.warming_pings_left <= 0:
                    continue
                now = time.time()
                if now < self.next_cache_warm:
                    continue

                self.warming_pings_left -= 1
                self.next_cache_warm = time.time() + delay

                kwargs = dict(self.get_active_model().extra_params) or dict()
                kwargs["max_tokens"] = 1

                try:
                    completion = litellm.completion(
                        model=self.get_active_model().name,
                        messages=ConversationService.get_manager(self).get_messages_dict(),
                        stream=False,
                        **kwargs,
                    )
                except Exception as err:
                    self.io.tool_warning(f"Cache warming error: {str(err)}")
                    continue

                cache_hit_tokens = getattr(
                    completion.usage, "prompt_cache_hit_tokens", 0
                ) or getattr(completion.usage, "cache_read_input_tokens", 0)

                if self.verbose:
                    self.io.tool_output(f"Warmed {format_tokens(cache_hit_tokens)} cached tokens.")

        self.cache_warming_thread = threading.Timer(0, warm_cache_worker)
        self.cache_warming_thread.daemon = True
        self.cache_warming_thread.start()

        return chunks

    async def check_tokens(self, messages):
        """Check if the messages will fit within the model's token limits."""
        input_tokens = self.get_active_model().token_count(messages)
        max_input_tokens = self.get_active_model().info.get("max_input_tokens") or 0

        if max_input_tokens and input_tokens >= max_input_tokens:
            if self.enable_context_compaction:
                self.io.tool_output(
                    f"Estimated chat context of {input_tokens:,} tokens exceeds the"
                    f" {max_input_tokens:,} token limit. Attempting to compact..."
                )
                await self.compact_context_if_needed(force=True)

                # After compaction, re-format messages and re-check tokens
                messages = self.format_messages()
                input_tokens = self.get_active_model().token_count(messages)

            if max_input_tokens and input_tokens >= max_input_tokens:
                self.io.tool_error(
                    f"Your estimated chat context of {input_tokens:,} tokens still exceeds the"
                    f" {max_input_tokens:,} token limit for {self.get_active_model().name}!"
                )
                self.io.tool_output("To reduce the chat context:")
                self.io.tool_output("- Use /drop to remove unneeded files from the chat")
                self.io.tool_output("- Use /clear to clear the chat history")
                self.io.tool_output("- Break your code into smaller files")
                self.io.tool_output(
                    "It's probably safe to try and send the request, most providers won't charge if"
                    " the context limit is exceeded."
                )

                if not await self.io.confirm_ask("Try to proceed anyway?"):
                    return None

        return messages

    def get_active_model(self):
        return self.main_model

    def empty_llm_tool_warning(self) -> str:
        """Ollama-friendly copy for local models; cloud hint otherwise."""
        name = str(getattr(getattr(self, "main_model", None), "name", "") or "")
        if "ollama" in name.lower():
            return (
                "Empty response from the local model (Ollama). "
                "The model may have timed out, unloaded, or hit context limits."
            )
        return "Empty response received from LLM. Check API keys, quota, or provider status."

    async def send_message(self, inp):
        # Notify IO that LLM processing is starting
        self.io.llm_started()

        ConversationService.get_manager(self).flush_queue()

        # Clear any stale interrupt state before starting formatting
        # to avoid immediately re-catching a previous interrupt
        self.interrupt_event.clear()

        if inp:
            # Make sure current coder actually has control of conversation system
            ConversationService.get_chunks(self).initialize_conversation_system()
            self.format_chat_chunks()

            # Always add user message to conversation manager
            ConversationService.get_manager(self).queue_message(
                message_dict=dict(role="user", content=inp),
                tag=MessageTag.CUR,
                hash_key=(
                    "user_message",
                    xxhash.xxh3_128_hexdigest(inp.encode("utf-8", errors="replace")),
                    str(time.monotonic_ns()),
                ),
            )

        ConversationService.get_manager(self).decrement_message_markers()
        import asyncio

        loop = asyncio.get_running_loop()

        async def format_in_executor():
            return await loop.run_in_executor(None, self.format_messages)

        result, interrupted = await self.coroutines.interruptible(
            format_in_executor(), self.interrupt_event
        )

        if interrupted:
            # Use CancelledError instead of KeyboardInterrupt to avoid
            # propagating through the asyncio event loop during cleanup.
            # KeyboardInterrupt is re-raised by Task.__step and bypasses
            # asyncio.gather(return_exceptions=True), causing crashes
            # when tasks are gathered during _cleanup_loop.
            raise asyncio.CancelledError("Interrupted during message formatting")

        messages = result

        messages = await self.check_tokens(messages)
        if not messages:
            return

        if self.verbose:
            utils.show_messages(messages, functions=self.functions)

        self.multi_response_content = ""
        if self.show_pretty():
            spinner_text = f"Waiting for {self.get_active_model().name}"

            if not self.tui:
                spinner_text += f" • ${self.format_cost(self.total_cost)} session"

            if self.io.spinner_active:
                self.io.start_spinner(spinner_text, coder_uuid=getattr(self, "uuid", None))
            else:
                self.message_cost_deferred = spinner_text

            if self.stream:
                self.mdstream = True
            else:
                self.mdstream = None
        else:
            self.mdstream = None

        retry_delay = 0.125

        litellm_ex = LiteLLMExceptions()

        self.usage_report = None
        exhausted = False
        interrupted = False

        try:
            while True:
                try:
                    async for chunk in self.send(messages, tools=self.get_tool_list()):
                        yield chunk
                    break
                except EmptyResponseError:
                    self.io.tool_warning(self.empty_llm_tool_warning())

                    retry_on_empty = False
                    retries_config = self.get_active_model().retries
                    if isinstance(retries_config, str):
                        try:
                            retries_config = json.loads(retries_config)
                        except json.JSONDecodeError:
                            self.io.tool_warning(
                                f"Could not parse retries config: {retries_config}"
                            )
                            retries_config = {}
                    if isinstance(retries_config, dict):
                        retry_on_empty = retries_config.get("retry_on_empty", False)

                    if not retry_on_empty:
                        break

                    retry_delay *= 2
                    if retry_delay > RETRY_TIMEOUT:
                        self.io.tool_error("Retry timeout exceeded on empty response.")
                        break

                    self.io.tool_output(f"Retrying in {retry_delay:.1f} seconds...")

                    _res, interrupted_sleep = await coroutines.interruptible(
                        asyncio.sleep(retry_delay), self.interrupt_event
                    )
                    if interrupted_sleep:
                        interrupted = True
                        break
                    continue
                except litellm_ex.exceptions_tuple() as err:
                    ex_info = litellm_ex.get_ex_info(err)

                    if ex_info.name == "ContextWindowExceededError":
                        exhausted = True
                        break

                    should_retry = ex_info.retry
                    if should_retry:
                        retry_delay *= 2
                        if retry_delay > RETRY_TIMEOUT:
                            should_retry = False

                    if not should_retry:
                        self.mdstream = None
                        await self.check_and_open_urls(err, ex_info.description)
                        break

                    err_msg = str(err)
                    if ex_info.description:
                        self.io.tool_warning(err_msg)
                        self.io.tool_error(ex_info.description)
                    else:
                        self.io.tool_error(err_msg)

                    self.io.tool_output(f"Retrying in {retry_delay:.1f} seconds...")

                    _res, interrupted_sleep = await coroutines.interruptible(
                        asyncio.sleep(retry_delay), self.interrupt_event
                    )
                    if interrupted_sleep:
                        interrupted = True
                        break

                    continue
                except (KeyboardInterrupt, asyncio.CancelledError):
                    interrupted = True
                    break
                except FinishReasonLength:
                    # We hit the output limit!
                    if not self.get_active_model().info.get("supports_assistant_prefill"):
                        exhausted = True
                        break

                    self.multi_response_content = self.get_multi_response_content_in_progress()

                    if messages[-1]["role"] == "assistant":
                        messages[-1]["content"] = self.multi_response_content
                    else:
                        messages.append(
                            dict(role="assistant", content=self.multi_response_content, prefix=True)
                        )
                except Exception as err:
                    self.error_code = 1
                    self.mdstream = None
                    lines = traceback.format_exception(type(err), err, err.__traceback__)
                    self.io.tool_warning("".join(lines))
                    self.io.tool_error(str(err))
                    return
        finally:
            if self.mdstream:
                content_to_show = (
                    "" if self.tui and self.tui() else self.live_incremental_response(True)
                )
                self.stream_wrapper(content_to_show, final=True)
            self.mdstream = None

            # Ensure any waiting spinner is stopped
            self.io.start_spinner("Processing Answer...", coder_uuid=getattr(self, "uuid", None))

            if not self.io.spinner_active:
                self.partial_response_content = self.get_multi_response_content_in_progress(True)

            self.remove_reasoning_content()
            self.multi_response_content = ""

        self.io.tool_output()
        self.show_usage_report()
        await self.add_assistant_reply_to_cur_messages()
        if exhausted:
            cur_messages = ConversationService.get_manager(self).get_messages_dict(MessageTag.CUR)
            if cur_messages and cur_messages[-1]["role"] == "user":
                # Always add to conversation manager
                ConversationService.get_manager(self).add_message(
                    message_dict=dict(
                        role="assistant",
                        content="FinishReasonLength exception: you sent too many tokens",
                    ),
                    tag=MessageTag.CUR,
                    force=True,
                    promotion=ConversationService.get_manager(self).DEFAULT_TAG_PROMOTION_VALUE,
                    mark_for_demotion=1,
                )

            await self.show_exhausted_error()
            self.num_exhausted_context_windows += 1
            self._release_response_buffers()
            return
        if self.partial_response_function_call:
            args = self.parse_partial_args()
            if args:
                content = args.get("explanation") or ""
            else:
                content = ""
        elif self.partial_response_content:
            content = self.partial_response_content
        else:
            content = ""

        if interrupted:
            # Always add to conversation manager
            ConversationService.get_manager(self).add_message(
                message_dict=dict(role="user", content="^C KeyboardInterrupt"),
                tag=MessageTag.CUR,
                force=True,
                promotion=ConversationService.get_manager(self).DEFAULT_TAG_PROMOTION_VALUE,
                mark_for_demotion=1,
            )

            # Always add assistant response to conversation manager
            ConversationService.get_manager(self).add_message(
                message_dict=dict(
                    role="assistant", content="I see that you interrupted my previous reply."
                ),
                tag=MessageTag.CUR,
                force=True,
                promotion=ConversationService.get_manager(self).DEFAULT_TAG_PROMOTION_VALUE,
                mark_for_demotion=1,
            )

            # The reply was interrupted mid-stream; drop the partial chunk buffers
            # rather than holding them until the next send().
            self._release_response_buffers()
            return

        edited = await self.apply_updates()

        # Run tests before committing so failing tests abort the commit and
        # reflect the errors back for the model to fix first.
        if edited and self.auto_test and self.test_cmd:
            test_errors = await self.commands.execute("test", self.test_cmd)
            self.test_outcome = not test_errors
            if test_errors:
                ok = await self.io.confirm_ask("Attempt to fix test errors?")
                if ok:
                    self.reflected_message = test_errors
                    return

        if edited:
            self.coder_edited_files.update(edited)
            saved_message = await self.auto_commit(edited)

            if not saved_message and hasattr(self.gpt_prompts, "files_content_gpt_edits_no_repo"):
                saved_message = self.gpt_prompts.files_content_gpt_edits_no_repo

        if not interrupted:
            add_rel_files_message = await self.check_for_file_mentions(content)
            if add_rel_files_message:
                if self.reflected_message:
                    self.reflected_message += "\n\n" + add_rel_files_message
                else:
                    self.reflected_message = add_rel_files_message
                return

            # Process any tools using MCP servers
            try:
                if self.partial_response_tool_calls:
                    tool_call_response, a, b = self.consolidate_chunks()
                    if await self.process_tool_calls(tool_call_response):
                        self.num_tool_calls += 1
                        self.reflected_message = self.reflected_message or True
                        return
            except Exception as e:
                self.io.tool_error(f"Error processing tool calls: {str(e)}")
                self.io.tool_error(traceback.format_exc())
                self.reflected_message = True
                return
                # Continue without tool processing

            self.num_tool_calls = 0

            try:
                if await self.reply_completed():
                    return
            except KeyboardInterrupt:
                interrupted = True

        if self.reflected_message:
            return

        if edited and self.auto_lint:
            lint_errors = await self.lint_edited(edited)
            if lint_errors is None:  # Interrupted
                return

            await self.auto_commit(edited, context="Ran the linter")
            self.lint_outcome = not lint_errors
            if lint_errors:
                ok = await self.io.confirm_ask("Attempt to fix lint errors?")
                if ok:
                    self.reflected_message = lint_errors
                    return

        shared_output = await self.run_shell_commands()
        if shared_output:
            ConversationService.get_manager(self).add_message(
                message_dict=dict(role="user", content=shared_output),
                tag=MessageTag.CUR,
                force=True,  # Force update existing message
                promotion=ConversationService.get_manager(self).DEFAULT_TAG_PROMOTION_VALUE,
                mark_for_demotion=1,
            )
            ConversationService.get_manager(self).add_message(
                message_dict=dict(role="assistant", content="Ok"),
                tag=MessageTag.CUR,
                force=True,  # Force update existing message
                promotion=ConversationService.get_manager(self).DEFAULT_TAG_PROMOTION_VALUE,
                mark_for_demotion=1,
            )

        # Turn complete: drop the per-turn LLM stream buffers.  They are reset at
        # the start of the next send(), so holding on to them while idle only
        # wastes memory (chunks can be large for long streaming responses).
        self._release_response_buffers()

    def _extract_and_prepare_tool_calls(self, tool_call_response):
        """
        Unified extraction and preparation of tool calls.
        Returns: list of prepared tool calls
        """
        # 1. Use partial_response_tool_calls if available
        if self.partial_response_tool_calls:
            tool_calls = self.partial_response_tool_calls
        # 2. Extract from tool_call_response
        elif tool_call_response is not None:
            tool_calls = self._extract_from_response(tool_call_response)
        else:
            return []

        # 3. Expand concatenated JSON
        return self._expand_concatenated_json(tool_calls)

    def _extract_from_response(self, response):
        """Extract tool calls from various response formats."""
        original_tool_calls = []
        try:
            if hasattr(response, "choices") and response.choices:
                message = response.choices[0].message
                if hasattr(message, "tool_calls") and message.tool_calls:
                    original_tool_calls = message.tool_calls
        except (AttributeError, IndexError):
            pass

        return original_tool_calls

    def _expand_concatenated_json(self, tool_calls):
        """Expand concatenated JSON arguments."""
        expanded_tool_calls = []
        for tool_call in tool_calls:
            args_string = tool_call.function.arguments.strip()

            # If there are no arguments, or it's not a string that looks like it could
            # be concatenated JSON, just add it and continue.
            if not args_string or not (args_string.startswith("{") or args_string.startswith("[")):
                expanded_tool_calls.append(tool_call)
                continue

            json_chunks = utils.split_concatenated_json(args_string)

            # If it's just a single JSON object, there's nothing to expand.
            if len(json_chunks) <= 1:
                expanded_tool_calls.append(tool_call)
                continue

            merged = responses.merge_glued_json_objects(json_chunks)
            if merged is not None:
                new_tool_call = copy_tool_call(tool_call)
                new_tool_call.function.arguments = json.dumps(merged)
                expanded_tool_calls.append(new_tool_call)
                continue

            # We have concatenated JSON, so expand it into multiple tool calls.
            for i, chunk in enumerate(json_chunks):
                if not chunk.strip():
                    continue

                # Create a new tool call for each JSON chunk, with a unique ID.
                new_tool_call = copy_tool_call(tool_call)
                if hasattr(new_tool_call, "model_copy"):
                    new_tool_call.function.arguments = chunk
                    new_tool_call.id = f"{tool_call.id}-{i}"
                else:
                    new_tool_call.function.arguments = chunk
                    new_tool_call.id = f"{getattr(tool_call, 'id', 'call')}-{i}"
                expanded_tool_calls.append(new_tool_call)

        return expanded_tool_calls

    def _group_tools_by_executor(self, tool_calls):
        """
        Group tools by their server instance.
        Returns: dict with server instances as keys and lists of tool calls as values.
        Uses servers from self.mcp_manager (including LocalServer for local tools).
        """
        groups = {}

        for tool_call in tool_calls:
            # Find which server in mcp_manager handles this tool
            server = self._find_mcp_server_for_tool(tool_call)
            if server:
                if server not in groups:
                    groups[server] = []

                _, unprefixed_tool_call = responses.unprefix_tool_call(tool_call)
                groups[server].append(unprefixed_tool_call)

        return groups

    def _find_mcp_server_for_tool(self, tool_call):
        """Find which MCP server handles this tool."""
        if not self.mcp_tools or len(self.mcp_tools) == 0:
            return None

        # Unprefix the tool name to get the server name and unprefixed tool name
        server_name_from_prefix, unprefixed_tool_name = responses.unprefix_tool_name(
            nested.getter(tool_call, "function.name")
        )

        # Check if this tool_call matches any MCP tool
        for server_name, server_tools in self.mcp_tools:
            for tool in server_tools:
                tool_name_from_schema = nested.getter(tool, "function.name")
                if (
                    tool_name_from_schema
                    and tool_name_from_schema.lower() == unprefixed_tool_name.lower()
                ):
                    # Find the McpServer instance that will be used for communication
                    for server in self.mcp_manager:
                        if server.name == server_name and (
                            not server_name_from_prefix or server.name == server_name_from_prefix
                        ):
                            return server

        return None

    async def _execute_tool_groups(self, tool_groups):
        """Execute all tool groups."""
        all_responses = {}

        # Execute tools for each server
        for server, tool_calls in tool_groups.items():
            # Check if this server is an instance of LocalServer (local tools)
            if isinstance(server, LocalServer):
                # Local tools - use _execute_local_tools
                local_responses = await self._execute_local_tools(tool_calls)
                all_responses[server] = local_responses
            else:
                # MCP tools - use _execute_mcp_tools
                mcp_responses = await self._execute_mcp_tools(server, tool_calls)
                all_responses[server] = mcp_responses

        return all_responses

    async def _execute_local_tools(self, tool_calls):
        """Execute local tools via ToolRegistry."""
        # Default implementation returns errors
        # AgentCoder will override this
        error_responses = []
        for tool_call in tool_calls:
            error_responses.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": f"Coder does not support local tool: {tool_call.function.name}",
                }
            )
        return error_responses

    async def _execute_mcp_tools(self, server, tool_calls):
        """Execute MCP tools via LiteLLM."""
        from cecli.http import httpx

        tool_responses = []
        try:
            # Connect to the server once
            session = await server.connect()
            tool_id_set = set()

            # Execute all tool calls for this server
            for tool_call in tool_calls:
                # LLM APIs sometimes return duplicates and that's annoying part 4
                if tool_call.id in tool_id_set:
                    continue

                tool_id_set.add(tool_call.id)

                try:
                    # Arguments can be a stream of JSON objects.
                    # We need to parse them and run a tool call for each.
                    args_string = tool_call.function.arguments.strip()
                    parsed_args_list = []
                    if args_string:
                        json_chunks = utils.split_concatenated_json(args_string)
                        for chunk in json_chunks:
                            try:
                                parsed_args_list.append(json.loads(chunk))
                            except json.JSONDecodeError:
                                self.io.tool_warning(
                                    "Malformed JSON arguments in tool"
                                    f" {tool_call.function.name}: {chunk}"
                                )
                                continue

                    if not parsed_args_list and not args_string:
                        parsed_args_list.append({})  # For tool calls with no arguments

                    all_results_content = []
                    for args in parsed_args_list:
                        new_tool_call = copy_tool_call(tool_call)
                        new_tool_call.function.arguments = json.dumps(args)

                        if not await HookIntegration.call_pre_tool_hooks(
                            self, new_tool_call.function.name, args
                        ):
                            self.io.tool_warning("Tool call skipped by pre-tool call hook")
                            all_results_content.append("Tool Request Aborted.")
                            continue

                        async def do_tool_call():
                            nonlocal session

                            try:
                                return await self.call_mcp_tool_from_session(session, new_tool_call)
                            except Exception as e:
                                if server.is_session_expired_error(e):
                                    session = await server.reconnect()
                                    return await self.call_mcp_tool_from_session(
                                        session, new_tool_call
                                    )
                                raise

                        call_result, interrupted = await coroutines.interruptible(
                            do_tool_call(), self.interrupt_event
                        )

                        if interrupted:
                            raise KeyboardInterrupt("Tool call interrupted")

                        content_parts = []
                        if call_result.content:
                            for item in call_result.content:
                                if hasattr(item, "resource"):  # EmbeddedResource
                                    resource = item.resource
                                    if hasattr(resource, "text"):  # TextResourceContents
                                        content_parts.append(resource.text)
                                    elif hasattr(resource, "blob"):  # BlobResourceContents
                                        try:
                                            decoded_blob = base64.b64decode(resource.blob).decode(
                                                "utf-8"
                                            )
                                            content_parts.append(decoded_blob)
                                        except (UnicodeDecodeError, TypeError):
                                            # Handle non-text blobs gracefully
                                            name = getattr(resource, "name", "unnamed")
                                            mime_type = getattr(
                                                resource, "mimeType", "unknown mime type"
                                            )
                                            content_parts.append(
                                                f"[embedded binary resource: {name} ({mime_type})]"
                                            )
                                elif hasattr(item, "text"):  # TextContent
                                    content_parts.append(item.text)

                        result_text = "".join(content_parts)

                        if not await HookIntegration.call_post_tool_hooks(
                            self, new_tool_call.function.name, args, result_text
                        ):
                            self.io.tool_warning("Tool call output skipped by post-tool call hook")
                            all_results_content.append("Tool Response Redacted.")
                            continue

                        all_results_content.append(result_text)

                    tool_responses.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": "\n\n".join(all_results_content),
                        }
                    )

                except KeyboardInterrupt:
                    self.io.tool_warning(f"Tool call {tool_call.function.name} interrupted.")
                    raise
                except Exception as e:
                    tool_error = f"Error executing tool call {tool_call.function.name}: \n{e}"
                    self.io.tool_warning(
                        f"Executing {tool_call.function.name} on {server.name} failed: \n "
                        f" Error: {e}\n"
                    )
                    tool_responses.append(
                        {"role": "tool", "tool_call_id": tool_call.id, "content": tool_error}
                    )
        except httpx.RemoteProtocolError as e:
            connection_error = f"Server {server.name} disconnected unexpectedly: {e}"
            self.io.tool_warning(connection_error)
            for tool_call in tool_calls:
                tool_responses.append(
                    {"role": "tool", "tool_call_id": tool_call.id, "content": connection_error}
                )
        except asyncio.CancelledError:
            # Re-raise CancelledError to ensure the task cancellation propagates
            raise
        except Exception as e:
            connection_error = f"Could not connect to server {server.name}\n{e}"
            self.io.tool_warning(connection_error)
            for tool_call in tool_calls:
                tool_responses.append(
                    {"role": "tool", "tool_call_id": tool_call.id, "content": connection_error}
                )

        return tool_responses

    async def call_mcp_tool_from_session(self, session, tool_call):
        """Call an MCP tool from an OpenAI-style tool call using the native SDK.

        Accepts either a dict (``{"function": {"name": ..., "arguments": ...}}``)
        or a tool-call object (e.g. litellm's ChatCompletionMessageToolCall) and
        invokes the MCP session directly, avoiding litellm's lazily-imported
        ``experimental_mcp_client`` submodule.
        """
        if isinstance(tool_call, dict):
            function = tool_call.get("function", {})
            name = function.get("name")
            arguments = function.get("arguments", {})
        else:
            function = getattr(tool_call, "function", None)
            name = getattr(function, "name", None)
            arguments = getattr(function, "arguments", {})

        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}

        if not isinstance(arguments, dict):
            arguments = {}

        # Some models mirror the OpenAI wire format and wrap the real params
        # under a single "arguments"/"parameters"/"params" key. Unwrap so the
        # server receives the actual parameters instead of rejecting the call
        # with a "missing required parameter" error.
        arguments = responses.coerce_tool_structure(arguments)

        return await session.call_tool(name=name, arguments=arguments)

    async def process_tool_calls(self, tool_call_response):
        """Simplified main entry point."""
        # Check if max tool calls exceeded
        if self.num_tool_calls >= self.max_tool_calls:
            self.io.tool_warning(f"Only {self.max_tool_calls} tool calls allowed, stopping.")
            return False

        # 1. Extract and prepare tool calls
        prepared_calls = self._extract_and_prepare_tool_calls(tool_call_response)
        if not prepared_calls:
            return False

        # 2. Group by executor
        tool_groups = self._group_tools_by_executor(prepared_calls)

        # 3. Print tool call information
        if tool_groups:
            self._print_tool_call_info(server_tool_calls=tool_groups)

        # 4. Ask for user confirmation
        if not await self.io.confirm_ask("Run tools?", group_response="Run MCP Tools"):
            return False

        # 5. Execute tools
        self.interrupt_event.clear()

        tool_responses_by_server, interrupted = await coroutines.interruptible(
            self._execute_tool_groups(tool_groups), self.interrupt_event
        )

        if interrupted:
            self.io.tool_warning("Tool execution interrupted.")
            return False

        # 6. Add responses to conversation (re-prefixing if necessary)
        tool_responses = []
        for server, server_responses in tool_responses_by_server.items():
            for tool_response in server_responses:
                tool_responses.append(tool_response)

                ConversationService.get_manager(self).add_message(
                    message_dict=tool_response,
                    tag=MessageTag.CUR,
                    hash_key=(tool_response["tool_call_id"], str(time.monotonic_ns())),
                    # promotion=ConversationService.get_manager(self).DEFAULT_TAG_PROMOTION_VALUE,
                    # mark_for_demotion=1,
                )

        return bool(tool_responses)

    def _print_tool_call_info(self, server_tool_calls):
        """Print information about an MCP tool call."""
        # self.io.tool_output("Preparing to run MCP tools", bold=False)

        for server, tool_calls in server_tool_calls.items():
            for tool_call in tool_calls:
                try:
                    if ToolRegistry.get_tool(tool_call.function.name.lower()):
                        ToolRegistry.get_tool(tool_call.function.name.lower()).format_output(
                            coder=self, mcp_server=server, tool_response=tool_call
                        )
                    else:
                        print_tool_response(coder=self, mcp_server=server, tool_response=tool_call)
                except Exception:
                    self.io.tool_output(f"Tool Output Error: {tool_call.function.name.lower()}")
                    self.io.tool_error(traceback.format_exc())
                    pass

    async def initialize_mcp_tools(self):
        """
        Any setup that needs to happen for MCP Servers so that coder can use it properly
        """
        pass

    @property
    def mcp_tools(self):
        if not self.mcp_manager:
            return []

        return list(self.mcp_manager.all_tools.items())

    @mcp_tools.setter
    def mcp_tools(self, value):
        raise AttributeError("mcp_tools is read only.")

    def get_tool_list(self):
        """Get a flattened list of all MCP tools with server prefixes, filtered by registered_servers."""
        tool_list = []
        if self.mcp_tools:
            for server_name, server_tools in self.mcp_tools:
                # Apply per-instance server filtering
                if (
                    self.registered_servers["included"]
                    and server_name not in self.registered_servers["included"]
                ):
                    continue
                if server_name in self.registered_servers["excluded"]:
                    continue

                for tool in server_tools:
                    if server_name.lower() == "local":
                        # Apply per-instance tool name filtering
                        tool_name = tool.get("function", {}).get("name", "")
                        if (
                            self.registered_tools["excluded"]
                            and tool_name.lower() in self.registered_tools["excluded"]
                        ):
                            continue
                        if (
                            self.registered_tools["included"]
                            and tool_name.lower() not in self.registered_tools["included"]
                        ):
                            continue

                    # Prefix the tool name with server name
                    prefixed_tool = responses.prefix_tool_call(tool, server_name)
                    tool_list.append(prefixed_tool)
        return tool_list

    async def reply_completed(self):
        pass

    async def hot_reload(self):
        pass

    async def show_exhausted_error(self):
        output_tokens = 0
        if self.partial_response_content:
            output_tokens = self.get_active_model().token_count(self.partial_response_content)
        max_output_tokens = self.get_active_model().info.get("max_output_tokens") or 0

        messages = self.format_messages()
        if hasattr(messages, "all_messages"):
            # Old system: messages is a ChatChunks object
            messages = messages.all_messages()
        # New system: messages is already a list
        input_tokens = self.get_active_model().token_count(messages)
        max_input_tokens = self.get_active_model().info.get("max_input_tokens") or 0

        total_tokens = input_tokens + output_tokens

        fudge = 0.7

        out_err = ""
        if output_tokens >= max_output_tokens * fudge:
            out_err = " -- possibly exceeded output limit!"

        inp_err = ""
        if input_tokens >= max_input_tokens * fudge:
            inp_err = " -- possibly exhausted context window!"

        tot_err = ""
        if total_tokens >= max_input_tokens * fudge:
            tot_err = " -- possibly exhausted context window!"

        res = ["", ""]
        res.append(f"Model {self.get_active_model().name} has hit a token limit!")
        res.append("Token counts below are approximate.")
        res.append("")
        res.append(f"Input tokens: ~{input_tokens:,} of {max_input_tokens:,}{inp_err}")
        res.append(f"Output tokens: ~{output_tokens:,} of {max_output_tokens:,}{out_err}")
        res.append(f"Total tokens: ~{total_tokens:,} of {max_input_tokens:,}{tot_err}")

        if output_tokens >= max_output_tokens:
            res.append("")
            res.append("To reduce output tokens:")
            res.append("- Ask for smaller changes in each request.")
            res.append("- Break your code into smaller source files.")
            if "diff" not in self.get_active_model().edit_format:
                res.append("- Use a stronger model that can return diffs.")

        if input_tokens >= max_input_tokens or total_tokens >= max_input_tokens:
            res.append("")
            res.append("To reduce input tokens:")
            res.append("- Use /tokens to see token usage.")
            res.append("- Use /drop to remove unneeded files from the chat session.")
            res.append("- Use /clear to clear the chat history.")
            res.append("- Break your code into smaller source files.")

        res = "".join([line + "\n" for line in res])
        self.io.tool_error(res)
        await self.io.offer_url(urls.token_limits)

    async def lint_edited(self, fnames, show_output=True):
        res = ""
        for fname in fnames:
            if not fname:
                continue
            try:
                errors = await self.linter.lint(self.abs_root_path(fname))
            except asyncio.CancelledError:
                self.io.tool_warning("Linting interrupted.")
                return None

            if errors:
                res += "\n"
                res += errors
                res += "\n"

        if self.edit_format in ("agent", "subagent"):
            if self.agent_config.get("show_lint_errors"):
                show_output = True
            else:
                show_output = False

        if res and show_output:
            self.io.tool_warning(res)

        return res

    def __del__(self):
        """Cleanup when the Coder object is destroyed."""
        self.ok_to_warm_cache = False

    def _release_response_buffers(self):
        """Drop per-turn LLM stream data now that the turn has completed.

        `partial_response_content` is intentionally kept: subclasses and callers
        (get_edits, reply_completed, run_stream, ...) read it after the turn.
        """
        self.partial_response_chunks = []
        self.partial_response_consolidated = None
        self.partial_response_reasoning_content = ""

    async def add_assistant_reply_to_cur_messages(self):
        """
        Add the assistant's reply to `cur_messages`.
        Handles model-specific quirks, like Deepseek which requires `content`
        to be `None` when `tool_calls` are present.
        """
        msg = dict(role="assistant")

        # Prefer the response already produced by consolidate_chunks(): it carries
        # the provider-specific fields (e.g. reasoning_items) that we preserved
        # across all chunks, which a fresh litellm.stream_chunk_builder() pass
        # alone would drop or truncate.
        if self.partial_response_consolidated:
            response = self.partial_response_consolidated[0]
        elif not self.stream:
            response = self.partial_response_chunks[0]
        else:
            response = litellm.stream_chunk_builder(self.partial_response_chunks)

        try:
            # Use response_dict as a regular dictionary
            response_dict = response.model_dump()
        except AttributeError:
            # Option 2: Fall back to dict() or response.dict() (Pydantic V1 style)
            try:
                # Note: calling dict(response) works in both V1 and V2 for raw fields,
                # but response.dict() is the Pydantic V1 method name.
                response_dict = dict(response)
            except TypeError:
                self.error_code = 1
                self.io.tool_warning("Response parsing error.")
                return

        msg = response_dict["choices"][0]["message"]

        if self.partial_response_tool_calls:
            msg["tool_calls"] = [_tool_call_to_dict(tc) for tc in self.partial_response_tool_calls]
        elif self.partial_response_function_call:
            msg["function_call"] = _function_call_to_dict(self.partial_response_function_call)

        if "reasoning_content" not in msg:
            msg["reasoning_content"] = self.partial_response_reasoning_content

        # Only add a message if it's not empty.
        if msg is not None and (
            msg.get("content", None)
            or msg.get("tool_calls", None)
            or msg.get("function_call", None)
        ):
            if not await HookIntegration.call_end_message_hooks(self, str(msg)):
                self.io.tool_warning("Execution stopped by end message hook")
                return

            if self.edit_format in ("agent", "subagent"):
                msg.pop("function_call", None)

            ConversationService.get_manager(self).add_message(
                message_dict=msg,
                tag=MessageTag.CUR,
                hash_key=(
                    "assistant_message",
                    xxhash.xxh3_128_hexdigest(str(msg).encode("utf-8", errors="replace")),
                    str(time.monotonic_ns()),
                ),
                # promotion=ConversationService.get_manager(self).DEFAULT_TAG_PROMOTION_VALUE,
                # mark_for_demotion=1,
            )

    def get_file_mentions(self, content, ignore_current=False):
        # 1. Extract words once: O(N)
        words = set()
        for word in content.split():
            word = word.strip("\"'`*_,.!;:?")
            if re.search(r"[\\\/._-]", word):
                words.add(word)

        basename_words = {os.path.basename(w) for w in words if os.path.basename(w) != w}
        all_words = words | basename_words

        # Pre-normalize for O(1) lookups: O(W)
        normalized_words = {w.replace("\\", "/") for w in all_words}

        # 2. Get files and filter ignored once: O(F)
        raw_files = (
            self.get_all_relative_files() if ignore_current else self.get_addable_relative_files()
        )

        # Filter ignored files once to avoid repeated expensive calls
        files_to_check = [f for f in raw_files if not (self.repo and self.repo.git_ignored_file(f))]

        # 3. Existing basenames setup
        existing_basenames = set()

        if not ignore_current:
            existing_basenames = {os.path.basename(f) for f in self.get_inchat_relative_files()} | {
                os.path.basename(self.get_rel_fname(f))
                for f in self.abs_read_only_fnames | self.abs_read_only_stubs_fnames
            }

        # 4. Build map: O(F)
        basename_to_files = {}
        for rel_fname in files_to_check:
            bn = os.path.basename(rel_fname)
            if re.search(r"[\\\/._-]", bn):
                basename_to_files.setdefault(bn, []).append(rel_fname)

        # 5. Final selection: O(F)
        mentioned_rel_fnames = set()
        for rel_fname in files_to_check:
            # Full path match
            if rel_fname.replace("\\", "/") in normalized_words:
                mentioned_rel_fnames.add(rel_fname)
                continue

            # Basename match logic
            bn = os.path.basename(rel_fname)
            if (
                bn in all_words
                and bn not in existing_basenames
                and len(basename_to_files.get(bn, [])) == 1
            ):
                mentioned_rel_fnames.add(rel_fname)

        return mentioned_rel_fnames

    async def check_for_file_mentions(self, content):
        mentioned_rel_fnames = self.get_file_mentions(content)

        new_mentions = mentioned_rel_fnames - self.ignore_mentions

        if not new_mentions:
            return

        added_fnames = []
        group = ConfirmGroup(new_mentions)
        for rel_fname in sorted(new_mentions):
            message = "Add file to the chat?"
            if self.args and self.args.tui:
                message = f"Add file to the chat? ({rel_fname})"

            if await self.io.confirm_ask(
                message,
                subject=rel_fname,
                group=group,
                group_response=str(new_mentions),
                allow_never=True,
            ):
                self.add_rel_fname(rel_fname)
                added_fnames.append(rel_fname)
            else:
                self.ignore_mentions.add(rel_fname)

        if added_fnames:
            return prompts.added_files.format(fnames=", ".join(added_fnames))

    async def send(self, messages, model=None, functions=None, tools=None):
        ModelResponse = litellm.types.utils.ModelResponse

        self.interrupt_event.clear()
        self.got_reasoning_content = False
        self.ended_reasoning_content = False
        self.empty_response = False
        self._output_loop_detected = False
        self._output_loop_message = ""

        self._streaming_buffer_length = 0
        self.io.reset_streaming_response()

        if not model:
            model = self.get_active_model()

        self.partial_response_content = ""
        self.partial_response_reasoning_content = ""
        self.partial_response_chunks = []
        self.partial_response_tool_calls = []
        self.partial_response_function_call = dict()
        self.partial_response_consolidated = None

        completion = None
        self.token_profiler.start()

        litellm_ex = LiteLLMExceptions()

        try:
            # Compaction retry loop for ContextWindowExceededError
            max_compaction_retries = (
                2 if self.edit_format in ("agent", "subagent") else self.max_compaction_retries
            )
            compaction_retry_count = 0

            while True:
                try:
                    completion_coro = model.send_completion(
                        messages,
                        functions,
                        self.stream,
                        self.temperature,
                        tools=tools,
                        override_kwargs=self.model_kwargs.copy(),
                        interrupt_event=self.interrupt_event,
                    )

                    try:
                        (hash_object, completion), interrupted = await coroutines.interruptible(
                            completion_coro, self.interrupt_event
                        )
                    except TypeError:
                        self.io.tool_warning(
                            "TypeError in interruptible() — this may indicate a bug "
                            "in the LLM response handling. Converting to KeyboardInterrupt."
                        )
                        raise KeyboardInterrupt

                    if interrupted:
                        raise KeyboardInterrupt

                    break  # Success

                except litellm.ContextWindowExceededError as err:
                    if not self.enable_context_compaction:
                        raise err

                    if compaction_retry_count >= max_compaction_retries:
                        self.io.tool_error(
                            f"Context window exceeded after {max_compaction_retries}"
                            " compaction attempt(s)."
                        )
                        raise err

                    compaction_retry_count += 1
                    self.io.tool_error(
                        f"Compacting context... retry {compaction_retry_count}"
                        f"/{max_compaction_retries}"
                    )

                    try:
                        await self.compact_context_if_needed(
                            force=True,
                            message="Context window exceeded, please summarize to retry.",
                        )
                    except Exception:
                        self.io.tool_error(
                            "Context compaction failed." " Please use /clear or /compact manually."
                        )
                        raise err

                    # Re-format messages after compaction and retry
                    messages = self.format_messages()
                    continue

            self.chat_completion_call_hashes.append(hash_object.hexdigest())

            if not isinstance(completion, ModelResponse):
                async for chunk in self.show_send_output_stream(completion):
                    yield chunk
            else:
                await self.show_send_output(completion)

            if self.empty_response:
                raise EmptyResponseError

            response, func_err, content_err = self.consolidate_chunks()

            if response:
                completion = response
            # Calculate costs for successful responses
            self.calculate_and_show_tokens_and_cost(messages, completion)

        except litellm_ex.exceptions_tuple() as err:
            self.error_code = 1
            ex_info = litellm_ex.get_ex_info(err)
            if ex_info.name == "ContextWindowExceededError":
                # Still calculate costs for context window errors
                self.token_profiler.on_error()
                self.calculate_and_show_tokens_and_cost(messages, completion)
            raise
        except (KeyboardInterrupt, asyncio.CancelledError) as kbi:
            self.error_code = 130  # apparently standard?
            self.keyboard_interrupt()
            raise kbi
        finally:
            self.preprocess_response()

            if self.partial_response_content:
                self.io.ai_output(self.partial_response_content)
            elif self.partial_response_function_call:
                # TODO: push this into subclasses
                args = self.parse_partial_args()
                if args:
                    self.io.ai_output(json.dumps(args, indent=4))

    async def show_send_output(self, completion):
        ModelResponse = litellm.types.utils.ModelResponse

        if self.verbose:
            print(completion)

        if not isinstance(completion, ModelResponse):
            self.io.tool_error(str(completion))
            return

        if not completion.choices:
            self.io.tool_error(str(completion))
            return

        self.partial_response_chunks.append(completion)

        response, func_err, content_err = self.consolidate_chunks()

        if not await HookIntegration.call_on_message_hooks(self, self.partial_response_content):
            self.io.tool_warning("Execution stopped by on message hook")
            return

        resp_hash = dict(
            function_call=str(self.partial_response_function_call),
            content=self.partial_response_content,
        )
        resp_hash = hashlib.sha1(json.dumps(resp_hash, sort_keys=True).encode())
        self.chat_completion_response_hashes.append(resp_hash.hexdigest())

        if func_err and content_err:
            self.io.tool_error(func_err)
            self.io.tool_error(content_err)
            raise Exception("No data found in LLM response!")

        show_resp = self.render_incremental_response(True)

        if self.partial_response_reasoning_content:
            if nested.getter(self, "args.show_thinking"):
                formatted_reasoning = format_reasoning_content(
                    self.partial_response_reasoning_content, self.reasoning_tag_name
                )
                show_resp = formatted_reasoning + show_resp

        if len(self.partial_response_tool_calls):
            self.tool_reflection = True

        if nested.getter(self, "args.show_thinking"):
            show_resp = replace_reasoning_tags(show_resp, self.reasoning_tag_name)

        if (
            not len(self.partial_response_content)
            and not len(self.partial_response_tool_calls)
            and not len(self.partial_response_reasoning_content)
        ):
            self.empty_response = True
            return

        self.io.assistant_output(show_resp, pretty=self.show_pretty())

        if (
            self.edit_format == "agent"
            and self.stream
            and not show_resp
            and nested.getter(self, "_has_empty_reflected")
        ):
            await asyncio.sleep(4)
            self._has_empty_reflected = True
            self.reflected_message = True
            self.empty_response = True
        else:
            self._has_empty_reflected = False

        if (
            hasattr(completion.choices[0], "finish_reason")
            and completion.choices[0].finish_reason == "length"
        ):
            raise FinishReasonLength()

    async def show_send_output_stream(self, completion):
        received_content = False
        chunk_index = 0
        content_detector = LoopDetector()
        tool_detector = LoopDetector()
        loop_detected = False

        stream = coroutines.interruptible_async_generator(completion, self.interrupt_event)

        try:
            async for chunk in stream:
                if self.args.debug:
                    with safe_open(".cecli/logs/chunks.log", "a") as f:
                        print(chunk, file=f)

                # Check if confirmation is in progress and wait if needed
                if not self.io.confirmation_in_progress_event.is_set():
                    await self.io.confirmation_in_progress_event.wait()

                if isinstance(chunk, str):
                    self.io.tool_error(chunk)
                    continue
                else:
                    if len(chunk.choices) == 0:
                        continue

                    if (
                        hasattr(chunk.choices[0], "finish_reason")
                        and chunk.choices[0].finish_reason == "length"
                    ):
                        raise FinishReasonLength()

                    try:
                        if chunk.choices[0].delta.tool_calls:
                            received_content = True
                            self.token_profiler.on_token()
                            for tool_call_chunk in chunk.choices[0].delta.tool_calls:
                                self.tool_reflection = True

                                if tool_call_chunk.type:
                                    self.io.update_spinner_suffix(tool_call_chunk.type)

                                if tool_call_chunk.function:
                                    if tool_call_chunk.function.name:
                                        self.io.update_spinner_suffix(tool_call_chunk.function.name)

                                    if tool_call_chunk.function.arguments:
                                        self.io.update_spinner_suffix(
                                            tool_call_chunk.function.arguments
                                        )

                                        tool_detector.push(tool_call_chunk.function.arguments)

                    except (AttributeError, IndexError):
                        # Handle cases where the response structure doesn't match expectations
                        pass

                    try:
                        func = chunk.choices[0].delta.function_call
                        # dump(func)
                        if func:
                            for k, v in func.items():
                                self.tool_reflection = True
                                self.io.update_spinner_suffix(v)

                            received_content = True
                            self.token_profiler.on_token()
                    except AttributeError:
                        pass

                    text = ""

                    try:
                        reasoning_content = chunk.choices[0].delta.reasoning_content
                    except AttributeError:
                        try:
                            reasoning_content = chunk.choices[0].delta.reasoning
                        except AttributeError:
                            reasoning_content = None

                    try:
                        content = chunk.choices[0].delta.content
                        if content:
                            if self.got_reasoning_content and not self.ended_reasoning_content:
                                text += f"\n\n</{self.reasoning_tag_name}>\n\n"

                            self.ended_reasoning_content = True
                            text += content
                            received_content = True
                            self.token_profiler.on_token()
                            self.io.update_spinner_suffix(content)
                    except AttributeError:
                        pass

                    if reasoning_content:
                        if (
                            nested.getter(self.args, "show_thinking")
                            and not self.ended_reasoning_content
                        ):
                            if not self.got_reasoning_content:
                                text += f"<{REASONING_TAG}>\n\n"

                            text += reasoning_content
                            self.got_reasoning_content = True
                            received_content = True

                        self.token_profiler.on_token()
                        self.io.update_spinner_suffix(reasoning_content)
                        self.partial_response_reasoning_content += reasoning_content

                self.partial_response_content += text

                chunk_index += 1
                chunk._hidden_params["created_at"] = chunk_index
                self.partial_response_chunks.append(chunk)

                if text:
                    content_detector.push(text)

                if self.show_pretty():
                    # Use simplified streaming - just call the method with full content
                    content_to_show = self.live_incremental_response(False)
                    self.stream_wrapper(content_to_show, final=False)
                elif text:
                    # Apply reasoning tag formatting for non-pretty output
                    if nested.getter(self.args, "show_thinking"):
                        text = replace_reasoning_tags(text, self.reasoning_tag_name)
                    try:
                        self.stream_wrapper(text, final=False)
                    except UnicodeEncodeError:
                        # Safely encode and decode the text
                        safe_text = text.encode(
                            sys.stdout.encoding, errors="backslashreplace"
                        ).decode(sys.stdout.encoding)
                        self.stream_wrapper(safe_text, final=False)
                    yield text
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise KeyboardInterrupt

        except LoopDetectedError as e:
            self._output_loop_detected = True
            self._output_loop_message = str(e)
            loop_detected = True

        if loop_detected:
            self.io.tool_warning(
                f"Output loop detected while streaming: {self._output_loop_message}"
            )
            # Explicitly close the async generators so the wrapper's interrupt
            # task and the underlying provider generator are cleaned up instead
            # of being left suspended after we stop consuming them.
            if hasattr(stream, "aclose"):
                try:
                    await stream.aclose()
                except Exception:
                    pass
            if hasattr(completion, "aclose"):
                try:
                    await completion.aclose()
                except Exception:
                    pass
            return

        if (
            self.show_pretty()
            and nested.getter(self.args, "show_thinking")
            and self.got_reasoning_content
            and not self.ended_reasoning_content
        ):
            self.partial_response_content += f"\n\n</{self.reasoning_tag_name}>\n\n"
            content_to_show = self.live_incremental_response(False)
            self.stream_wrapper(content_to_show, final=False)

        # The Part Doing the Heavy Lifting Now
        self.consolidate_chunks()

        if not await HookIntegration.call_on_message_hooks(self, self.partial_response_content):
            self.io.tool_warning("Execution stopped by on message hook")
            return

        if not received_content and len(self.partial_response_tool_calls) == 0:
            self.empty_response = True
            return

    def consolidate_chunks(self):
        if self.partial_response_consolidated:
            return self.partial_response_consolidated

        response = (
            self.partial_response_chunks[0]
            if not self.stream
            else litellm.stream_chunk_builder(self.partial_response_chunks)
        )
        func_err = None
        content_err = None

        if len(self.partial_response_chunks):
            last_chunk = self.partial_response_chunks[len(self.partial_response_chunks) - 1]
            if last_chunk:
                if getattr(last_chunk, "usage", None):
                    response.usage = last_chunk.usage

        # Collect message-level provider-specific fields (e.g. `reasoning_items`
        # for reasoning models) from ALL chunks.  litellm's stream_chunk_builder()
        # merges these with last-wins semantics for list fields, silently dropping
        # every reasoning item except the final one.  Reasoning models depend on the
        # full ordered item list being present in the assistant message so that
        # exact-prefix prompt caching keeps working across turns, so we collect the
        # fields ourselves and concatenate list-valued entries.
        message_provider_specific_fields = {}
        for chunk in self.partial_response_chunks:
            try:
                if chunk.choices and chunk.choices[0].delta:
                    psf = getattr(chunk.choices[0].delta, "provider_specific_fields", None)
                    if psf and isinstance(psf, dict):
                        for key, value in psf.items():
                            if isinstance(value, list):
                                message_provider_specific_fields.setdefault(key, []).extend(value)
                            elif (
                                key in message_provider_specific_fields
                                and isinstance(message_provider_specific_fields[key], dict)
                                and isinstance(value, dict)
                            ):
                                # Merge dict-valued metadata (e.g. gemini per-call
                                # function_call_signatures) so parallel tool calls
                                # each keep their own signature across chunks.
                                message_provider_specific_fields[key].update(value)
                            elif value is not None:
                                message_provider_specific_fields[key] = value
            except (AttributeError, IndexError):
                continue

        if message_provider_specific_fields:
            message_psf = getattr(response.choices[0].message, "provider_specific_fields", None)
            if not isinstance(message_psf, dict):
                message_psf = {}
            message_psf.update(message_provider_specific_fields)
            response.choices[0].message.provider_specific_fields = message_psf

        try:
            message_tool_calls = response.choices[0].message.tool_calls
            if message_tool_calls and len(message_tool_calls):
                if self.stream:
                    built_tool_calls = self._build_tool_calls_from_chunks()
                    if built_tool_calls:
                        response.choices[0].message.tool_calls = built_tool_calls
                        self.partial_response_tool_calls = built_tool_calls
                    else:
                        # Fall back to litellm's merged list, keeping every call
                        self.partial_response_tool_calls = list(message_tool_calls)
                else:
                    # Non-streaming: the single response chunk already carries the
                    # full tool_calls list
                    self.partial_response_tool_calls = list(message_tool_calls)

                self.partial_response_function_call = self.partial_response_tool_calls[0].function
        except AttributeError as e:
            func_err = e

        try:
            reasoning_content = response.choices[0].message.reasoning_content
        except AttributeError:
            try:
                reasoning_content = response.choices[0].message.reasoning
            except AttributeError:
                reasoning_content = None

        self.partial_response_reasoning_content = reasoning_content or ""

        try:
            content = response.choices[0].message.content
            if isinstance(content, list):
                # OpenAI-compatible APIs sometimes return content as a list
                # of blocks; join the textual pieces for display.
                content = "".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "output_text"
                ) or "".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            self.partial_response_content = content or ""
        except AttributeError as e:
            content_err = e

        # If no native tool calls, check if the content contains JSON tool calls
        # This handles models that write JSON in text instead of using native calling
        if not self.partial_response_tool_calls and self.partial_response_content:
            extracted_calls = responses.extract_tools_from_content_json(
                self.partial_response_content
            )

            if not extracted_calls:
                extracted_calls = responses.extract_tools_from_content_xml(
                    self.partial_response_content
                )

            if not extracted_calls:
                extracted_calls = responses.extract_tools_from_pseudo_json(
                    self.partial_response_content
                )

            if extracted_calls:
                self.tool_reflection = True
                self.partial_response_tool_calls = extracted_calls

        if self._output_loop_detected:
            # A repeating output loop was caught while streaming; turn it into an
            # assistant message so the model can adjust and drop any tool calls.
            marker = "\n\n[SYSTEM CANCEL: OUTPUT LOOP DETECTED]\n"
            self.partial_response_content += marker
            self.partial_response_tool_calls = []
            self.partial_response_function_call = dict()

            # The assistant message stored in the conversation is built from the
            # response object (via model_dump()), so the marker has to be written
            # there too, otherwise it never reaches the model to react to.
            message = response.choices[0].message
            message.content = (message.content or "") + marker
            message.tool_calls = []
            if hasattr(message, "function_call"):
                message.function_call = None

        self.partial_response_consolidated = (response, func_err, content_err)
        return response, func_err, content_err

    def _build_tool_calls_from_chunks(self):
        """Rebuild tool calls from the raw streaming chunks.

        Parallel tool calls arrive interleaved and may start at any index.
        Most providers key fragments by a per-call ``index`` (openai / anthropic /
        gemini), but some (e.g. deepseek) reuse index0 for every call and only
        distinguish them by the ``id`` announced on the first fragment.  Keying
        by id when present -- and remembering the index -> key mapping so later
        id-less fragments resolve to the right call -- preserves every parallel
        call instead of collapsing them onto one, keeps them ordered by first
        appearance, and retains provider-specific fields (e.g. thought
        signatures) attached.
        """
        ChatCompletionMessageToolCall = litellm.types.utils.ChatCompletionMessageToolCall
        Function = litellm.types.utils.Function

        tool_calls_dict = {}
        index_lookup = {}
        last_key = None

        for chunk in self.partial_response_chunks:
            try:
                if not (chunk.choices and chunk.choices[0].delta):
                    continue

                delta = chunk.choices[0].delta
                for tool_call in delta.tool_calls or []:
                    if tool_call is None:
                        continue

                    if nested.getter(tool_call, "function") is None:
                        continue

                    tool_id = nested.getter(tool_call, "id") or ""
                    index = nested.getter(tool_call, "index")

                    if tool_id:
                        key = ("id", tool_id)

                        if index is not None:
                            index_lookup[index] = key

                        last_key = key
                    elif index is not None and index in index_lookup:
                        key = index_lookup[index]
                    elif index is not None:
                        key = ("index", index)
                        index_lookup[index] = key
                    elif last_key is not None:
                        key = last_key
                    else:
                        key = ("slot", len(tool_calls_dict))

                    entry = tool_calls_dict.setdefault(
                        key,
                        {
                            "id": None,
                            "name": None,
                            "type": "function",
                            "arguments": [],
                            "provider_specific_fields": {},
                            "_order": len(tool_calls_dict),
                        },
                    )

                    entry["id"] = tool_id or entry["id"]
                    entry["type"] = nested.getter(tool_call, "type") or entry["type"]
                    entry["name"] = nested.getter(tool_call, "function.name") or entry["name"]

                    arguments = nested.getter(tool_call, "function.arguments")
                    if arguments:
                        entry["arguments"].append(arguments)

                    psf = nested.getter(tool_call, "provider_specific_fields")
                    if not psf:
                        psf = nested.getter(tool_call, "function.provider_specific_fields")
                    if psf and isinstance(psf, dict):
                        entry["provider_specific_fields"].update(psf)
            except (AttributeError, IndexError):
                continue

        tool_calls = []
        for key in sorted(tool_calls_dict.keys(), key=lambda k: tool_calls_dict[k]["_order"]):
            data = tool_calls_dict[key]
            if not (data["id"] and data["name"]):
                continue

            function = Function(
                arguments="".join(data["arguments"]) or "{}",
                name=data["name"],
            )
            params = {
                "id": data["id"],
                "function": function,
                "type": data["type"] or "function",
            }
            if data["provider_specific_fields"]:
                params["provider_specific_fields"] = data["provider_specific_fields"]

            tool_calls.append(ChatCompletionMessageToolCall(**params))

        return tool_calls

    def stream_wrapper(self, content, final):
        if not hasattr(self, "_streaming_buffer_length"):
            self._streaming_buffer_length = 0

        if final:
            content += "\n\n"

        if isinstance(content, str):
            self._streaming_buffer_length += len(content)

            self.io.stream_output(content, final=final)

            if final:
                self._streaming_buffer_length = 0

    def live_incremental_response(self, final):
        show_resp = self.render_incremental_response(final)
        # Apply any reasoning tag formatting
        if nested.getter(self.args, "show_thinking"):
            show_resp = replace_reasoning_tags(show_resp, self.reasoning_tag_name)

        # Track streaming state to avoid repetitive output
        if not hasattr(self, "_streaming_buffer_length"):
            self._streaming_buffer_length = 0

        # Only send new content that hasn't been streamed yet
        if len(show_resp) >= self._streaming_buffer_length:
            new_content = show_resp[self._streaming_buffer_length :]
            return new_content
        else:
            self._streaming_buffer_length = 0
            self.io.reset_streaming_response()
            return show_resp

    def render_incremental_response(self, final):
        # Just return the current content - the streaming logic will handle incremental updates
        return self.get_multi_response_content_in_progress()

    def preprocess_response(self):
        if len(self.partial_response_tool_calls):
            tool_list = []
            tool_id_set = set()

            for tool_call in self.partial_response_tool_calls:
                # Handle both dictionary and object tool calls
                if isinstance(tool_call, dict):
                    tool_id = tool_call.get("id")
                else:
                    tool_id = getattr(tool_call, "id", None)

                # LLM APIs sometimes return duplicates and that's annoying part 2
                if tool_id in tool_id_set:
                    continue

                tool_id_set.add(tool_id)
                tool_list.append(tool_call)

            self.partial_response_tool_calls = tool_list

    def remove_reasoning_content(self):
        """Remove reasoning content from the model's response."""

        self.partial_response_content = remove_reasoning_content(
            self.partial_response_content,
            self.reasoning_tag_name,
        )

    def calculate_and_show_tokens_and_cost(self, messages, completion=None):
        prompt_tokens = 0
        completion_tokens = 0
        cache_hit_tokens = 0
        cache_write_tokens = 0

        if (
            completion
            and nested.getter(completion, "usage.prompt_tokens") is not None
            and nested.getter(completion, "usage.completion_tokens") is not None
        ):
            prompt_tokens = (
                nested.getter(completion.usage, "prompt_tokens", 0)
                or nested.getter(completion.usage, "prompt_eval_count", 0)
                or 0
            )
            completion_tokens = (
                nested.getter(completion.usage, "completion_tokens", 0)
                or nested.getter(completion.usage, "eval_count", 0)
                or 0
            )
            cache_hit_tokens = (
                getattr(completion.usage, "prompt_cache_hit_tokens", 0)
                or getattr(completion.usage, "cache_read_input_tokens", 0)
                or nested.getter(completion.usage, "prompt_tokens_details.cached_tokens", 0)
                or 0
            )
            cache_write_tokens = getattr(completion.usage, "cache_creation_input_tokens", 0) or 0
            self.message_cached_tokens += cache_hit_tokens

            # ``prompt_tokens`` is normalized to the full input (including any
            # cache read/write) for anthropic usage, so no separate add here.
            self.message_tokens_sent += prompt_tokens

        else:
            prompt_tokens = self.get_active_model().token_count(messages)
            completion_tokens = self.get_active_model().token_count(self.partial_response_content)
            self.message_tokens_sent += prompt_tokens

        self.message_tokens_received += completion_tokens

        # Build tokens string as "{prompt} CH {hit_rate:.1f}% ↑ {completion} ↓"
        if prompt_tokens > 0:
            hit_rate = round(cache_hit_tokens / prompt_tokens * 100, 1) if cache_hit_tokens else 0.0
        else:
            hit_rate = 0.0
        tokens_str = f"{format_tokens(prompt_tokens)} ◇ {hit_rate:.1f}%"

        tokens_report = f"{tokens_str} ↑ {format_tokens(completion_tokens)} ↓"

        tokens_report = self.token_profiler.add_to_usage_report(
            tokens_report, self.message_tokens_sent, self.message_tokens_received
        )

        total_combined_tokens = (
            self.total_tokens_sent
            + self.total_tokens_received
            + self.message_tokens_sent
            + self.message_tokens_received
        )
        total_combined_cached = self.total_cached_tokens + self.message_cached_tokens
        total_input_tokens = self.total_tokens_sent + self.message_tokens_sent
        if total_input_tokens > 0:
            total_hit_rate = (
                round(total_combined_cached / total_input_tokens * 100, 1)
                if total_combined_cached
                else 0.0
            )
        else:
            total_hit_rate = 0.0

        total_stats = f"{format_tokens(total_combined_tokens)} ◇ {total_hit_rate:.1f}% ↑↓"

        if not self.get_active_model().info.get("input_cost_per_token"):
            self.usage_report = tokens_report + " " + total_stats
            return

        try:
            # Try and use litellm's built in cost calculator. Seems to work for non-streaming only?
            cost = litellm.completion_cost(completion_response=completion)
        except Exception:
            cost = 0

        if not cost:
            cost = self.compute_costs_from_tokens(
                prompt_tokens, completion_tokens, cache_write_tokens, cache_hit_tokens
            )

        self.total_cost += cost
        self.message_cost += cost

        cost_report = (
            f"${self.format_cost(self.message_cost)} • {total_stats}"
            f" ${self.format_cost(self.total_cost)}"
        )

        if cache_hit_tokens and cache_write_tokens:
            sep = " "
        else:
            sep = " "

        self.usage_report = tokens_report + sep + cost_report

    def format_cost(self, value):
        if value == 0:
            return "0.00"
        magnitude = abs(value)
        if magnitude >= 0.01:
            return f"{value:.2f}"
        else:
            return f"{value:.{max(2, 2 - int(math.log10(magnitude)))}f}"

    def compute_costs_from_tokens(
        self, prompt_tokens, completion_tokens, cache_write_tokens, cache_hit_tokens
    ):
        cost = 0

        input_cost_per_token = self.get_active_model().info.get("input_cost_per_token") or 0
        output_cost_per_token = self.get_active_model().info.get("output_cost_per_token") or 0
        input_cost_per_token_cache_hit = (
            self.get_active_model().info.get("input_cost_per_token_cache_hit")
            or self.get_active_model().info.get("cache_read_input_token_cost")
            or 0
        )

        # deepseek
        # prompt_cache_hit_tokens + prompt_cache_miss_tokens
        #    == prompt_tokens == total tokens that were sent
        #
        # Anthropic
        # cache_creation_input_tokens + cache_read_input_tokens + prompt
        #    == total tokens that were

        if input_cost_per_token_cache_hit:
            cost += cache_hit_tokens * input_cost_per_token_cache_hit
            cost += (prompt_tokens - cache_hit_tokens) * input_cost_per_token
        else:
            # hard code the anthropic adjustments, no-ops for other models since cache_x_tokens==0
            cost += cache_write_tokens * input_cost_per_token * 1.25
            cost += cache_hit_tokens * input_cost_per_token * 0.10
            cost += (prompt_tokens - cache_hit_tokens) * input_cost_per_token

        cost += completion_tokens * output_cost_per_token
        return cost

    def show_usage_report(self):
        if not self.usage_report:
            return

        self.total_tokens_sent += self.message_tokens_sent
        self.total_tokens_received += self.message_tokens_received
        self.total_cached_tokens += self.message_cached_tokens

        if self.tui and self.tui():
            self.tui().update_cost(self.usage_report.replace("\n", " "))
        else:
            self.io.tool_output(self.usage_report)
            self.io.rule()

        self.message_cost = 0.0
        self.message_tokens_sent = 0
        self.message_tokens_received = 0
        self.message_cached_tokens = 0

    def get_multi_response_content_in_progress(self, final=False):
        cur = self.multi_response_content or ""
        new = self.partial_response_content or ""

        if new.rstrip() != new and not final:
            new = new.rstrip()

        return cur + new

    def get_file_stub(self, fname):
        return ConversationService.get_files(self).get_file_stub(fname)

    def get_rel_fname(self, fname):
        try:
            return os.path.relpath(fname, self.root)
        except ValueError:
            return fname

    def get_inchat_relative_files(self):
        files = [self.get_rel_fname(fname) for fname in self.abs_fnames]
        return sorted(set(files))

    def is_file_safe(self, fname):
        try:
            return Path(self.abs_root_path(fname)).is_file()
        except OSError:
            return

    def get_all_relative_files(self):
        """Get all files known to the file service for this coder's base path."""
        fs = getattr(self, "fs", None) or FileSystemService.get_instance()
        if fs.trie:
            # Auto-rebuild if the repository state has changed
            # (e.g., new commits, staged files, or HEAD change)
            if fs.needs_rebuild():
                fs.rebuild()
            files = fs.list_all()
            return files
        return self.get_inchat_relative_files()

    def get_all_abs_files(self):
        files = self.get_all_relative_files()
        files = [self.abs_root_path(path) for path in files]
        return files

    def get_addable_relative_files(self):
        all_files = set(self.get_all_relative_files())
        inchat_files = set(self.get_inchat_relative_files())
        read_only_files = set(self.get_rel_fname(fname) for fname in self.abs_read_only_fnames)
        stub_files = set(self.get_rel_fname(fname) for fname in self.abs_read_only_stubs_fnames)
        return all_files - inchat_files - read_only_files - stub_files

    def check_for_dirty_commit(self, path):
        if not self.repo:
            return
        if not self.dirty_commits:
            return
        if not self.repo.is_dirty(path):
            return

        # We need a committed copy of the file in order to /undo, so skip this
        # fullp = Path(self.abs_root_path(path))
        # if not fullp.stat().st_size:
        #     return

        self.io.tool_output(f"Committing {path} before applying edits.")
        self.need_commit_before_edits.add(path)

    async def allowed_to_edit(self, path):
        full_path = self.abs_root_path(path)
        if self.repo:
            need_to_add = not self.repo.path_in_repo(path)
        else:
            need_to_add = False

        if full_path in self.abs_fnames:
            self.check_for_dirty_commit(path)
            return True

        if self.repo and self.repo.git_ignored_file(path) and not self.add_gitignore_files:
            self.io.tool_warning(f"Skipping edits to {path} that matches gitignore spec.")
            return

        if not Path(full_path).exists():
            rel_path = os.path.relpath(full_path)
            if not await self.io.confirm_ask(f"Create new file? ({rel_path})", subject=path):
                self.io.tool_output(f"Skipping edits to {path}")
                return

            if not self.dry_run:
                if not utils.touch_file(full_path):
                    self.io.tool_error(f"Unable to create {path}, skipping edits.")
                    return

                # Seems unlikely that we needed to create the file, but it was
                # actually already part of the repo.
                # But let's only add if we need to, just to be safe.
                if need_to_add:
                    if not (self.add_gitignore_files and self.repo.git_ignored_file(path)):
                        self.repo.repo.git.add(full_path)

            self.abs_fnames.add(full_path)
            self.check_added_files()
            return True

        if not await self.io.confirm_ask(
            "Allow edits to file that has not been added to the chat?",
            subject=path,
        ):
            self.io.tool_output(f"Skipping edits to {path}")
            return

        if need_to_add:
            if not (self.add_gitignore_files and self.repo.git_ignored_file(path)):
                self.repo.repo.git.add(full_path)

        self.abs_fnames.add(full_path)
        self.check_added_files()
        self.check_for_dirty_commit(path)

        return True

    warning_given = False

    def check_added_files(self):
        if self.warning_given:
            return

        warn_number_of_files = 4
        warn_number_of_tokens = 32 * 1024

        num_files = len(self.abs_fnames)
        if num_files < warn_number_of_files:
            return

        tokens = 0
        for fname in self.abs_fnames:
            if is_image_file(fname):
                continue
            content = self.io.read_text(fname)
            tokens += self.get_active_model().token_count(content)

        if tokens < warn_number_of_tokens:
            return

        if self.context_compaction_current_ratio > 0.5:
            self.io.tool_warning(
                "Warning: it's best to only add files that need changes to the chat."
            )
            self.io.tool_warning(urls.edit_errors)
            self.warning_given = True

    async def prepare_to_edit(self, edits):
        res = []
        seen = dict()

        self.need_commit_before_edits = set()

        for edit in edits:
            path = edit[0]
            if path is None:
                res.append(edit)
                continue
            if path == "python":
                dump(edits)
            if path in seen:
                allowed = seen[path]
            else:
                allowed = await self.allowed_to_edit(path)
                seen[path] = allowed

            if allowed:
                res.append(edit)

        await self.dirty_commit()
        self.need_commit_before_edits = set()

        return res

    async def apply_updates(self):
        edited = set()
        try:
            if getattr(self.args, "tweak_responses", False):
                confirmation = await self.io.confirm_ask("Tweak Response?", allow_tweak=True)

                if confirmation or confirmation == "tweak":
                    if self.tui and self.tui():
                        self.partial_response_content = self.tui().get_response_from_editor(
                            self.partial_response_content
                        )
                    else:
                        self.partial_response_content = self.io.edit_in_editor(
                            self.partial_response_content
                        )

            await asyncio.sleep(0.1)

            edits = self.get_edits()
            edits = self.apply_edits_dry_run(edits)
            edits = await self.prepare_to_edit(edits)
            edited = set(edit[0] for edit in edits)

            self.apply_edits(edits)
        except ValueError as err:
            self.num_malformed_responses += 1

            err = err.args[0]

            self.io.tool_error("The LLM did not conform to the edit format.")
            self.io.tool_output(urls.edit_errors)
            self.io.tool_output()
            self.io.tool_output(str(err))

            self.reflected_message = str(err)
            return edited

        except ANY_GIT_ERROR as err:
            self.io.tool_error(traceback.format_exc())
            self.io.tool_error(str(err))
            return edited
        except Exception as err:
            self.io.tool_error("Exception while updating files:")
            self.io.tool_error(str(err), strip=False)
            self.io.tool_error(traceback.format_exc())
            self.reflected_message = str(err)
            return edited

        for path in edited:
            if self.dry_run:
                self.io.tool_output(f"Did not apply edit to {path} (--dry-run)")
            else:
                self.io.tool_output(f"Applied edit to {path}")

        return edited

    def parse_partial_args(self):
        # dump(self.partial_response_function_call)

        function_call = self.partial_response_function_call
        if isinstance(function_call, dict):
            data = function_call.get("arguments")
        else:
            data = getattr(function_call, "arguments", None)

        if not data:
            return

        try:
            return json.loads(data)
        except JSONDecodeError:
            pass

        try:
            return json.loads(data + "]}")
        except JSONDecodeError:
            pass

        try:
            return json.loads(data + "}]}")
        except JSONDecodeError:
            pass

        try:
            return json.loads(data + '"}]}')
        except JSONDecodeError:
            pass

    def _find_occurrences(self, content, pattern, near_context=None):
        """Find all occurrences of pattern, optionally filtered by near_context."""
        occurrences = []
        start = 0
        while True:
            index = content.find(pattern, start)
            if index == -1:
                break

            if near_context:
                # Check if near_context is within a window around the match
                window_start = max(0, index - 200)
                window_end = min(len(content), index + len(pattern) + 200)
                window = content[window_start:window_end]
                if near_context in window:
                    occurrences.append(index)
            else:
                occurrences.append(index)

            start = index + 1  # Move past this occurrence's start
        return occurrences

    # commits...

    def get_context_from_history(self, history):
        context = ""
        if history:
            for msg in history:
                msg_content = msg.get("content") or ""
                context += "\n" + msg["role"].upper() + ": " + msg_content + "\n"

        return context

    async def auto_commit(self, edited, context=None):
        if not self.repo or not self.auto_commits or self.dry_run:
            return

        # Workspace-aware commit logic
        if hasattr(self.args, "workspace") and self.args.workspace:
            # We are in a workspace context.
            # The GitRepo instance (self.repo) should already be pointing to the correct worktree/repo
            # within the workspace because of the os.chdir in main.py and detection in repo.py.
            pass

        if not context:
            context = self.get_context_from_history(
                ConversationService.get_manager(self).get_messages_dict(MessageTag.CUR)
            )

        try:
            res = await self.repo.commit(
                fnames=edited, context=context, coder_edits=True, coder=self
            )
            if res:
                self.show_auto_commit_outcome(res)
                commit_hash, commit_message = res
                return self.gpt_prompts.files_content_gpt_edits.format(
                    hash=commit_hash,
                    message=commit_message,
                )

            return self.gpt_prompts.files_content_gpt_no_edits
        except ANY_GIT_ERROR as err:
            self.io.tool_error(f"Unable to commit: {str(err)}")
            return

    def show_auto_commit_outcome(self, res):
        commit_hash, commit_message = res
        self.last_coder_commit_hash = commit_hash
        self.coder_commit_hashes.add(commit_hash)
        self.last_coder_commit_message = commit_message
        if self.show_diffs:
            self.commands.execute("diff", "")

    def show_undo_hint(self):
        if not self.commit_before_message:
            return
        if self.commit_before_message[-1] != self.repo.get_head_commit_sha():
            self.io.tool_output("You can use /undo to undo and discard each cecli commit.")

    async def dirty_commit(self):
        if not self.need_commit_before_edits:
            return
        if not self.dirty_commits:
            return
        if not self.repo:
            return

        await self.repo.commit(fnames=self.need_commit_before_edits, coder=self)

        return True

    def get_edits(self, mode="update"):
        return []

    def apply_edits(self, edits):
        return

    def apply_edits_dry_run(self, edits):
        return edits

    def local_agent_folder(self, path):
        primary_uuid = self._resolve_primary_agent_uuid()
        primary_root = os.path.abspath(self.primary_root)

        stripped = path.lstrip("/")

        if self.uuid == primary_uuid:
            rel_dir = f"{primary_root}/.cecli/agents/{GLOBAL_DATE}/{primary_uuid}"
        else:
            rel_dir = f"{primary_root}/.cecli/agents/{GLOBAL_DATE}/{primary_uuid}/s/{self.uuid}"

        os.makedirs(
            self.abs_root_path(rel_dir),
            exist_ok=True,
        )

        return f"{rel_dir}/{stripped}"

    def _resolve_primary_agent_uuid(self):
        """Return the primary coder's uuid for this session (memoized).

        All agents spawned under a primary coder share the same base folder,
        so sub-agent files nest under one location regardless of delegation
        depth. Falls back to this coder's own uuid when the service is
        unavailable (e.g. in unit tests).
        """
        if getattr(self, "_primary_agent_uuid", None):
            return self._primary_agent_uuid

        primary_uuid = self.uuid
        try:
            from cecli.helpers.agents.service import AgentService

            primary_uuid = AgentService.get_primary_uuid() or self.uuid
        except Exception:
            pass

        self._primary_agent_uuid = primary_uuid
        return primary_uuid

    async def auto_save_session(self, force=False):
        """Automatically save the current session to {auto-save-session-name}.json."""
        if not getattr(self.args, "auto_save", False):
            return

        # Initialize last autosave time if not exists
        if not hasattr(self, "_last_autosave_time"):
            self._last_autosave_time = 0

        if not hasattr(self, "_autosave_future"):
            self._autosave_future = None

        if self._autosave_future and not self._autosave_future.done():
            if force:
                try:
                    await self._autosave_future
                except Exception:
                    pass
            else:
                return

        # Throttle autosave to run at most once every 15 seconds
        current_time = time.time()
        if current_time - self._last_autosave_time >= 15.0 or force:
            try:
                self._last_autosave_time = current_time
                session_manager = SessionManager(self, self.io)
                loop = asyncio.get_running_loop()
                self._autosave_future = loop.run_in_executor(
                    None,
                    session_manager.save_session,
                    getattr(self.args, "auto_save_session_name", "auto-save"),
                    False,
                )
            except Exception:
                # Don't show errors for auto-save to avoid interrupting the user experience
                pass

    async def run_shell_commands(self):
        if not self.suggest_shell_commands:
            return ""

        done = set()
        group = ConfirmGroup(set(self.shell_commands))
        accumulated_output = ""

        try:
            self.commands.cmd_running_event.clear()  # Command is running

            for command in self.shell_commands:
                if command in done:
                    continue
                done.add(command)
                output = await self.handle_shell_commands(command, group)
                if output:
                    accumulated_output += output + "\n\n"

            return accumulated_output
        finally:
            self.commands.cmd_running_event.set()  # Command finished

    async def handle_shell_commands(self, commands_str, group):
        commands = [
            cmd
            for cmd in command_parser.split_shell_commands(commands_str)
            if cmd and not (isinstance(cmd, str) and cmd.startswith("#"))
        ]

        # Early return if none of the command strings have length after stripping whitespace
        if not any(cmd.strip() for cmd in commands):
            return

        command_count = sum(
            1 for cmd in commands if cmd.strip() and not cmd.strip().startswith("#")
        )
        prompt = "Run shell command?" if command_count == 1 else "Run shell commands?"
        if not await self.io.confirm_ask(
            prompt,
            subject="\n".join(commands),
            explicit_yes_required=not self.args.yes_always_commands,
            group=group,
            allow_never=True,
        ):
            return

        accumulated_output = ""
        for command in commands:
            command = command.strip()
            if not command or command.startswith("#"):
                continue

            command = self.format_command_with_prefix(command)

            self.io.tool_output()
            self.io.tool_output(f"Running {command}")
            # Add the command to input history
            # self.io.add_to_input_history(f"/run {command.strip()}")
            exit_status, output = await run_cmd_async(
                command,
                self.interrupt_event,
                cwd=self.root,
            )

            if output:
                accumulated_output += f"Output from {command}\n{output}\n"

        self.io.tool_output(accumulated_output)

        if accumulated_output.strip() and await self.io.confirm_ask(
            "Add command output to the chat?", allow_never=True
        ):
            num_lines = len(accumulated_output.strip().splitlines())
            line_plural = "line" if num_lines == 1 else "lines"
            self.io.tool_output(f"Added {num_lines} {line_plural} of output to the chat.")
            return accumulated_output

    def format_command_with_prefix(self, command):
        """
        Format a command with a command prefix.

        If the command prefix contains a {} placeholder, replace it with the command.
        Otherwise, append the command to the prefix with a space.

        Args:
            command (str): The command to format

        Returns:
            str: The formatted command
        """
        command_prefix = None

        if command and getattr(self.args, "command_prefix", None):
            command_prefix = getattr(self.args, "command_prefix", None)

        if not command_prefix:
            return command

        # Check if the prefix contains a {} placeholder
        if "{}" in command_prefix:
            # Replace the {} placeholder with the command
            return command_prefix.replace("{}", command)
        else:
            # Append the command to the prefix with a space
            return f"{command_prefix} {command}"


def _tool_call_to_dict(tc):
    """Normalize a tool call (dict or litellm-shaped object) to a wire-format dict."""
    if isinstance(tc, dict):
        return tc

    if hasattr(tc, "to_dict"):
        return tc.to_dict()

    return tc


def _function_call_to_dict(function_call):
    """Normalize a function call (dict or litellm-shaped Function) to a dict."""
    if isinstance(function_call, dict):
        return function_call

    if hasattr(function_call, "to_dict"):
        return function_call.to_dict()

    return function_call
