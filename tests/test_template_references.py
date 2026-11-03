"""Jinja references and render_template literals must name a real template."""

import ast
import unittest
from pathlib import Path

from jinja2 import TemplateNotFound, meta

from app import create_app

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"
APP = ROOT / "app"


def template_names():
    names = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        names.append(path.relative_to(TEMPLATES).as_posix())
    return names


def referenced_names(env, source):
    parsed = env.parse(source)
    return list(meta.find_referenced_templates(parsed))


def literal_render_names(tree):
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        is_render = (
            isinstance(func, ast.Name) and func.id == "render_template"
        ) or (
            isinstance(func, ast.Attribute) and func.attr == "render_template"
        )
        if not is_render:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            found.append(arg.value)
    return found


def unresolved_names(env, names):
    missing = []
    dynamic = 0
    for name in names:
        if name is None:
            dynamic += 1
            continue
        try:
            env.get_template(name)
        except TemplateNotFound:
            missing.append(name)
    return missing, dynamic


def render_literals_under(root):
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.extend(literal_render_names(tree))
    return found


class TestTemplateReferences(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = create_app().jinja_env

    def test_template_references_resolve(self):
        missing = []
        dynamic = 0
        for name in template_names():
            source = (TEMPLATES / name).read_text(encoding="utf-8")
            unresolved, skipped = unresolved_names(
                self.env, referenced_names(self.env, source)
            )
            dynamic += skipped
            for ref in unresolved:
                missing.append("%s -> %s" % (name, ref))
        message = "unresolved template references: %s" % missing
        if dynamic:
            message += " (%s dynamic names ignored)" % dynamic
        self.assertEqual(missing, [], message)

    def test_render_template_literals_resolve(self):
        names = render_literals_under(APP)
        self.assertTrue(set(names))
        missing, dynamic = unresolved_names(self.env, names)
        message = "unresolved render_template names: %s" % missing
        if dynamic:
            message += " (%s dynamic names ignored)" % dynamic
        self.assertEqual(missing, [], message)

    def test_scanners_report_unresolved_samples(self):
        tree = ast.parse('render_template("does_not_exist.html")\n')
        missing, _dynamic = unresolved_names(self.env, literal_render_names(tree))
        self.assertEqual(missing, ["does_not_exist.html"])

        refs = referenced_names(self.env, '{% extends "missing_base.html" %}')
        missing, _dynamic = unresolved_names(self.env, refs)
        self.assertEqual(missing, ["missing_base.html"])
