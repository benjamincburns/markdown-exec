"""Hooks for the `markdown-exec` plugin."""
import functools
import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from types import ModuleType
from typing import Any, NamedTuple, Protocol, Union

from markupsafe import Markup

from markdown_exec.formatters.base import Formatter


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

def _import_module_from_cwd(module_name: str) -> ModuleType:
    module_path = os.path.abspath(
        os.path.join(os.getcwd(), module_name.replace(".", os.sep) + ".py")
    )

    if not os.path.isfile(module_path):
        raise FileNotFoundError(f"Module '{module_name}' not found at {module_path}")

    module_dir = os.path.dirname(module_path)

    pop_dir = False
    # Ensure the module's directory is in sys.path to resolve dependencies
    if module_dir not in sys.path:
        pop_dir = True
        sys.path.insert(0, module_dir)

    try:
        spec = spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load spec for module '{module_name}' - tried to import from {module_path}")

        module = module_from_spec(spec)
        sys.modules[module_name] = module  # Register in sys.modules
        spec.loader.exec_module(module)  # Execute the module

        return module
    finally:
        if pop_dir:
            sys.path.pop(0)


@functools.cache
def _import_hook(resolution_str: str) -> Union[PreSessionHook, PostSessionHook]:
    """Imports a hook function from a string."""
    module_name, function_name = resolution_str.split(":")
    module = _import_module_from_cwd(module_name)
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
        new_kwargs = kwargs.copy()
        new_kwargs["extra"] = kwargs.get("extra", {}).copy()
        session = kwargs.get("session", "")

        is_new_session = session == "" or session not in _sessions_by_formatter[formatter]

        if is_new_session:
            if session != "":
              _sessions_by_formatter[formatter].append(session)

            for hook in [_import_hook(hook) for hook in pre_session_hooks]:
                result = hook(formatter=formatter, language=language, **dict(new_kwargs))
                if result is not None:
                    new_kwargs.update(result)
        try:
            output = formatter(**new_kwargs)
            if session != "":
                if formatter not in _session_history:
                    _session_history[formatter] = {}
                if session not in _session_history[formatter]:
                    _session_history[formatter][session] = []
                _session_history[formatter][session].append(
                    SessionHistoryEntry(
                        inputs=dict(language=language, **new_kwargs),
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
                        inputs=dict(language=language, session=session, **new_kwargs),
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
    _session_history.clear()
    _sessions_by_formatter.clear()
    _formatter_by_language.clear()


def pre_session_hook(
    **kwargs: Any,
) -> Union[dict[str, Any], None]:
    """Test hook."""

    if "transform_source" in kwargs:
        source_input, source_output = kwargs["transform_source"](kwargs["code"])
    else:
        source_input = kwargs["code"]
        source_output = kwargs["code"]

    def transform_source(_code: str) -> tuple[str, str]:
        return (source_input + "\nprint('YOLO!')\n", # this is what executes
                source_output) # this is what is rendered

    return { "transform_source": transform_source }
