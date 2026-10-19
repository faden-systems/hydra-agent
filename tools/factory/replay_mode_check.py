#!/usr/bin/env python3
"""Does an acceptance script implement a --replay mode?  python tools/factory/replay_mode_check.py loops/<id>.acceptance.py
Exit 0 when the script's code (not a comment, docstring or help text) handles the flag: a positional '--replay'
argument of an add_argument(...) call, or a comparison / membership test against the literal '--replay'
(e.g. `'--replay' in sys.argv`, `sys.argv[1] == '--replay'`, `sys.argv[1] in ('--replay', '--replay-strict')`). Exit 1 otherwise; exit 2 when the file does not parse."""
import ast, sys

def has_replay_mode(src):
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_argument":
            if any(isinstance(a, ast.Constant) and a.value == "--replay" for a in n.args):
                return True
        if isinstance(n, ast.Compare):
            operands = [n.left] + list(n.comparators)
            for a in operands:  # `x == '--replay'`, `'--replay' in argv`, and `x in ('--replay', '--replay-strict')`
                members = a.elts if isinstance(a, (ast.Tuple, ast.List, ast.Set)) else [a]
                if any(isinstance(m, ast.Constant) and m.value == "--replay" for m in members):
                    return True
    return False

if __name__ == "__main__":
    path = sys.argv[1]
    try:
        ok = has_replay_mode(open(path, encoding="utf-8").read())
    except SyntaxError as e:
        print(f"{path}: does not parse: {e}"); sys.exit(2)
    print(f"{path}: {'has' if ok else 'has no'} --replay mode (code-level check)"); sys.exit(0 if ok else 1)
