"""Streamlit 启动约束测试：确保 set_page_config 是最早的 Streamlit 命令

复现并验证该报错场景：
  "StreamlitSetPageConfigMustBeFirstCommandError: set_page_config() can only be
   called once per app page, and must be called as the first Streamlit command."

原因：脚本在 st.set_page_config() 之前就执行了其他 st.* 命令
（例如 with st.sidebar: / st.selectbox / st.session_state / st.error 等）。

本测试通过两种方式防止回归：
  1. 静态检查（AST）：main.py 中最早的 st.* 调用必须是 set_page_config，且仅出现一次；
  2. 运行时检查（Streamlit AppTest）：实际执行脚本，断言无异常抛出。

运行：python tests/test_app_startup.py
"""
import ast
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

MAIN_PY = os.path.join(project_root, "src", "main.py")

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))


def _walk_st_calls(tree):
    """收集所有形如 st.<attr>(...) 的调用，返回 [(lineno, attr), ...] 升序"""
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and f.value.id == "st"):
                calls.append((node.lineno, f.attr))
    calls.sort(key=lambda x: x[0])
    return calls


def test_set_page_config_is_first_and_single():
    """静态检查：set_page_config 必须是最早的 st 调用，且仅一次"""
    src = open(MAIN_PY, encoding="utf-8").read()
    tree = ast.parse(src)

    calls = _walk_st_calls(tree)
    check("main.py 中存在 st.* 调用", len(calls) > 0, f"{len(calls)} 处")

    setpage_calls = [c for c in calls if c[1] == "set_page_config"]
    check("set_page_config 恰好出现一次", len(setpage_calls) == 1,
          f"出现 {len(setpage_calls)} 次：{[l for l, _ in setpage_calls]}")

    if calls:
        first_attr = calls[0][1]
        check("最早的 st 调用是 set_page_config", first_attr == "set_page_config",
              f"最早为 st.{first_attr}()（第 {calls[0][0]} 行）")

    if setpage_calls:
        sp_line = setpage_calls[0][0]
        earlier = [c for c in calls if c[0] < sp_line]
        # 允许导入语句等，但不应有 st.* 运行时调用
        earlier_runtime = [c for c in earlier if c[1] != "set_page_config"]
        check("set_page_config 之前没有其他 st.* 命令",
              len(earlier_runtime) == 0,
              f"之前有：{[(l, a) for l, a in earlier_runtime][:5]}")


def test_no_top_level_st_sidebar_before_config():
    """静态检查：with st.sidebar 不应出现在 set_page_config 之前"""
    src = open(MAIN_PY, encoding="utf-8").read()
    tree = ast.parse(src)

    sp_line = None
    sidebar_lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and f.value.id == "st"):
                if f.attr == "set_page_config" and sp_line is None:
                    sp_line = node.lineno
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if (isinstance(ctx, ast.Attribute) and isinstance(ctx.value, ast.Name)
                        and ctx.value.id == "st" and ctx.attr == "sidebar"):
                    sidebar_lines.append(node.lineno)

    if sp_line:
        bad = [l for l in sidebar_lines if l < sp_line]
        check("with st.sidebar 均在 set_page_config 之后",
              len(bad) == 0, f"之前有：{bad}")
    # 否则由上一个用例覆盖


def _container_kind_with_stmt(node):
    """判断 with 语句是否为 Streamlit 容器上下文，返回 'expander'|'popover'|None

    注意：`with st.expander("...") as box:` 的 context_expr 是 ast.Call
    （st.expander(...)），不是 ast.Attribute。
    """
    if not isinstance(node, ast.With):
        return None
    for item in node.items:
        ctx = item.context_expr
        if isinstance(ctx, ast.Call):
            f = ctx.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id == "st" and f.attr in ("expander", "popover"):
                return f.attr
    return None


def test_no_nested_expander():
    """静态检查：禁止 expander 嵌套 expander

    复现并验证该报错场景：
      "StreamlitAPIException: Expanders may not be nested inside other expanders."
    Streamlit 明确禁止该嵌套；应改用 st.popover / st.container 等可嵌套容器。

    实现：深度优先遍历 AST，维护「当前所处的 Streamlit 容器栈」。
    遇到 with st.expander 时，若栈中已存在 expander，则判为违规。
    """
    src = open(MAIN_PY, encoding="utf-8").read()
    tree = ast.parse(src)

    violations = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []  # 元素：'expander' | 'popover' | None

        def generic_visit(self, node):
            kind = _container_kind_with_stmt(node)
            if kind is not None:
                if kind == "expander" and "expander" in self.stack:
                    violations.append(node.lineno)
                self.stack.append(kind)
                for child in ast.iter_child_nodes(node):
                    self.visit(child)
                self.stack.pop()
                return
            super().generic_visit(node)

    Visitor().visit(tree)

    check("main.py 中不存在 expander 嵌套 expander",
          len(violations) == 0,
          f"违规行：{violations}" if violations else "无嵌套")


def test_apptest_runs_without_exception():
    """运行时检查：AppTest 实际执行脚本，断言无异常"""
    try:
        from streamlit.testing.v1 import AppTest
    except Exception as e:
        check("Streamlit AppTest 可用", False, f"{type(e).__name__}: {e}")
        return

    try:
        at = AppTest.from_file(MAIN_PY, default_timeout=90)
        at.run()
    except Exception as e:
        check("AppTest 执行脚本成功", False, f"{type(e).__name__}: {e}")
        return

    check("AppTest 执行脚本成功", True)
    if at.exception:
        msgs = [str(e.value) for e in at.exception]
        check("脚本运行无异常抛出", False, "; ".join(m[:120] for m in msgs))
    else:
        check("脚本运行无异常抛出", True)

    # 断言页面正常渲染出标题
    titles = [t.value for t in at.title]
    check("页面渲染出应用标题", any("NetPilot" in t for t in titles), str(titles))


def main():
    print("=" * 60)
    print("Streamlit 启动约束测试（set_page_config 顺序）")
    print("=" * 60)
    for fn in (
        test_set_page_config_is_first_and_single,
        test_no_top_level_st_sidebar_before_config,
        test_no_nested_expander,
        test_apptest_runs_without_exception,
    ):
        print(f"\n--- {fn.__name__} ---")
        fn()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"启动约束测试结果: {passed}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  未通过: {n} -> {d}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
