"""This module contains an optional plugin for MkDocs."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mkdocs.config import config_options
from mkdocs.config.base import BaseConfigOption, Config, ValidationError
from mkdocs.exceptions import PluginError
from mkdocs.plugins import BasePlugin
from mkdocs.utils import write_file

from markdown_exec import formatter, formatters, validator
from markdown_exec.formatters.jupyter import _shutdown_kernels
from markdown_exec.hooks import _import_module_from_cwd, fire_post_session_hooks, hook_formatter
from markdown_exec.logger import patch_loggers
from markdown_exec.rendering import MarkdownConverter, markdown_config

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from jinja2 import Environment
    from mkdocs.config.defaults import MkDocsConfig
    from mkdocs.structure.files import Files

try:
    __import__("pygments_ansi_color")
except ImportError:
    ansi_ok = False
else:
    ansi_ok = True

_formatters = dict(formatters)


class _LoggerAdapter(logging.LoggerAdapter):
    def __init__(self, prefix: str, logger: logging.Logger) -> None:
        super().__init__(logger, {})
        self.prefix = prefix

    def process(self, msg: str, kwargs: MutableMapping[str, Any]) -> tuple[str, MutableMapping[str, Any]]:
        return f"{self.prefix}: {msg}", kwargs


def _get_logger(name: str) -> _LoggerAdapter:
    logger = logging.getLogger(f"mkdocs.plugins.{name}")
    return _LoggerAdapter(name.split(".", 1)[0], logger)


patch_loggers(_get_logger)

class Hook(BaseConfigOption[str]):
    """A config option that validates a hook definition."""

    def __init__(self):
        """Initialize the config option."""
        self._default = None
        super().__init__()

    def run_validation(self, value: object, /) -> str:
        """Validate the value."""
        if not isinstance(value, str):
            raise ValidationError(f"Expected type: str but received: {type(value)}")
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_\.]*:[a-zA-Z_][a-zA-Z0-9_]*$", value):
            raise ValidationError(
                f"Expected a valid hook definition, got: {value}. Valid hook "
                "definitions are of the form: "
                "'module_import_string:function_name' (without quotes). For"
                "example: 'markdown_exec.hooks:pre_session'",
            )

        module_name, function_name = value.split(":")
        module = None
        module = _import_module_from_cwd(module_name)

        if module is not None and not hasattr(module, function_name):
            raise ValidationError(
                f"The module {module_name} could not be imported. Please "
                "check the spelling of the module import string.",
            )

        if module is not None and not hasattr(module, function_name):
            raise ValidationError(
                f"The function {function_name} could not be found in the module "
                f"{module_name}. Please check the spelling of the function name.",
            )

        return cast(str, value)


class LanguageHookConfig(Config):
    """Defines the set of hooks for a language."""
    pre_session = config_options.ListOfItems(Hook(), default=[])
    post_session = config_options.ListOfItems(Hook(), default=[])


class MarkdownExecPluginConfig(Config):
    """Configuration of the plugin (for `mkdocs.yml`)."""

    ansi = config_options.Choice(("auto", "off", "required", True, False), default="auto")
    """Whether the `ansi` extra is required when installing the package."""
    languages = config_options.ListOfItems(
        config_options.Choice(formatters.keys()),
        default=list(formatters.keys()),
    )
    """Which languages to enabled the extension for."""
    hooks = config_options.DictOfItems(
        config_options.SubConfig(LanguageHookConfig, validate=True),
        default={},
    )
    """Which hooks to run."""

class MarkdownExecPlugin(BasePlugin[MarkdownExecPluginConfig]):
    """MkDocs plugin to easily enable custom fences for code blocks execution."""

    def on_config(self, config: MkDocsConfig) -> MkDocsConfig | None:
        """Configure the plugin.

        Hook for the [`on_config` event](https://www.mkdocs.org/user-guide/plugins/#on_config).
        In this hook, we add custom fences for all the supported languages.

        We also save the Markdown extensions configuration
        into [`markdown_config`][markdown_exec.rendering.markdown_config].

        Arguments:
            config: The MkDocs config object.

        Returns:
            The modified config.
        """
        if "pymdownx.superfences" not in config["markdown_extensions"]:
            message = "The 'markdown-exec' plugin requires the 'pymdownx.superfences' Markdown extension to work."
            raise PluginError(message)
        if self.config.ansi in ("required", True) and not ansi_ok:
            raise PluginError(
                "The configuration for the 'markdown-exec' plugin requires "
                "that it is installed with the 'ansi' extra. "
                "Install it with 'pip install markdown-exec[ansi]'.",
            )
        self.mkdocs_config_dir = os.getenv("MKDOCS_CONFIG_DIR")
        os.environ["MKDOCS_CONFIG_DIR"] = os.path.dirname(config["config_file_path"])
        self.languages = self.config.languages
        mdx_configs = config.setdefault("mdx_configs", {})
        superfences = mdx_configs.setdefault("pymdownx.superfences", {})
        custom_fences = superfences.setdefault("custom_fences", [])
        for language in self.languages:
            if language not in self.config.hooks:
                self.config.hooks[language] = LanguageHookConfig()
            formatters[language] = hook_formatter(
                formatter=_formatters[language],
                language=language,
                pre_session_hooks=self.config.hooks[language].pre_session,
            )
            if not any(fence["name"] == language for fence in custom_fences):
                custom_fences.append(
                    {
                        "name": language,
                        "class": language,
                        "validator": validator,
                        "format": formatter,
                    },
                )
        markdown_config.save(config.markdown_extensions, config.mdx_configs)
        return config

    def on_env(  # noqa: D102
        self,
        env: Environment,
        *,
        config: MkDocsConfig,
        files: Files,  # noqa: ARG002
    ) -> Environment | None:
        if self.config.ansi in ("required", True) or (self.config.ansi == "auto" and ansi_ok):
            self._add_css(config, "ansi.css")
        if "pyodide" in self.languages:
            self._add_css(config, "pyodide.css")
            self._add_js(config, "pyodide.js")
        return env

    def on_post_build(self, *, config: MkDocsConfig) -> None:  # noqa: D102, ARG002
        fire_post_session_hooks(
            post_session_hooks_by_language={
                key: hooks.post_session or []
                for key, hooks in self.config.hooks.items()
            },
        )
        MarkdownConverter.counter = 0
        markdown_config.reset()
        if self.mkdocs_config_dir is None:
            os.environ.pop("MKDOCS_CONFIG_DIR", None)
        else:
            os.environ["MKDOCS_CONFIG_DIR"] = self.mkdocs_config_dir

        _shutdown_kernels()

    def _add_asset(self, config: MkDocsConfig, asset_file: str, asset_type: str) -> None:
        asset_filename = f"assets/_markdown_exec_{asset_file}"
        asset_content = Path(__file__).parent.joinpath(asset_file).read_text()
        write_file(asset_content.encode("utf-8"), os.path.join(config.site_dir, asset_filename))
        config[f"extra_{asset_type}"].insert(0, asset_filename)

    def _add_css(self, config: MkDocsConfig, css_file: str) -> None:
        self._add_asset(config, css_file, "css")

    def _add_js(self, config: MkDocsConfig, js_file: str) -> None:
        self._add_asset(config, js_file, "javascript")
