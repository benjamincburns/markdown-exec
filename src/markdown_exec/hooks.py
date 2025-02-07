"""Hooks for the `markdown-exec` plugin."""
import functools
import importlib
import json
import traceback
from typing import Any, NamedTuple, Protocol, Union, cast

from markupsafe import Markup

from markdown_exec.formatters.base import ExecutionError, Formatter


class PreSessionHook(Protocol):
    """A protocol for pre-session hooks."""
    def __call__(self, *, formatter: Formatter, session: str, **kwargs: Any) -> Union[dict[str, Any], None]:
        """Call the hook."""
        ...

class SessionHistoryEntry(NamedTuple):
    """A protocol for session history."""
    inputs: dict[str, Any]
    output: Any
    error: Union[Exception, None]

class PostSessionHook(Protocol):
    """A protocol for post-session hooks."""
    def __call__(
        self,
        formatter: Formatter,
        language: str,
        session: str,
        history: list[SessionHistoryEntry],
        **kwargs: Any,
    ) -> None:
        """Call the hook."""
        ...


@functools.cache
def _import_hook(resolution_str: str) -> Union[PreSessionHook, PostSessionHook]:
    """Imports a hook function from a string."""
    module_name, function_name = resolution_str.split(":")
    module = importlib.import_module(module_name)
    if not hasattr(module, function_name):
        raise ValueError(f"Function {function_name} not found in module {module_name}")
    return getattr(module, function_name)


_formatter_by_language: dict[str, Formatter] = {}
_sessions_by_formatter: dict[Formatter, list[str]] = {}
_session_history: dict[Formatter, dict[str, list[SessionHistoryEntry]]] = {}


def hook_formatter(
    *,
    formatter: Formatter,
    language: str,
    pre_session_hooks: list[str],
) -> Formatter:
    """Wraps a formatter to support pre- and post-session hooks."""
    if formatter not in _sessions_by_formatter:
        _sessions_by_formatter[formatter] = []
    if language not in _formatter_by_language:
        _formatter_by_language[language] = formatter

    def wrapped_formatter(**kwargs: Any) -> Markup:
        session = kwargs.get("session", "")
        is_new_session = session == "" and session not in _sessions_by_formatter[formatter]
        if is_new_session:
            if session != "":
              _sessions_by_formatter[formatter].append(session)
            for hook in [_import_hook(hook) for hook in pre_session_hooks]:
                try:
                    result = hook(formatter=formatter, language=language, **kwargs)
                    if result is not None:
                        kwargs.update(result)
                except Exception as e:
                    if not isinstance(e, ExecutionError):
                        raise ExecutionError(traceback.format_exc()) from e
                    raise
        try:
            output = formatter(**kwargs)
            if session != "":
                if formatter not in _session_history:
                    _session_history[formatter] = {}
                if session not in _session_history[formatter]:
                    _session_history[formatter][session] = []
                _session_history[formatter][session].append(
                    SessionHistoryEntry(
                        inputs=dict(language=language, **kwargs),
                        output=output,
                        error=None,
                    ),
                )
            return output  # noqa: TRY300
        except Exception as e:
            if session != "":
                if formatter not in _session_history:
                    _session_history[formatter] = {}
                if session not in _session_history[formatter]:
                    _session_history[formatter][session] = []
                _session_history[formatter][session].append(
                    SessionHistoryEntry(
                        inputs=dict(language=language, session=session, **kwargs),
                        output=output,
                        error=e,
                    ),
                )
            raise
    return wrapped_formatter


def fire_post_session_hooks(
    *,
    post_session_hooks_by_language: dict[str, list[str]],
) -> None:
    """Fires post-session hooks when a session is ended."""
    for language, hook_resolution_strings in post_session_hooks_by_language.items():
        if language not in _formatter_by_language:
            continue
        formatter = _formatter_by_language[language]
        for session in _sessions_by_formatter.get(formatter, []):
            for hook in [_import_hook(hook) for hook in hook_resolution_strings]:
                hook(
                    formatter=formatter,
                    language=language,
                    session=session,
                    history=_session_history[formatter][session],
                )


def pre_session_hook(
    **kwargs: Any,
) -> Union[dict[str, Any], None]:
    """Test hook."""

    if "transform_source" in kwargs:
        source_input, source_output = kwargs["transform_source"](kwargs["code"])
    else:
        source_input = kwargs["code"]
        source_output = kwargs["code"]

    def transform_source(code):
        return (source_input + "\nprint('YOLO!')\n", # this is what executes
                source_output) # this is what is rendered

    return { "transform_source": transform_source }
