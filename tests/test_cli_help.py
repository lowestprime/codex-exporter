"""Help covers the actual parser and displays resolved preferences accurately."""
from __future__ import annotations

import argparse
import ast
from pathlib import Path

from codex_exporter.cli_help import HELP, configure_help


def test_catalog_covers_every_cli_flag_including_legacy_switches():
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'Export-CodexThread.py').read_text())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'add_argument' and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.append(first.value.lstrip('-'))
    assert names
    assert not set(names).difference(HELP), 'Add a complete, accurate help explanation for each new argument.'


def test_help_starts_with_version_and_uses_effective_defaults_without_mutation():
    parser = argparse.ArgumentParser(prog='codex-export')
    parser.add_argument('--archive-method', default=None, choices=['LZMA', 'Deflate'])
    parser.add_argument('--archive-level', type=int, default=None)
    parser.add_argument('--filename-template')
    parser.add_argument('--no-ui-style', dest='ui_style', action='store_false', default=True)
    configure_help(parser, version='0.5.0', defaults={
        'archive_method': 'LZMA', 'archive_level': 9, 'filename_template': '100%_{title}.md',
        'ui_style': False})
    text = parser.format_help()
    assert text.startswith('Codex Exporter 0.5.0\n\nusage:')
    assert 'Current default (archive_method): LZMA.' in text
    assert 'Current default (archive_level): 9.' in text
    assert '100%_{title}.md' in text
    assert 'Current default (ui_style): off.' in ' '.join(text.split())
    assert 'Accepted: LZMA, Deflate.' in text
    assert parser.parse_args([]).archive_method is None
    assert parser.parse_args([]).ui_style is True


def test_help_shows_selector_inputs_and_keyboard_instructions():
    parser = argparse.ArgumentParser()
    parser.add_argument('--since')
    parser.add_argument('--page-size', type=int, default=0)
    parser.add_argument('--fix-mojibake', action='store_true', help=argparse.SUPPRESS)
    configure_help(parser, version='0.5.0')
    text = parser.format_help()
    assert 'turn:N, id:ID, text:WORDS, time:ISO' in text
    assert 'Enter/Space/PgDn' in text and 'Home/End' in text
    assert '--fix-mojibake' in text and 'Compatibility flag' in text
    assert 'Current default (page_size): 0.' in ' '.join(text.split())


def test_paired_flags_show_one_effective_default_even_with_argparse_action_defaults():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fast', action='store_true', default=None)
    parser.add_argument('--no-fast', dest='fast', action='store_false')
    parser.add_argument('--progress', action='store_true', default=None)
    parser.add_argument('--no-progress', dest='progress', action='store_false')
    parser.add_argument('--work-dir', default='')
    configure_help(parser, version='0.5.0', defaults={'progress': True})
    text = ' '.join(parser.format_help().split())
    assert text.count('Current default (fast): automatic.') == 2
    assert text.count('Current default (progress): on.') == 2
    assert 'Current default (work_dir): output-directory volume.' in text
    assert parser.parse_args([]).fast is None
