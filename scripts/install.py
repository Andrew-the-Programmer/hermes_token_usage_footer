#!/usr/bin/env python3
"""Install the token-usage footer into Hermes.

Insertion points are located structurally (AST), never by exact text, and every
edited file is syntax-checked before it is written. An anchor that cannot be
found is reported and skipped instead of guessed at. Each inserted hook is
wrapped in a guard, so a hook that cannot resolve a name costs the footer, not
the reply.

Usage:
    python scripts/install.py [HERMES_WORKSPACE] [--dry-run]
    python scripts/install.py [HERMES_WORKSPACE] --revert
"""

from __future__ import annotations

import ast
import shutil
import sys
from pathlib import Path

HELPER_SENTINEL = "def build_turn_usage_footer"
BASELINE_SENTINEL = "record_turn_baseline"
BASELINE_LEGACY = "_turn_start_total_tokens"
HOOK_SENTINEL = "_tf_agent"


def helpers() -> str:
    return '''


_TURN_BASELINES = {}


def record_turn_baseline(agent) -> None:
    """Snapshot the session counters at the start of a turn.

    The snapshot lives here instead of on the agent because some builds reject
    attributes they do not declare.
    """
    try:
        _TURN_BASELINES[id(agent)] = (
            int(getattr(agent, "session_prompt_tokens", 0) or 0),
            int(getattr(agent, "session_completion_tokens", 0) or 0),
            int(getattr(agent, "session_total_tokens", 0) or 0),
        )
    except Exception:
        return
    if len(_TURN_BASELINES) > 256:
        for key in list(_TURN_BASELINES)[:-128]:
            _TURN_BASELINES.pop(key, None)


def build_turn_usage_footer(agent, markdown: bool = False) -> str:
    """Return the token footer for the turn that just finished.

    Returns "" when no baseline was recorded for this agent, so a caller that
    renders without a turn behind it shows nothing rather than a session total.
    """
    if agent is None:
        return ""
    baseline = _TURN_BASELINES.get(id(agent))
    if baseline is None:
        return ""

    def total(name):
        return int(getattr(agent, name, 0) or 0)

    prompt = total("session_prompt_tokens") - baseline[0]
    completion = total("session_completion_tokens") - baseline[1]
    total_tokens = total("session_total_tokens") - baseline[2]
    if total_tokens <= 0:
        return ""

    model = getattr(agent, "model", "") or "unknown"
    line = (
        f"📊 Tokens: ↑{prompt:,} ↓{completion:,} "
        f"| Total: {total_tokens:,} | Model: {model}"
    )
    return f"\\n\\n---\\n*{line}*" if markdown else f"\\n\\n---\\n{line}"


def token_footer_enabled(config, platform_key=None) -> bool:
    """True when display.show_token_usage is on, honouring per-platform
    overrides where the gateway resolver is importable."""
    if platform_key:
        try:
            from gateway.display_config import resolve_display_setting

            return bool(
                resolve_display_setting(
                    config, platform_key, "show_token_usage", False
                )
            )
        except Exception:
            pass
    try:
        return bool((config.get("display") or {}).get("show_token_usage"))
    except Exception:
        return False
'''


def baseline_block(indent: str, target: str) -> str:
    i = indent
    return (
        f"{i}try:\n"
        f"{i}    from agent.turn_usage import record_turn_baseline\n"
        f"{i}    record_turn_baseline({target})\n"
        f"{i}except Exception:\n"
        f"{i}    pass\n"
    )


def _agent_lookup(indent: str) -> str:
    i = indent
    return (
        f'{i}    _tf_agent = locals().get("agent")\n'
        f"{i}    if _tf_agent is None:\n"
        f"{i}        try:\n"
        f'{i}            _tf_agent = getattr(self, "agent", None)\n'
        f"{i}        except NameError:\n"
        f"{i}            _tf_agent = None\n"
    )


def cli_block(indent: str, var: str) -> str:
    i = indent
    return (
        f"{i}try:\n"
        f"{i}    from agent.turn_usage import (\n"
        f"{i}        build_turn_usage_footer,\n"
        f"{i}        token_footer_enabled,\n"
        f"{i}    )\n"
        f"{i}    from hermes_cli.config import load_config as _tf_load_config\n"
        + _agent_lookup(i)
        + f"{i}    if _tf_agent is not None and token_footer_enabled(_tf_load_config()):\n"
        f"{i}        {var} += build_turn_usage_footer(_tf_agent, markdown=True)\n"
        f"{i}except Exception:\n"
        f"{i}    pass\n"
    )


def gateway_block(indent: str, var: str) -> str:
    i = indent
    return (
        f"{i}try:\n"
        f"{i}    from agent.turn_usage import (\n"
        f"{i}        build_turn_usage_footer,\n"
        f"{i}        token_footer_enabled,\n"
        f"{i}    )\n"
        + _agent_lookup(i)
        + f"{i}    try:\n"
        f"{i}        _tf_cfg = _load_gateway_config()\n"
        f"{i}    except NameError:\n"
        f"{i}        from hermes_cli.config import load_config as _tf_load_config\n"
        f"{i}        _tf_cfg = _tf_load_config()\n"
        f"{i}    if _tf_agent is not None and token_footer_enabled(_tf_cfg):\n"
        f"{i}        {var} += build_turn_usage_footer(_tf_agent, markdown=True)\n"
        f"{i}except Exception:\n"
        f"{i}    pass\n"
    )


def find_hermes_agent_dir(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    try:
        import hermes_cli

        candidates.append(Path(hermes_cli.__file__).resolve().parent.parent)
    except Exception:
        pass
    home = Path.home() / ".hermes"
    candidates.extend(sorted(home.glob("installs/*/environments/*/workspace")))
    candidates.append(home / "hermes-agent")
    for candidate in candidates:
        if (candidate / "agent" / "turn_usage.py").exists():
            return candidate
    raise RuntimeError(
        "Could not find the Hermes workspace. Pass it as the first argument, e.g.\n"
        "  python scripts/install.py ~/.hermes/installs/<id>/environments/<id>/workspace"
    )


def backup(path: Path) -> None:
    bak = path.with_suffix(path.suffix + ".bak")
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)


def save(path: Path, text: str, dry_run: bool) -> bool:
    try:
        ast.parse(text)
    except SyntaxError as exc:
        print(f"  ✗ refusing to write {path}: result would not parse ({exc})")
        return False
    if dry_run:
        print(f"  · would write {path.name}")
        return True
    backup(path)
    path.write_text(text)
    return True


def insert_block(text: str, lineno: int, block: str) -> str:
    lines = text.splitlines(keepends=True)
    lines.insert(lineno - 1, block)
    return "".join(lines)


def function_named(tree: ast.AST, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def first_param(func: ast.FunctionDef) -> str:
    args = list(func.args.posonlyargs) + list(func.args.args)
    return args[0].arg if args else "self"


def call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def find_call(tree: ast.AST, suffix: str) -> ast.Call | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and call_name(node).endswith(suffix):
            return node
    return None


def statement_holding(tree: ast.AST, call: ast.Call) -> ast.stmt | None:
    best = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        if not (node.lineno <= call.lineno and call.end_lineno <= node.end_lineno):
            continue
        span = node.end_lineno - node.lineno
        if best is None or span < best[0]:
            best = (span, node)
    return best[1] if best else None


def mentions(node: ast.AST, value: str) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and child.value == value:
            return True
    return False


def final_response_assign(tree: ast.AST) -> ast.Assign | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if mentions(node.value, "final_response"):
            return node
    return None


def nested_dict(tree: ast.AST, outer_key: str) -> ast.Dict | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == outer_key
                and isinstance(value, ast.Dict)
            ):
                return value
    return None


def dicts_with_key(tree: ast.AST, wanted: str) -> list[ast.Dict]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key in node.keys:
            if isinstance(key, ast.Constant) and key.value == wanted:
                found.append(node)
                break
    return found


def add_key_after(text: str, node: ast.Dict, key: str, entry: str) -> str | None:
    for item, value in zip(node.keys, node.values):
        if isinstance(item, ast.Constant) and item.value == key:
            indent = " " * value.col_offset
            return insert_block(text, value.end_lineno + 1, f"{indent}{entry}\n")
    for item in node.keys:
        if isinstance(item, ast.Constant):
            indent = " " * item.col_offset
            return insert_block(text, item.lineno, f"{indent}{entry}\n")
    return None


def add_to_overrideable_keys(text: str, tree: ast.AST, entry: str) -> str | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or "OVERRIDEABLE_KEYS" not in target.id:
            continue
        if not isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
            continue
        items = node.value.elts
        if not items or items[0].lineno == items[-1].lineno:
            continue
        indent = " " * items[-1].col_offset
        return insert_block(text, items[-1].end_lineno + 1, f"{indent}{entry}\n")
    return None


def patch_helpers(base: Path, dry_run: bool) -> bool:
    path = base / "agent" / "turn_usage.py"
    if not path.exists():
        print("  ✗ agent/turn_usage.py not found")
        return False
    text = path.read_text()
    if HELPER_SENTINEL in text:
        print("  ✓ footer helpers already present")
        return True
    updated = text.rstrip("\n") + "\n" + helpers()
    if not save(path, updated, dry_run):
        return False
    print("  ✓ added build_turn_usage_footer / token_footer_enabled")
    return True


def patch_baseline(base: Path, dry_run: bool) -> bool:
    for rel in ("agent/conversation_loop.py", "agent/turn_facade.py"):
        path = base / rel
        if not path.exists():
            continue
        text = path.read_text()
        if BASELINE_SENTINEL in text:
            print(f"  ✓ turn baseline already present in {rel}")
            return True
        if BASELINE_LEGACY in text:
            print(f"  ! {rel} carries the old unguarded baseline; run --revert first")
            return False
        tree = ast.parse(text)
        func = function_named(tree, "run_conversation")
        if func is None:
            continue
        body = list(func.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        if not body:
            continue
        anchor = body[0]
        block = baseline_block(" " * anchor.col_offset, first_param(func))
        if not save(path, insert_block(text, anchor.lineno, block), dry_run):
            return False
        print(f"  ✓ sampled session counters at turn start in {rel}")
        return True
    print("  ✗ no run_conversation() found to sample the turn baseline")
    return False


def patch_config_defaults(base: Path, dry_run: bool) -> bool:
    path = base / "hermes_cli" / "config_defaults.py"
    if not path.exists():
        print("  ✗ hermes_cli/config_defaults.py not found")
        return False
    text = path.read_text()
    if '"show_token_usage"' in text:
        print("  ✓ display.show_token_usage already declared")
        return True
    display = nested_dict(ast.parse(text), "display")
    if display is None:
        print("  ✗ no display defaults dict found")
        return False
    updated = add_key_after(
        text, display, "show_reasoning", '"show_token_usage": False,'
    )
    if updated is None:
        print("  ✗ display defaults dict has no literal keys")
        return False
    if not save(path, updated, dry_run):
        return False
    print("  ✓ declared display.show_token_usage = false")
    return True


def patch_display_config(base: Path, dry_run: bool) -> bool:
    path = base / "gateway" / "display_config.py"
    if not path.exists():
        print("  ! gateway/display_config.py not found (skipping)")
        return True
    text = path.read_text()
    if '"show_token_usage"' in text:
        print("  ✓ gateway display defaults already declare show_token_usage")
        return True
    tree = ast.parse(text)
    updated = text
    for node in sorted(dicts_with_key(tree, "streaming"), key=lambda d: -d.lineno):
        result = add_key_after(
            updated, node, "streaming", '"show_token_usage": False,'
        )
        if result is not None:
            updated = result
    result = add_to_overrideable_keys(
        updated, ast.parse(updated), '"show_token_usage",'
    )
    if result is not None:
        updated = result
    if updated == text:
        print("  ! no display tier defaults found (skipping)")
        return True
    if not save(path, updated, dry_run):
        return False
    print("  ✓ declared show_token_usage in gateway display tiers")
    return True


def patch_cli(base: Path, dry_run: bool) -> bool:
    path = base / "hermes_cli" / "cli_chat_turn_mixin.py"
    if not path.exists():
        print("  ✗ hermes_cli/cli_chat_turn_mixin.py not found")
        return False
    text = path.read_text()
    if HOOK_SENTINEL in text:
        print("  ✓ CLI footer already hooked")
        return True
    tree = ast.parse(text)
    call = find_call(tree, "_render_final_assistant_content")
    if call is None:
        print("  ✗ no _render_final_assistant_content() call found")
        return False
    stmt = statement_holding(tree, call)
    if stmt is None:
        print("  ✗ could not resolve the statement rendering the reply")
        return False
    var = (
        call.args[0].id
        if call.args and isinstance(call.args[0], ast.Name)
        else "response"
    )
    block = cli_block(" " * stmt.col_offset, var)
    if not save(path, insert_block(text, stmt.lineno, block), dry_run):
        return False
    print(f"  ✓ footer appended to `{var}` before rendering")
    return True


def patch_gateway(base: Path, dry_run: bool) -> bool:
    for rel in ("gateway/run_turn.py", "gateway/run_turn_runner.py", "gateway/run.py"):
        path = base / rel
        if not path.exists():
            continue
        text = path.read_text()
        if HOOK_SENTINEL in text:
            print(f"  ✓ gateway footer already hooked in {rel}")
            return True
        tree = ast.parse(text)
        node = final_response_assign(tree)
        if node is None:
            continue
        var = node.targets[0].id
        block = gateway_block(" " * node.col_offset, var)
        if not save(path, insert_block(text, node.end_lineno + 1, block), dry_run):
            return False
        print(f"  ✓ footer appended to `{var}` before delivery ({rel})")
        return True
    print("  ! no final_response assignment found (gateway footer skipped)")
    return True


def revert(base: Path) -> int:
    restored = 0
    for bak in sorted(base.rglob("*.py.bak")):
        target = bak.with_suffix("")
        shutil.copy2(bak, target)
        print(f"  ✓ restored {target.relative_to(base)}")
        restored += 1
    if not restored:
        print("  ! no *.py.bak backups found")
        return 1
    return 0


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    revert_requested = "--revert" in sys.argv
    positional = [arg for arg in sys.argv[1:] if not arg.startswith("--")]

    print("🔧 Token Usage Monitoring Installer")
    print("-" * 40)
    try:
        base = find_hermes_agent_dir(positional[0] if positional else None)
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"Hermes at: {base}\n")

    if revert_requested:
        print("[revert]")
        code = revert(base)
        print("-" * 40)
        if code == 0:
            print("✅ Backups restored. Restart with: hermes gateway restart")
        return code

    steps = [
        ("footer helpers", patch_helpers),
        ("turn baseline", patch_baseline),
        ("config default", patch_config_defaults),
        ("gateway defaults", patch_display_config),
        ("CLI footer", patch_cli),
        ("gateway footer", patch_gateway),
    ]
    results = []
    for label, step in steps:
        print(f"[{label}]")
        results.append(step(base, dry_run))

    print("-" * 40)
    if all(results):
        print("✅ Patched. Enable it with:")
        print("  hermes config set display.show_token_usage true")
        print("  hermes gateway restart    # only if you use messaging platforms")
        return 0
    print("⚠ Some steps failed; every applied edit is listed above and backed up as *.bak.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
