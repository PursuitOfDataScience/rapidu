"""The rule glyph is decided in one place, because two copies is the bug it had.

`_section_rule`'s own docstring records the original defect:

    Was a hard-coded 78 ASCII dashes, which ignored both the terminal width and
    the ``--ascii`` glyph decision -- so a unicode report carried one unicode
    rule and one ASCII one.

That was fixed by giving each rule the conditional -- and left the *decision*
written twice, in two functions in `report.py`, which is the same shape one step
back: nothing made the second copy follow the first. It now lives on
`ui.Style.rule`, beside `bar_chars` and `partials`, which resolve their glyph sets
the same way.

`ui.box` is a deliberate exception and is pinned as one below: its six border
glyphs are resolved together as a set.
"""

import ast
import pathlib

from rapidu import report, ui

SRC = pathlib.Path(ui.__file__).resolve().parent
#: The two glyphs the decision picks between, as VALUES. Matching on source text
#: is what made a first cut of this file vacuous: the code spells the glyph
#: `"\u2500"` (an escape), and a scan looking for the character never matched it.
RULE_PAIR = {"\u2500", "-"}


def _style(unicode_ok):
    return ui.Style(color=False, unicode_ok=unicode_ok, width=100)


def _rules(unicode_ok):
    """Both rules the report draws, as rendered strings.

    Named explicitly rather than looked up with `hasattr`: a first cut of this
    file guessed `_table_rule`, which does not exist, so the guard silently
    skipped the second site and the file tested half of what it claimed.
    """
    style = _style(unicode_ok)
    rows = ["a" * 40, "b" * 30]
    return style, report._section_rule(style), report._entries_rule(style, rows, "")


class TestBothRulesFollowTheOneDecision:
    def test_both_rules_are_drawn_with_the_styles_glyph(self):
        for unicode_ok in (True, False):
            style, section, entries = _rules(unicode_ok)
            for name, drawn in (("section", section), ("entries", entries)):
                glyphs = set(drawn.strip()) - set(" ")
                assert glyphs == {style.rule}, (unicode_ok, name, sorted(glyphs), style.rule)

    def test_the_two_rules_agree_with_each_other(self):
        """The original defect, stated as a test: one unicode rule, one ASCII."""
        for unicode_ok in (True, False):
            _style_, section, entries = _rules(unicode_ok)
            assert set(section.strip()) == set(entries.strip()), (unicode_ok, section, entries)

    def test_the_glyph_sets_disagree_so_nothing_above_is_vacuous(self):
        """A guard, NOT a control: it reads the property the fix added, so a
        neuter that breaks the property reddens it too. Kept here rather than in
        `TestControls`, where it would have claimed a role it cannot fill."""
        assert _style(True).rule != _style(False).rule

    def test_the_two_glyph_sets_really_reach_both_rendered_rules(self):
        _s, uni_sec, uni_ent = _rules(True)
        _s, asc_sec, asc_ent = _rules(False)
        assert not uni_sec.isascii() and not uni_ent.isascii(), (uni_sec, uni_ent)
        assert asc_sec.isascii() and asc_ent.isascii(), (asc_sec, asc_ent)

    def test_no_second_copy_of_the_decision_outside_the_style(self):
        """A third copy must not be able to appear quietly.

        Checked as source rather than behaviour: a fresh copy that happens to
        agree today is exactly what the docstring above says went wrong, and it
        renders identically until one side is changed.
        """
        offenders = {}
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.IfExp):
                    continue
                # The decision, as values: a conditional choosing between the two
                # rule glyphs. Spelling-independent, so `"\u2500"` and a literal
                # box-drawing character are both caught.
                picked = {
                    side.value
                    for side in (node.body, node.orelse)
                    if isinstance(side, ast.Constant) and isinstance(side.value, str)
                }
                if picked != RULE_PAIR:
                    continue
                fn = _enclosing(tree, node)
                # `Style.rule` IS the one home, and `box` resolves its whole
                # border set at once -- both on purpose.
                if path.name == "ui.py" and fn in {"rule", "box"}:
                    continue
                offenders.setdefault(path.name, []).append((node.lineno, fn))
        assert offenders == {}, offenders


def _enclosing(tree, target):
    best = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.lineno <= target.lineno <= (
            node.end_lineno or node.lineno
        ):
            best = node.name
    return best


class TestControls:
    """None of these depends on where the decision lives, so all hold either way."""

    def test_the_rule_still_has_its_floor_and_its_ceiling(self):
        # Width behaviour, not glyph behaviour: at least 20 columns, and never
        # wider than the terminal.
        narrow = report._section_rule(ui.Style(color=False, unicode_ok=False, width=5))
        wide = report._section_rule(ui.Style(color=False, unicode_ok=False, width=400))
        assert len(narrow) == 20, len(narrow)
        assert 20 <= len(wide) <= 400

    def test_box_still_resolves_its_own_border_set(self):
        """The documented exception. Holds with the fix in or out."""
        drawn = ui.box(["x"], ui.Style(color=False, unicode_ok=False, width=40))
        text = "\n".join(drawn) if isinstance(drawn, list) else str(drawn)
        assert "+" in text and "\u256d" not in text, text
