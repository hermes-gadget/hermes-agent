"""Shared YAML 1.1 policy for config, manifests, and frontmatter.

Ruamel's native schema includes bare y/n booleans and rejects duplicate keys.
Every operation owns its parser/emitter; instances must not be shared by threads.
"""

from io import StringIO
from typing import Any, IO, overload

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError as YAMLError
from ruamel.yaml.resolver import VersionedResolver


class _Yaml11Resolver(VersionedResolver):
    # Quote strings like "off" without adding a %YAML directive to every config/snippet.
    @property
    def processing_version(self) -> tuple[int, int]:
        return (1, 1)


def _load(document: str | bytes, *, pure: bool) -> Any:
    yaml = YAML(typ="safe", pure=pure)
    yaml.version = (1, 1)
    return yaml.load(document)


def safe_load(stream: str | bytes | IO[str] | IO[bytes]) -> Any:
    """Read standard YAML data; existing configs use YAML 1.1 booleans.

    The pure parser defines what parses: Windows ARM64 has no C extension, and libyaml rejects
    documents the pure parser accepts (``[{url: http://h}]``), so a C rejection is re-read pure.
    """
    document = stream if isinstance(stream, (str, bytes)) else stream.read()
    try:
        return _load(document, pure=False)
    except YAMLError:
        return _load(document, pure=True)


@overload
def safe_dump(
    data: Any, stream: None = None, *, default_flow_style: bool = False,
    sort_keys: bool = True, allow_unicode: bool = True, width: int = 80,
) -> str: ...


@overload
def safe_dump(
    data: Any, stream: IO[str], *, default_flow_style: bool = False,
    sort_keys: bool = True, allow_unicode: bool = True, width: int = 80,
) -> None: ...


def safe_dump(
    data: Any,
    stream: IO[str] | None = None,
    *,
    default_flow_style: bool = False,
    sort_keys: bool = True,
    allow_unicode: bool = True,
    width: int = 80,
) -> str | None:
    """Write standard YAML data with readable Unicode and indented block lists."""
    # The C emitter ignores sequence offsets and escapes astral Unicode.
    yaml = YAML(typ="safe", pure=True)
    yaml.Resolver = _Yaml11Resolver
    yaml.default_flow_style = default_flow_style
    yaml.allow_unicode = allow_unicode
    yaml.width = width
    yaml.sort_base_mapping_type_on_output = sort_keys
    yaml.indent(mapping=2, sequence=4, offset=2)
    if stream is not None:
        yaml.dump(data, stream)
        return None
    output = StringIO()
    yaml.dump(data, output)
    return output.getvalue()


# ruamel's emitter can change a double-quoted value when it folds a long line right after an
# escaped backslash (``D:\\Cent…`` → ``D:\\`` + bare newline): the fold reloads as a literal space
# and a no-op save mutates the stored value (#119844). Config writes must be value-preserving, so
# every round-trip emitter in the tree keeps scalars on one line instead of folding (``None``
# does NOT disable folding on 0.18.x; only a large width does).
ROUNDTRIP_YAML_WIDTH = 2**31 - 1


def _editor(width: int, sequence: int, offset: int) -> YAML:
    """A fresh round-trip editor emitting at *width* with the given sequence indent."""
    yaml = YAML(typ="rt")
    yaml.width = width
    yaml.Resolver = _Yaml11Resolver
    yaml.preserve_quotes = True
    yaml.allow_unicode = True
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=sequence, offset=offset)
    return yaml


def _dump(editor: YAML, data: Any) -> str:
    out = StringIO()
    editor.dump(data, out)
    return out.getvalue()


def roundtrip_yaml() -> YAML:
    """Create a fresh comment/quote-preserving editor for user-authored YAML."""
    return _editor(ROUNDTRIP_YAML_WIDTH, 4, 2)


# The emitter settings a config in the wild may have been authored with. ruamel cannot restore a
# multi-line plain scalar's fold points once loaded, so any fixed width reflows every long value
# it did not touch (a one-item ``plugins.enabled`` edit rewrote ~988 lines of a 120-column config).
# Probing these candidates against the document itself finds the settings that reproduce it.
# Indent first: the previous hard-coded 4/2 is tried before the authored zero-indent style, so a
# file this writer produced matches on the first candidate instead of the last.
_CANDIDATE_WIDTHS = (120, 100, 80, 132, 160, 2**31 - 1)
_CANDIDATE_INDENTS = ((4, 2), (2, 0), (2, 2))


def _candidate_widths(text: str) -> tuple[int, ...]:
    """Widths worth probing, widest-that-still-fits first.

    Every width at or above the document's longest line folds identically (nothing exceeds the
    limit), so one representative replaces the whole tail; the narrower widths can still reflow
    and are probed after it. Without this a 1,800-line config cost eighteen parse/dump passes
    whenever it matched a late candidate.
    """
    longest = max((len(line) for line in text.splitlines()), default=0)
    fitting = [w for w in _CANDIDATE_WIDTHS if w >= longest]
    wide = min(fitting) if fitting else max(_CANDIDATE_WIDTHS)
    return (wide, *(w for w in _CANDIDATE_WIDTHS if w < longest))


def roundtrip_yaml_for(text: str) -> YAML:
    """Editor whose no-op dump best reproduces *text* (byte-identical when possible).

    ruamel cannot restore a multi-line plain scalar's fold points, so a fixed emitter
    setting reflows every long value it did not touch. Probe the document's own style
    once, then hand back the editor that reproduces it; fall back to the existing
    no-fold/4-2 settings when nothing matches (previous behaviour, never worse).
    """
    if not text.strip():
        return roundtrip_yaml()
    best: tuple[int, tuple[int, int, int]] | None = None
    lines = text.splitlines()
    for width in _candidate_widths(text):
        for sequence, offset in _CANDIDATE_INDENTS:
            settings = (width, sequence, offset)
            try:
                editor = _editor(*settings)
                rendered = _dump(editor, editor.load(text))
            except Exception:
                continue
            if rendered == text:
                return _editor(*settings)
            rendered_lines = rendered.splitlines()
            changed = sum(
                1 for a, b in zip(lines, rendered_lines) if a != b
            ) + abs(len(lines) - len(rendered_lines))
            if best is None or changed < best[0]:
                best = (changed, settings)
    return _editor(*best[1]) if best else roundtrip_yaml()
