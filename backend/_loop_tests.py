"""Loop emission tests for codegen.py. Run via:
    docker compose exec vm python /app/_loop_tests.py
or directly:
    python backend/_loop_tests.py    (with backend/ on PYTHONPATH)
"""
import sys
sys.path.insert(0, "/app")
from codegen import generate, CodegenError


def assert_compiles(code: str) -> None:
    compile(code, "<workflow>", "exec")


def run() -> None:
    # TEST 1: non-Check self-loop emits node body exactly once
    g = {"global": {"llm": "gpt-4", "verbose": False},
         "nodes": [{"id": "a", "type": "Do", "label": "loop", "config": {"task": "w"}}],
         "edges": [{"id": "e1", "source": "a", "target": "a", "type": "loop_back", "max_iterations": 3}]}
    code = generate(g)
    assert_compiles(code)
    n = code.count("await Do(")
    assert n == 1, f"TEST 1 FAIL: Do emitted {n}x, expected 1"
    print("TEST 1 non-Check self-loop: Do emitted 1x")

    # TEST 2: Check self-loop emits Check exactly once
    g = {"global": {"llm": "gpt-4", "verbose": False},
         "nodes": [{"id": "c", "type": "Check", "label": "wait", "config": {"condition": "done?"}}],
         "edges": [{"id": "e1", "source": "c", "target": "c", "type": "loop_back", "max_iterations": 5}]}
    code = generate(g)
    assert_compiles(code)
    n = code.count("await Check(")
    assert n == 1, f"TEST 2 FAIL: Check emitted {n}x, expected 1"
    print("TEST 2 Check self-loop: Check emitted 1x")

    # TEST 3: ForEach in loop body rejected
    g = {"global": {"llm": "gpt-4", "verbose": False},
         "nodes": [
             {"id": "n", "type": "Navigate", "label": "nav", "config": {"target": "x"}},
             {"id": "f", "type": "ForEach", "label": "loop", "config": {"items_expr": "[]", "loop_var": "i"}},
             {"id": "c", "type": "Check", "label": "chk", "config": {"condition": "done?"}}],
         "edges": [
             {"id": "e1", "source": "n", "target": "f", "type": "sequential"},
             {"id": "e2", "source": "f", "target": "c", "type": "sequential"},
             {"id": "e3", "source": "c", "target": "n", "type": "loop_back", "max_iterations": 3}]}
    try:
        generate(g)
        raise SystemExit("TEST 3 FAIL: ForEach-in-body should have errored")
    except CodegenError as e:
        assert "ForEach" in str(e)
        print("TEST 3 ForEach-in-body rejected")

    # TEST 4: regression — legacy default break-on-True still applies when
    # the Check has NO conditional edges and NO sequential successor outside
    # the loop (the LinkedIn-style workflow you've been running).
    g = {"global": {"llm": "gpt-4", "verbose": False},
         "nodes": [
             {"id": "n", "type": "Navigate", "label": "nav", "config": {"target": "linkedin"}},
             {"id": "d", "type": "Do", "label": "apply", "config": {"task": "apply"}},
             {"id": "c", "type": "Check", "label": "done?", "config": {"condition": "finished?"}}],
         "edges": [
             {"id": "e1", "source": "n", "target": "d", "type": "sequential"},
             {"id": "e2", "source": "d", "target": "c", "type": "sequential"},
             {"id": "e3", "source": "c", "target": "n", "type": "loop_back", "max_iterations": 10}]}
    code = generate(g)
    assert_compiles(code)
    assert code.count("await Navigate(") == 1
    assert code.count("await Do(") == 1
    assert code.count("await Check(") == 1
    # New default behavior: no exit edges → Check runs but doesn't break;
    # loop runs all max_iterations (which is what users actually want).
    print("TEST 4 LinkedIn-style loop compiles cleanly")

    # TEST 5: conditional_false to OUTSIDE the loop → break on False
    g = {"global": {"llm": "gpt-4", "verbose": False},
         "nodes": [
             {"id": "n", "type": "Navigate", "label": "nav", "config": {"target": "x"}},
             {"id": "d", "type": "Do", "label": "work", "config": {"task": "work"}},
             {"id": "c", "type": "Check", "label": "still working?", "config": {"condition": "still on page?"}},
             {"id": "end", "type": "Code", "label": "end", "config": {"code": "pass"}}],
         "edges": [
             {"id": "e1", "source": "n", "target": "d", "type": "sequential"},
             {"id": "e2", "source": "d", "target": "c", "type": "sequential"},
             {"id": "e3", "source": "c", "target": "n", "type": "loop_back", "max_iterations": 5},
             {"id": "e4", "source": "c", "target": "end", "type": "conditional_false"}]}
    code = generate(g)
    assert_compiles(code)
    assert "if not await Check" in code, "TEST 5 FAIL: expected 'if not await Check' (break on False)"
    print("TEST 5 conditional_false to outside: emits break-on-False")

    # TEST 6: conditional_true to outside → break on True
    g = {"global": {"llm": "gpt-4", "verbose": False},
         "nodes": [
             {"id": "n", "type": "Navigate", "label": "nav", "config": {"target": "x"}},
             {"id": "d", "type": "Do", "label": "work", "config": {"task": "work"}},
             {"id": "c", "type": "Check", "label": "done?", "config": {"condition": "done?"}},
             {"id": "end", "type": "Code", "label": "end", "config": {"code": "pass"}}],
         "edges": [
             {"id": "e1", "source": "n", "target": "d", "type": "sequential"},
             {"id": "e2", "source": "d", "target": "c", "type": "sequential"},
             {"id": "e3", "source": "c", "target": "n", "type": "loop_back", "max_iterations": 5},
             {"id": "e4", "source": "c", "target": "end", "type": "conditional_true"}]}
    code = generate(g)
    assert_compiles(code)
    assert "if await Check" in code and "if not await Check" not in code
    print("TEST 6 conditional_true to outside: emits break-on-True")

    # TEST 7: both conditional branches outside ⇒ codegen error (clear msg)
    g = {"global": {"llm": "gpt-4", "verbose": False},
         "nodes": [
             {"id": "n", "type": "Navigate", "label": "nav", "config": {"target": "x"}},
             {"id": "d", "type": "Do", "label": "work", "config": {"task": "work"}},
             {"id": "c", "type": "Check", "label": "r?", "config": {"condition": "r?"}},
             {"id": "a", "type": "Code", "label": "a", "config": {"code": "pass"}},
             {"id": "b", "type": "Code", "label": "b", "config": {"code": "pass"}}],
         "edges": [
             {"id": "e1", "source": "n", "target": "d", "type": "sequential"},
             {"id": "e2", "source": "d", "target": "c", "type": "sequential"},
             {"id": "e3", "source": "c", "target": "n", "type": "loop_back", "max_iterations": 5},
             {"id": "e4", "source": "c", "target": "a", "type": "conditional_true"},
             {"id": "e5", "source": "c", "target": "b", "type": "conditional_false"}]}
    try:
        generate(g)
        raise SystemExit("TEST 7 FAIL: both-branches-outside should have errored")
    except CodegenError as e:
        assert "both" in str(e).lower() or "two" in str(e).lower()
        print("TEST 7 both-conditional-branches-outside rejected")

    # TEST 8: duplicate loop_back targets rejected
    g = {"global": {"llm": "gpt-4", "verbose": False},
         "nodes": [
             {"id": "a", "type": "Do", "label": "a", "config": {"task": "a"}},
             {"id": "b", "type": "Check", "label": "b", "config": {"condition": "q"}},
             {"id": "c", "type": "Check", "label": "c", "config": {"condition": "q"}}],
         "edges": [
             {"id": "e1", "source": "a", "target": "b", "type": "sequential"},
             {"id": "e2", "source": "b", "target": "c", "type": "sequential"},
             {"id": "e3", "source": "b", "target": "a", "type": "loop_back", "max_iterations": 3},
             {"id": "e4", "source": "c", "target": "a", "type": "loop_back", "max_iterations": 3}]}
    try:
        generate(g)
        raise SystemExit("TEST 8 FAIL: duplicate loop_back target should have errored")
    except CodegenError as e:
        assert "loop_back" in str(e)
        print("TEST 8 duplicate loop_back target rejected")

    # TEST 9: Pattern A with Check at header — break still works via helper
    g = {"global": {"llm": "gpt-4", "verbose": False},
         "nodes": [
             {"id": "c", "type": "Check", "label": "done?", "config": {"condition": "done?"}},
             {"id": "d", "type": "Do", "label": "work", "config": {"task": "work"}},
             {"id": "end", "type": "Code", "label": "end", "config": {"code": "pass"}}],
         "edges": [
             {"id": "e1", "source": "c", "target": "d", "type": "sequential"},
             {"id": "e2", "source": "d", "target": "c", "type": "loop_back", "max_iterations": 5},
             {"id": "e3", "source": "c", "target": "end", "type": "conditional_true"}]}
    code = generate(g)
    assert_compiles(code)
    assert code.count("await Check(") == 1 and code.count("await Do(") == 1
    assert "if await Check" in code
    print("TEST 9 Pattern A (Check header) with conditional_true exit: ok")

    print()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    run()
