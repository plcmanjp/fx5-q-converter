"""Guard product literals, including diagnostic text rendered outside Qt widgets."""
import ast
from pathlib import Path
import unittest


class DisplayTextTests(unittest.TestCase):
    def test_product_string_literals_do_not_use_prohibited_punctuation(self):
        source = Path(__file__).resolve().parents[1] / "src"
        prohibited = set("\u2014\u2013\u2015\u00b7\u30fb\u2022")
        found = []
        for path in sorted(source.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            # Module/class/function docstrings are internal operating text.
            docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                          if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                          and node.body and isinstance(node.body[0], ast.Expr)
                          and isinstance(node.body[0].value, ast.Constant)
                          and isinstance(node.body[0].value.value, str)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    marks = prohibited.intersection(node.value)
                    if marks:
                        found.append((path.relative_to(source).as_posix(), node.lineno,
                                      sorted(f"U+{ord(mark):04X}" for mark in marks)))
        self.assertEqual([], found)


if __name__ == "__main__":
    unittest.main()
