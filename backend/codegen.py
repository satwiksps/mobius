"""Graph JSON -> workflow.py code generator.

Reads /workspace/workflow.json, emits /workspace/workflow.py that uses mobius_core
verbs (Do, Navigate, Check, Fill, Read) with the same patterns as hand-written
workflow scripts.
"""

from __future__ import annotations

import json
import re
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class CodegenError(Exception):
    """Raised when the graph cannot be compiled to valid Python."""


# ── Graph data structures ────────────────────────────────────────────────────


@dataclass
class SchemaField:
    name: str
    type: str  # "str" | "int" | "float" | "bool"

    VALID_TYPES = {
        "str",
        "int",
        "float",
        "bool",
        "list[str]",
        "list[int]",
        "list[float]",
    }

    def python_type(self) -> str:
        if self.type not in self.VALID_TYPES:
            raise CodegenError(f"Unknown schema field type: {self.type!r}")
        # Python 3.9+ supports list[str] natively in type hints
        return self.type


@dataclass
class OutputSchema:
    fields: list[SchemaField]

    def class_name(self, node_id: str) -> str:
        return f"{node_id.replace('-', '_').capitalize()}Output"


@dataclass
class Node:
    id: str
    type: str  # Do | Navigate | Check | Fill | Read | Code
    label: str
    position: dict[str, float]
    config: dict[str, Any]
    output_schema: OutputSchema | None

    VALID_TYPES = {
        "Action",
        "Browse",
        "Condition",
        "Input",
        "Extract",
        "Code",
        "Agent",
        "ForEach",
        "Setup",
    }


@dataclass
class Edge:
    id: str
    source: str
    target: str
    type: str  # sequential | conditional_true | conditional_false | loop_back
    max_iterations: int = 3

    VALID_TYPES = {
        "sequential",
        "conditional_true",
        "conditional_false",
        "loop_back",
        "foreach_done",
    }


@dataclass
class GlobalConfig:
    llm: str = "gemini-3-flash-preview"
    human_in_the_loop: bool = False
    verbose: bool = True


@dataclass
class LoopGroup:
    """A retry loop detected from a loop_back edge."""

    header: str  # node the back-edge points TO (the Check node)
    body: list[str]  # nodes between header and tail in topo order
    tail: str  # node the back-edge comes FROM
    max_iterations: int


# ── Parsing ──────────────────────────────────────────────────────────────────


def parse_graph(data: dict) -> tuple[GlobalConfig, list[Node], list[Edge]]:
    """Parse the graph JSON into typed objects."""
    global_cfg = GlobalConfig(
        llm=data.get("global", {}).get("llm", "gemini-3-flash-preview"),
        human_in_the_loop=data.get("global", {}).get("human_in_the_loop", False),
        verbose=data.get("global", {}).get("verbose", True),
    )

    nodes = []
    for nd in data.get("nodes", []):
        schema = None
        if nd.get("output_schema") and nd["output_schema"].get("fields"):
            schema = OutputSchema(
                fields=[
                    SchemaField(f["name"], f["type"])
                    for f in nd["output_schema"]["fields"]
                ]
            )
        node = Node(
            id=nd["id"],
            type=nd["type"],
            label=nd.get("label", nd["id"]),
            position=nd.get("position", {"x": 0, "y": 0}),
            config=nd.get("config", {}),
            output_schema=schema,
        )
        if node.type not in Node.VALID_TYPES:
            raise CodegenError(f"Unknown node type: {node.type!r} on node {node.id!r}")
        nodes.append(node)

    edges = []
    for ed in data.get("edges", []):
        edge = Edge(
            id=ed["id"],
            source=ed["source"],
            target=ed["target"],
            type=ed["type"],
            max_iterations=ed.get("max_iterations", 3),
        )
        if edge.type not in Edge.VALID_TYPES:
            raise CodegenError(f"Unknown edge type: {edge.type!r} on edge {edge.id!r}")
        edges.append(edge)

    return global_cfg, nodes, edges


# ── Graph analysis ───────────────────────────────────────────────────────────


def _build_adjacency(
    nodes: list[Node], edges: list[Edge]
) -> tuple[dict[str, list[Edge]], dict[str, list[Edge]]]:
    out_edges: dict[str, list[Edge]] = defaultdict(list)
    in_edges: dict[str, list[Edge]] = defaultdict(list)
    for e in edges:
        out_edges[e.source].append(e)
        in_edges[e.target].append(e)
    return dict(out_edges), dict(in_edges)


def _detect_loops(edges: list[Edge]) -> list[LoopGroup]:
    """Find loop_back edges and build LoopGroups."""
    loops = []
    for e in edges:
        if e.type == "loop_back":
            loops.append(
                LoopGroup(
                    header=e.target,
                    body=[],  # filled during topo sort
                    tail=e.source,
                    max_iterations=e.max_iterations,
                )
            )
    # Check for nested loops (not supported in v1)
    if len(loops) > 1:
        headers = {lg.header for lg in loops}
        tails = {lg.tail for lg in loops}
        # Simple heuristic: if any header is also a body member of another loop, it's nested
        # For v1 we just warn; full detection happens below during topo sort
    return loops


def _reachable_from(
    start: str,
    out_edges: dict[str, list[Edge]],
    skip_types: frozenset = frozenset({"loop_back"}),
) -> set[str]:
    """DFS from start following edges whose type is not in skip_types."""
    visited: set[str] = set()
    stack = [start]
    while stack:
        nid = stack.pop()
        if nid in visited:
            continue
        visited.add(nid)
        for e in out_edges.get(nid, []):
            if e.type not in skip_types:
                stack.append(e.target)
    return visited


def _reachable_between(
    header: str,
    tail: str,
    out_edges: dict[str, list[Edge]],
    topo_order: list[str],
) -> list[str]:
    """Nodes strictly between the loop header and tail in the DAG.

    A node N is in the loop body iff it is BOTH reachable from the header (via
    DAG edges, i.e. excluding loop_back) AND can reach the tail (via DAG edges).
    Just "reachable from header" is not enough — that pulls in conditional
    branches that exit the loop (e.g. a Check at the header with a
    conditional_true edge to an external "end" node).

    Returns body nodes in topo order, excluding the header and tail.
    """
    # Forward: nodes reachable from header in DAG (excluding loop_back)
    forward: set[str] = set()
    stack = [header]
    while stack:
        nid = stack.pop()
        if nid in forward:
            continue
        forward.add(nid)
        for e in out_edges.get(nid, []):
            if e.type == "loop_back":
                continue
            if e.target not in forward:
                stack.append(e.target)

    # Backward: nodes that can reach the tail in DAG. Build inverse adjacency
    # on the fly, filtering out loop_back edges.
    in_edges_dag: dict[str, list[str]] = {}
    for src, es in out_edges.items():
        for e in es:
            if e.type == "loop_back":
                continue
            in_edges_dag.setdefault(e.target, []).append(src)

    backward: set[str] = set()
    stack = [tail]
    while stack:
        nid = stack.pop()
        if nid in backward:
            continue
        backward.add(nid)
        for src in in_edges_dag.get(nid, []):
            if src not in backward:
                stack.append(src)

    body = (forward & backward) - {header, tail}
    return [n for n in topo_order if n in body]


def _find_merge_point(
    true_target: str | None,
    false_target: str | None,
    out_edges: dict[str, list[Edge]],
    topo_order: list[str],
) -> str | None:
    """Return the first node in topo order reachable from both conditional branches."""
    if not true_target or not false_target:
        return None
    true_reachable = _reachable_from(true_target, out_edges)
    false_reachable = _reachable_from(false_target, out_edges)
    common = (true_reachable & false_reachable) - {true_target, false_target}
    for nid in topo_order:
        if nid in common:
            return nid
    return None


def _topo_sort(
    nodes: list[Node], edges: list[Edge], loops: list[LoopGroup]
) -> list[str]:
    """Kahn's algorithm, excluding loop_back edges (foreach_done edges are included)."""
    node_ids = {n.id for n in nodes}
    # Exclude loop_back for cycle detection; foreach_done is a real DAG edge
    dag_edges = [e for e in edges if e.type != "loop_back"]

    in_degree: dict[str, int] = {nid: 0 for nid in node_ids}
    adj: dict[str, list[str]] = defaultdict(list)
    for e in dag_edges:
        adj[e.source].append(e.target)
        in_degree[e.target] = in_degree.get(e.target, 0) + 1

    queue = [nid for nid in node_ids if in_degree[nid] == 0]
    # Stable sort: prefer nodes in the order they appear in the JSON
    id_order = {n.id: i for i, n in enumerate(nodes)}
    queue.sort(key=lambda x: id_order.get(x, 0))

    order = []
    while queue:
        nid = queue.pop(0)
        order.append(nid)
        for neighbor in adj.get(nid, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
                queue.sort(key=lambda x: id_order.get(x, 0))

    if len(order) != len(node_ids):
        missing = node_ids - set(order)
        node_labels = {n.id: (n.label or n.type or n.id) for n in nodes}
        cycle_edges = [
            f"{node_labels.get(e.source, e.source)!r} ({e.source}) → {node_labels.get(e.target, e.target)!r} ({e.target})"
            for e in dag_edges
            if e.source in missing and e.target in missing
        ]
        detail = (
            f" Cycle edges: {cycle_edges}."
            if cycle_edges
            else " Check for edges connecting nodes in a circle."
        )
        raise CodegenError(
            f"Graph has a cycle. Delete the edge that loops back, or mark it "
            f"as a loop_back edge by drawing it from a lower node to a higher one.{detail}"
        )

    # Build out_edges map for reachability (excluding loop_back)
    out_edges_dag: dict[str, list[Edge]] = defaultdict(list)
    for e in dag_edges:
        out_edges_dag[e.source].append(e)

    # Fill loop body lists using DFS reachability instead of index slicing
    for lg in loops:
        if lg.header not in {n.id for n in nodes} or lg.tail not in {
            n.id for n in nodes
        }:
            raise CodegenError(
                f"Loop references unknown node (header={lg.header!r}, tail={lg.tail!r})"
            )
        h_idx = order.index(lg.header) if lg.header in order else -1
        t_idx = order.index(lg.tail) if lg.tail in order else -1
        if h_idx == -1 or t_idx == -1:
            raise CodegenError(
                f"Loop header/tail not in topo order: {lg.header!r}, {lg.tail!r}"
            )
        if h_idx > t_idx:
            raise CodegenError(
                f"loop_back edge target {lg.header!r} must come before source "
                f"{lg.tail!r} in topological order."
            )
        lg.body = _reachable_between(lg.header, lg.tail, out_edges_dag, order)

    return order


# ── Template substitution ────────────────────────────────────────────────────

_SECRETS_RE = re.compile(r"\{\{secrets\.(\w+)\}\}", re.IGNORECASE)
_INPUTS_RE  = re.compile(r"\{\{inputs\.(\w+)\}\}", re.IGNORECASE)


def _resolve_inputs(text: str) -> tuple[str, bool]:
    """Replace {{inputs.KEY}} with {_inputs.get('KEY', '')}.

    Returns (resolved_text, uses_fstring).
    """
    has_input = bool(_INPUTS_RE.search(text))
    resolved = _INPUTS_RE.sub(
        lambda m: "{_inputs.get('" + m.group(1) + "', '')}", text
    )
    return resolved, has_input


def _resolve_secrets(text: str) -> tuple[str, bool]:
    """Replace {{secrets.KEY}} with {os.environ.get('KEY', '')}.

    Returns (resolved_text, uses_fstring).
    """
    has_secret = bool(_SECRETS_RE.search(text))
    resolved = _SECRETS_RE.sub(
        lambda m: "{os.environ.get('" + m.group(1) + "', '')}", text
    )
    return resolved, has_secret


_TEMPLATE_RE = re.compile(r"\{\{(\w+)\.(\w+)\}\}")
_BARE_VAR_RE = re.compile(r"\{\{([^{}]+)\}\}")


def _resolve_all(text: str, nodes_by_id: dict[str, Node]) -> tuple[str, bool]:
    """Run inputs, secrets, and node-template resolution. Returns (text, uses_fstring)."""
    text, is_input  = _resolve_inputs(text)   # must be before _resolve_templates (inputs.X matches TEMPLATE_RE)
    text, is_secret = _resolve_secrets(text)
    text, is_template = _resolve_templates(text, nodes_by_id)
    # Also resolve bare {{var}} (loop variables like {{item}})
    bare_result, bare_count = _BARE_VAR_RE.subn(r"{\1}", text)
    if bare_count:
        text = bare_result
        is_template = True
    return text, (is_input or is_secret or is_template)


def _resolve_templates(text: str, nodes_by_id: dict[str, Node]) -> tuple[str, bool]:
    """Replace {{node_id.field}} with {node_id_out.field}.

    Returns (resolved_text, uses_fstring).
    """
    has_template = False

    def _replace(m: re.Match) -> str:
        nonlocal has_template
        node_id, field_name = m.group(1), m.group(2)
        node = nodes_by_id.get(node_id)
        if not node:
            raise CodegenError(
                f"Template {{{{{{node_id}}.{field_name}}}}} references unknown node {node_id!r}"
            )
        if not node.output_schema:
            raise CodegenError(
                f"Template {{{{{{node_id}}.{field_name}}}}} references node {node_id!r} "
                f"which has no output_schema"
            )
        field_names = {f.name for f in node.output_schema.fields}
        if field_name not in field_names:
            raise CodegenError(
                f"Template {{{{{{node_id}}.{field_name}}}}} references field {field_name!r} "
                f"not in schema of node {node_id!r} (available: {field_names})"
            )
        has_template = True
        var_name = f"{node_id}_out"
        return "{" + f"{var_name}.{field_name}" + "}"

    resolved = _TEMPLATE_RE.sub(_replace, text)
    return resolved, has_template


# ── Code emission ────────────────────────────────────────────────────────────


def _emit_pydantic_models(nodes: list[Node]) -> list[str]:
    """Generate Pydantic model classes for nodes with output_schema."""
    lines = []
    for node in nodes:
        if not node.output_schema:
            continue
        cls_name = node.output_schema.class_name(node.id)
        lines.append(f"class {cls_name}(BaseModel):")
        for f in node.output_schema.fields:
            lines.append(f"    {f.name}: {f.python_type()}")
        lines.append("")
    return lines


def _esc(s: str) -> str:
    """Escape backslashes, double-quotes, and newlines so the string is safe inside '\"...\"'."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace('\r', '').replace('\n', '\\n')


def _node_llm_expr(node: Node, global_cfg: GlobalConfig) -> str:
    """Return the Python expression for this node's llm= kwarg."""
    node_llm = node.config.get("llm")
    if node_llm:
        return repr(node_llm)
    return "model"


def _mcp_open_lines(node: Node, indent: int) -> tuple[list[str], list[str], int]:
    """Return (open_lines, close_lines, inner_indent) for MCP context managers on a node.

    Each MCP server wraps the verb call in an ``async with MCPToolset...`` block.
    Returns the opening lines, closing lines (empty — context managers auto-close),
    and the new indent level inside the innermost context.
    """
    servers = node.config.get("mcp_servers") or []
    open_lines: list[str] = []
    for idx, srv in enumerate(servers):
        pad = "    " * (indent + idx)
        transport = srv.get("transport", "stdio")
        if transport == "sse":
            url = srv.get("url", "")
            open_lines.append(
                f"{pad}async with _MCPToolset.from_server(_SseServerParams(url={url!r})) as _mcp_{node.id}_{idx}:"
            )
        else:
            cmd = srv.get("command", "")
            args = srv.get("args") or []
            open_lines.append(
                f"{pad}async with _MCPToolset.from_server(_StdioParams(command={cmd!r}, args={args!r})) as _mcp_{node.id}_{idx}:"
            )
    inner_indent = indent + len(servers)
    return open_lines, inner_indent


def _mcp_extra_tools_expr(node: Node, indent: int) -> str | None:
    """Return the extra_tools=[...] expression for MCP servers, or None."""
    servers = node.config.get("mcp_servers") or []
    if not servers:
        return None
    parts = [f"*_mcp_{node.id}_{idx}.tools" for idx in range(len(servers))]
    return f"[{', '.join(parts)}]"


def _emit_node(
    node: Node,
    global_cfg: GlobalConfig,
    nodes_by_id: dict[str, Node],
    indent: int = 2,
    log_file_path: str | None = None,
    prev_output_var: str | None = None,
) -> list[str]:
    """Emit Python lines for a single node."""
    pad = "    " * indent
    lines = []
    llm = _node_llm_expr(node, global_cfg)
    max_steps = node.config.get("max_steps") or None
    # Explicit extra_info from config takes priority; otherwise pipe previous node's output
    extra_info = node.config.get("extra_info") or (
        f"{{str({prev_output_var})}}" if prev_output_var else None
    )

    if node.type == "Code":
        lines.append(f'{pad}print("--- {node.id}: {node.label} ---")')
        lines.append(f'{pad}report_node("{node.id}", "running")')
        lines.append(f'{pad}_current_log_node[0] = {node.id!r}')
        lines.append(f"{pad}try:")
        code = node.config.get("code", "pass")
        dedented = textwrap.dedent(code)
        for line in dedented.splitlines():
            lines.append(f"{pad}    {line}")
        lines.append(f"{pad}except Exception as _code_err_{node.id}:")
        lines.append(f"{pad}    import traceback as _tb_{node.id}")
        lines.append(f"{pad}    print(f'ERROR in Code node {node.id}: {{_code_err_{node.id}}}')")
        lines.append(f"{pad}    _tb_{node.id}.print_exc()")
        lines.append(f"{pad}    raise")
        lines.append(f'{pad}_current_log_node[0] = None')
        lines.append(f'{pad}report_node("{node.id}", "success")')
        return lines

    if node.type not in ("Condition", "Setup"):
        lines.append(f'{pad}report_node("{node.id}", "running")')
        lines.append(f'{pad}print("--- {node.id}: {node.label} ---")')
        lines.append(f'{pad}_current_log_node[0] = {node.id!r}')

    if node.type == "Setup":
        lines.append(f'{pad}report_node("{node.id}", "running")')
        lines.append(f'{pad}print("--- {node.id}: {node.label} ---")')
        lines.append(f'{pad}_current_log_node[0] = {node.id!r}')
        raw_packages = node.config.get("packages", "")
        if isinstance(raw_packages, list):
            pkg_list = raw_packages
        else:
            pkg_list = [p.strip() for p in re.split(r"[,\n]+", str(raw_packages)) if p.strip()]
        pkg_repr = repr(pkg_list)
        lines.append(f"{pad}_{node.id}_result = await Setup({pkg_repr}).run()")
        lines.append(
            f"{pad}if _{node.id}_result.status == 'failed': raise RuntimeError(_{node.id}_result.summary)"
        )
        lines.append(f"{pad}if hasattr(_{node.id}_result, 'summary') and _{node.id}_result.summary:")
        lines.append(f"{pad}    report_node_log({node.id!r}, str(_{node.id}_result.summary))")
        lines.append(f'{pad}_current_log_node[0] = None')
        lines.append(f'{pad}report_node("{node.id}", "success")')
        return lines

    # Open MCP context managers (if any), adjusting effective indent for the verb call
    mcp_open, verb_indent = _mcp_open_lines(node, indent)
    lines.extend(mcp_open)
    vpad = "    " * verb_indent  # effective padding inside context managers

    # Common kwargs
    steps_arg = f", max_steps={max_steps}" if max_steps is not None else ""
    timeout_val = node.config.get("timeout") or None
    timeout_arg = f", timeout={timeout_val}" if timeout_val is not None else ""
    common = f"session=s, llm={llm}"
    if node.type != "Condition":
        planner_val = "True" if node.config.get("planner", False) else "False"
        common += f"{steps_arg}{timeout_arg}, verbose=verbose, pause_event=pause_event, planner={planner_val}"
        if not global_cfg.human_in_the_loop:
            common += ", human_in_the_loop=False"
        # log_file_path is handled at workflow level via stdout Tee, not per-verb
    else:
        if max_steps is not None:
            common += f", max_steps={max_steps}"

    if extra_info and node.type in ("Action", "Browse", "Extract", "Input", "Agent"):
        # prev_output_var injection uses {str(...)} — emit as f-string, manual extra_info as plain string
        if extra_info.startswith("{") and extra_info.endswith("}"):
            common += f', extra_info=f"{extra_info}"'
        else:
            common += f", extra_info={extra_info!r}"

    # Append extra_tools for MCP servers
    mcp_tools = _mcp_extra_tools_expr(node, indent)
    if mcp_tools:
        common += f", extra_tools={mcp_tools}"

    if node.type == "Browse":
        target = node.config.get("target", "").strip()
        target, is_fstr = _resolve_all(target, nodes_by_id)
        target = _esc(target)
        q = "f" if is_fstr else ""
        if node.output_schema:
            cls_name = node.output_schema.class_name(node.id)
            lines.append(f"{vpad}_{node.id}_result = await Browse(")
            lines.append(f'{vpad}    {q}"{target}", {common},')
            lines.append(f"{vpad}).run()")
            lines.append(
                f"{vpad}if _{node.id}_result.status in ('error', 'failed'): raise RuntimeError(_{node.id}_result.summary)"
            )
            lines.append(f"{vpad}if hasattr(_{node.id}_result, 'summary') and _{node.id}_result.summary:")
            lines.append(f"{vpad}    report_node_log({node.id!r}, str(_{node.id}_result.summary))")
            lines.append(f"{vpad}{node.id}_out = _{node.id}_result.output")
            lines.append(
                f'{vpad}if {node.id}_out is None: raise RuntimeError("Browse node {node.id} ({node.label}) returned None output — schema validation failed")'
            )
            lines.append(f"{vpad}if isinstance({node.id}_out, str):")
            lines.append(f"{vpad}    import json as _json_{node.id}")
            lines.append(f"{vpad}    {node.id}_out = {cls_name}(**_json_{node.id}.loads({node.id}_out))")
        else:
            lines.append(f"{vpad}_{node.id}_result = await Browse(")
            lines.append(f'{vpad}    {q}"{target}", {common},')
            lines.append(f"{vpad}).run()")
            lines.append(
                f"{vpad}if _{node.id}_result.status in ('error', 'failed'): raise RuntimeError(_{node.id}_result.summary)"
            )
            lines.append(f"{vpad}if hasattr(_{node.id}_result, 'summary') and _{node.id}_result.summary:")
            lines.append(f"{vpad}    report_node_log({node.id!r}, str(_{node.id}_result.summary))")

    elif node.type == "Action":
        task = node.config.get("task", "").strip()
        task, is_fstr = _resolve_all(task, nodes_by_id)
        task = _esc(task)
        q = "f" if is_fstr else ""
        if node.output_schema:
            cls_name = node.output_schema.class_name(node.id)
            lines.append(f"{vpad}_{node.id}_result = await Action(")
            lines.append(f'{vpad}    {q}"{task}", {common},')
            lines.append(f"{vpad}    output_schema={cls_name},")
            lines.append(f"{vpad}).run()")
            lines.append(
                f"{vpad}if _{node.id}_result.status in ('error', 'failed'): raise RuntimeError(_{node.id}_result.summary)"
            )
            lines.append(f"{vpad}if hasattr(_{node.id}_result, 'summary') and _{node.id}_result.summary:")
            lines.append(f"{vpad}    report_node_log({node.id!r}, str(_{node.id}_result.summary))")
            lines.append(f"{vpad}{node.id}_out = _{node.id}_result.output")
            lines.append(
                f'{vpad}if {node.id}_out is None: raise RuntimeError("Action node {node.id} ({node.label}) returned None output — schema validation failed")'
            )
            lines.append(f"{vpad}if isinstance({node.id}_out, str):")
            lines.append(f"{vpad}    import json as _json_{node.id}")
            lines.append(f"{vpad}    {node.id}_out = {cls_name}(**_json_{node.id}.loads({node.id}_out))")
        else:
            lines.append(f"{vpad}_{node.id}_result = await Action(")
            lines.append(f'{vpad}    {q}"{task}", {common},')
            lines.append(f"{vpad}).run()")
            lines.append(
                f"{vpad}if _{node.id}_result.status in ('error', 'failed'): raise RuntimeError(_{node.id}_result.summary)"
            )
            lines.append(f"{vpad}if hasattr(_{node.id}_result, 'summary') and _{node.id}_result.summary:")
            lines.append(f"{vpad}    report_node_log({node.id!r}, str(_{node.id}_result.summary))")

    elif node.type == "Extract":
        task = node.config.get("task", "").strip()
        task, is_fstr = _resolve_all(task, nodes_by_id)
        task = _esc(task)
        q = "f" if is_fstr else ""
        if node.output_schema:
            cls_name = node.output_schema.class_name(node.id)
            # Inject the Pydantic JSON schema into the task prompt at runtime so
            # models that ignore set_model_response still know the exact shape.
            lines.append(f"{vpad}_{node.id}_schema_hint = json.dumps({cls_name}.model_json_schema(), indent=2)")
            lines.append(f"{vpad}_{node.id}_task = {q}\"{task}\" + \"\\n\\nReturn JSON matching EXACTLY this schema (no prose, no extra keys):\\n\" + _{node.id}_schema_hint")
            lines.append(f"{vpad}_{node.id}_result = await Extract(")
            lines.append(f'{vpad}    _{node.id}_task, schema={cls_name}, {common},')
            lines.append(f"{vpad}).run()")
            lines.append(
                f"{vpad}if _{node.id}_result.status in ('error', 'failed'): raise RuntimeError(_{node.id}_result.summary)"
            )
            lines.append(f"{vpad}if hasattr(_{node.id}_result, 'summary') and _{node.id}_result.summary:")
            lines.append(f"{vpad}    report_node_log({node.id!r}, str(_{node.id}_result.summary))")
            lines.append(f"{vpad}{node.id}_out = _{node.id}_result.output")
            lines.append(
                f'{vpad}if {node.id}_out is None: raise RuntimeError("Extract node {node.id} ({node.label}) returned None output — schema validation failed")'
            )
            # If mobius_core returned the output as a raw JSON string instead of a Pydantic object, deserialize it
            lines.append(f"{vpad}if isinstance({node.id}_out, str):")
            lines.append(f"{vpad}    import json as _json_{node.id}")
            lines.append(f"{vpad}    {node.id}_out = {cls_name}(**_json_{node.id}.loads({node.id}_out))")
        else:
            lines.append(f"{vpad}_{node.id}_result = await Extract(")
            lines.append(f'{vpad}    {q}"{task}", {common},')
            lines.append(f"{vpad}).run()")
            lines.append(
                f"{vpad}if _{node.id}_result.status in ('error', 'failed'): raise RuntimeError(_{node.id}_result.summary)"
            )
            lines.append(f"{vpad}if hasattr(_{node.id}_result, 'summary') and _{node.id}_result.summary:")
            lines.append(f"{vpad}    report_node_log({node.id!r}, str(_{node.id}_result.summary))")

    elif node.type == "Input":
        target = node.config.get("target", "").strip()
        target, is_fstr_target = _resolve_all(target, nodes_by_id)
        target = _esc(target)
        raw_data = node.config.get("data", {})
        # Resolve secrets in data values
        resolved_data = {}
        is_fstr_data = False
        for k, v in raw_data.items():
            rv, fstr = _resolve_secrets(str(v))
            resolved_data[k] = rv
            if fstr:
                is_fstr_data = True
        is_fstr = is_fstr_target or is_fstr_data
        q = "f" if is_fstr else ""
        # Build data dict literal — use f-string for values that contain {…}
        data_items = ", ".join(
            f'{k!r}: {q}"{_esc(v)}"' for k, v in resolved_data.items()
        )
        lines.append(f"{vpad}_{node.id}_result = await Input(")
        lines.append(f'{vpad}    {q}"{target}",')
        lines.append(f"{vpad}    data={{{data_items}}},")
        lines.append(f"{vpad}    {common},")
        lines.append(f"{vpad}).run()")
        lines.append(
            f"{vpad}if _{node.id}_result.status in ('error', 'failed'): raise RuntimeError(_{node.id}_result.summary)"
        )
        lines.append(f"{vpad}if hasattr(_{node.id}_result, 'summary') and _{node.id}_result.summary:")
        lines.append(f"{vpad}    report_node_log({node.id!r}, str(_{node.id}_result.summary))")

    elif node.type == "Agent":
        class_name = node.config.get("class_name", "CustomVerb").strip()
        task = node.config.get("task", "").strip()
        task, is_fstr = _resolve_all(task, nodes_by_id)
        task = _esc(task)
        q = "f" if is_fstr else ""
        if node.output_schema:
            lines.append(f"{vpad}_{node.id}_result = await {class_name}(")
            lines.append(f'{vpad}    {q}"{task}", {common},')
            lines.append(f"{vpad}).run()")
            lines.append(
                f"{vpad}if _{node.id}_result.status in ('error', 'failed'): raise RuntimeError(_{node.id}_result.summary)"
            )
            lines.append(f"{vpad}if hasattr(_{node.id}_result, 'summary') and _{node.id}_result.summary:")
            lines.append(f"{vpad}    report_node_log({node.id!r}, str(_{node.id}_result.summary))")
            lines.append(f"{vpad}{node.id}_out = _{node.id}_result.output")
        else:
            lines.append(f"{vpad}_{node.id}_result = await {class_name}(")
            lines.append(f'{vpad}    {q}"{task}", {common},')
            lines.append(f"{vpad}).run()")
            lines.append(
                f"{vpad}if _{node.id}_result.status in ('error', 'failed'): raise RuntimeError(_{node.id}_result.summary)"
            )
            lines.append(f"{vpad}if hasattr(_{node.id}_result, 'summary') and _{node.id}_result.summary:")
            lines.append(f"{vpad}    report_node_log({node.id!r}, str(_{node.id}_result.summary))")

    elif node.type == "Condition":
        # Check is handled at the control-flow level, not here
        pass

    if node.type not in ("Condition", "Code", "Setup"):
        if node.output_schema:
            lines.append(
                f'{vpad}report_node_output("{node.id}", _{node.id}_result.output.__dict__ if hasattr(_{node.id}_result.output, "__dict__") else _{node.id}_result.output)'
            )
        lines.append(f'{vpad}_current_log_node[0] = None')
        lines.append(f'{vpad}report_node("{node.id}", "success")')

    return lines


def _emit_check_expr(
    node: Node,
    global_cfg: GlobalConfig,
    nodes_by_id: dict[str, Node],
) -> str:
    """Return the Python expression for a Check node (used in if/for)."""
    llm = _node_llm_expr(node, global_cfg)
    max_steps = node.config.get("max_steps") or None
    condition = node.config.get("condition", "").strip()
    condition, is_fstr = _resolve_all(condition, nodes_by_id)
    condition = _esc(condition)
    q = "f" if is_fstr else ""
    steps_part = f", max_steps={max_steps}" if max_steps is not None else ""
    common = f"session=s, llm={llm}{steps_part}"
    return f'await Condition({q}"{condition}", {common}).check()'


def _emit_loop_check(
    check_node: Node,
    check_nid: str,
    inner_indent: int,
    ctx: dict,
) -> tuple[list[str], str | None]:
    """Emit a Check that lives at a loop header/tail, with edge-driven break direction.

    Returns (lines, after_loop_nid).

    Routing rules — driven by the Check's outgoing edges, not by hard-coded
    convention:

    | True target outside loop | False target outside loop | Behavior |
    |--------------------------|---------------------------|----------|
    | yes                      | no/missing                | break on True,  jump to true_target |
    | no/missing               | yes                       | break on False, jump to false_target |
    | yes                      | yes                       | branch on True/False, both break, jump to respective target |
    | no/missing               | no/missing                | no break (both branches loop or are absent); fall through to next iteration |

    If the Check has no conditional edges but has a sequential successor outside
    the loop, treat it as if true→sequential_target (preserves legacy
    "break on True" behavior for graphs drawn without conditional edges).
    """
    inner_pad = "    " * inner_indent
    break_pad = "    " * (inner_indent + 1)
    check_expr = _emit_check_expr(check_node, ctx["global_cfg"], ctx["nodes_by_id"])

    cond = ctx["conditionals"].get(check_nid, {})
    true_target = cond.get("true")
    false_target = cond.get("false")
    loop_inner = ctx["loop_inner_members"]

    true_outside = bool(true_target) and true_target not in loop_inner
    false_outside = bool(false_target) and false_target not in loop_inner

    seq_outside = [
        e.target
        for e in ctx["out_edges"].get(check_nid, [])
        if e.type == "sequential" and e.target not in loop_inner
    ]

    # If there are no conditional edges but there IS a sequential successor
    # outside the loop, treat that as the True-exit path (legacy default).
    if not true_outside and not false_outside and seq_outside:
        true_outside = True
        true_target = seq_outside[0]

    lines: list[str] = []
    lines.append(f'{inner_pad}report_node({check_nid!r}, "running")')
    lines.append(f'{inner_pad}print("--- {check_nid}: {check_node.label} ---")')

    if true_outside and false_outside:
        # Both branches exit the loop with different destinations.
        # Properly supporting this requires post-loop runtime dispatch which the
        # current after_loop_nid interface doesn't expose. Surface as a clear
        # codegen error so the user can restructure.
        raise CodegenError(
            f"Check {check_nid!r} ({check_node.label!r}) has both conditional_true "
            f"and conditional_false edges leaving the loop. Post-loop dispatch on "
            f"two different exit targets is not yet supported. Route both branches "
            f"to the same node after the loop, or restructure so only one branch "
            f"exits the loop and the other loops back."
        )

    if true_outside:
        lines.append(f'{inner_pad}if {check_expr}:')
        lines.append(f'{break_pad}report_node({check_nid!r}, "success")')
        lines.append(f'{break_pad}break')
        return lines, true_target

    if false_outside:
        lines.append(f'{inner_pad}if not {check_expr}:')
        lines.append(f'{break_pad}report_node({check_nid!r}, "success")')
        lines.append(f'{break_pad}break')
        return lines, false_target

    # No exit at all — Check is informational. Still call it so its status updates.
    lines.append(f'{inner_pad}_ = {check_expr}')
    lines.append(f'{inner_pad}report_node({check_nid!r}, "success")')
    return lines, None


# ── Recursive subgraph emission ──────────────────────────────────────────────


def _emit_loop_group(
    lg: LoopGroup,
    indent: int,
    emitted: set[str],
    ctx: dict,
) -> tuple[list[str], str | None]:
    """Emit a retry loop group. Returns (lines, nid_to_continue_after_loop)."""
    lines: list[str] = []
    nodes_by_id = ctx["nodes_by_id"]
    global_cfg = ctx["global_cfg"]
    log_file_path = ctx["log_file_path"]
    loop_pad = "    " * indent
    inner_indent = indent + 1
    inner_pad = "    " * inner_indent
    break_pad = "    " * (inner_indent + 1)

    header_node = nodes_by_id[lg.header]
    tail_node = nodes_by_id[lg.tail]

    # ── Self-loop (header == tail) ───────────────────────────────────────
    if lg.header == lg.tail:
        lines.append(f"{loop_pad}for _attempt_{lg.header} in range({lg.max_iterations}):")
        if header_node.type == "Condition":
            check_lines, after_loop_nid = _emit_loop_check(header_node, lg.header, inner_indent, ctx)
            lines.extend(check_lines)
        else:
            lines.extend(
                _emit_node(
                    header_node,
                    global_cfg,
                    nodes_by_id,
                    inner_indent,
                    log_file_path,
                    ctx["prev_output_var"][0],
                )
            )
            if header_node.output_schema:
                ctx["prev_output_var"][0] = f"{lg.header}_out"
            after_loop_nid = None
        lines.append(f"{inner_pad}if _attempt_{lg.header} < {lg.max_iterations - 1}:")
        lines.append(f"{break_pad}await asyncio.sleep(3)")
        if header_node.type == "Condition" and (
            after_loop_nid is not None or any("break" in l for l in check_lines)
        ):
            lines.append(f"{loop_pad}else:")
            lines.append(f'{loop_pad}    report_node({lg.header!r}, "error")')
            lines.append(f'{loop_pad}    print("CRITICAL: Failed after {lg.max_iterations} attempts.")')
            lines.append(f"{loop_pad}    return")
        # If the Check helper picked an exit target, return it. Otherwise compute
        # from sequential successors that aren't the header itself.
        if after_loop_nid is None:
            loop_inner = ctx["loop_inner_members"]
            seq_after = [
                e.target
                for e in ctx["out_edges"].get(lg.header, [])
                if e.type == "sequential" and e.target != lg.header and e.target not in loop_inner
            ]
            after_loop_nid = seq_after[0] if seq_after else None
        return lines, after_loop_nid

    lines.append(f"{loop_pad}for _attempt_{lg.header} in range({lg.max_iterations}):")

    if header_node.type == "Condition":
        # Pattern A: Check is the loop header (check-first retry)
        for body_nid in lg.body:
            body_node = nodes_by_id[body_nid]
            lines.extend(
                _emit_node(
                    body_node,
                    global_cfg,
                    nodes_by_id,
                    inner_indent,
                    log_file_path,
                    ctx["prev_output_var"][0],
                )
            )
            if body_node.output_schema:
                ctx["prev_output_var"][0] = f"{body_nid}_out"
            emitted.add(body_nid)

        if tail_node.type != "Condition":
            lines.extend(
                _emit_node(
                    tail_node,
                    global_cfg,
                    nodes_by_id,
                    inner_indent,
                    log_file_path,
                    ctx["prev_output_var"][0],
                )
            )
            if tail_node.output_schema:
                ctx["prev_output_var"][0] = f"{lg.tail}_out"
        emitted.add(lg.tail)
        check_node, check_nid = header_node, lg.header
        check_lines, after_loop_nid = _emit_loop_check(check_node, check_nid, inner_indent, ctx)
        lines.extend(check_lines)
        lines.append(f"{inner_pad}if _attempt_{lg.header} < {lg.max_iterations - 1}:")
        lines.append(f"{break_pad}await asyncio.sleep(3)")
        # Only emit the for-else "CRITICAL" branch if the loop has an actual exit
        # condition (a break inside). Without a break, the for-loop runs to
        # completion normally — no failure state.
        if after_loop_nid is not None or any("break" in l for l in check_lines):
            lines.append(f"{loop_pad}else:")
            lines.append(f'{loop_pad}    report_node({check_nid!r}, "error")')
            lines.append(f'{loop_pad}    print("CRITICAL: Failed after {lg.max_iterations} attempts.")')
            lines.append(f"{loop_pad}    return")
    else:
        # Pattern B: non-Check header (e.g. Navigate → Check)
        lines.extend(
            _emit_node(
                header_node,
                global_cfg,
                nodes_by_id,
                inner_indent,
                log_file_path,
                ctx["prev_output_var"][0],
            )
        )
        if header_node.output_schema:
            ctx["prev_output_var"][0] = f"{lg.header}_out"

        for body_nid in lg.body:
            body_node = nodes_by_id[body_nid]
            lines.extend(
                _emit_node(
                    body_node,
                    global_cfg,
                    nodes_by_id,
                    inner_indent,
                    log_file_path,
                    ctx["prev_output_var"][0],
                )
            )
            if body_node.output_schema:
                ctx["prev_output_var"][0] = f"{body_nid}_out"
            emitted.add(body_nid)

        if tail_node.type == "Condition":
            # Tail is a Check — use it as the exit condition (edge-driven)
            emitted.add(lg.tail)
            check_node, check_nid = tail_node, lg.tail
            check_lines, after_loop_nid = _emit_loop_check(check_node, check_nid, inner_indent, ctx)
            lines.extend(check_lines)
            lines.append(f"{inner_pad}if _attempt_{lg.header} < {lg.max_iterations - 1}:")
            lines.append(f"{break_pad}await asyncio.sleep(3)")
            if after_loop_nid is not None or any("break" in l for l in check_lines):
                lines.append(f"{loop_pad}else:")
                lines.append(f'{loop_pad}    report_node({check_nid!r}, "error")')
                lines.append(f'{loop_pad}    print("CRITICAL: Failed after {lg.max_iterations} attempts.")')
                lines.append(f"{loop_pad}    return")
        else:
            # No Check at tail — unconditional retry loop, runs up to max_iterations times
            lines.extend(
                _emit_node(
                    tail_node,
                    global_cfg,
                    nodes_by_id,
                    inner_indent,
                    log_file_path,
                    ctx["prev_output_var"][0],
                )
            )
            if tail_node.output_schema:
                ctx["prev_output_var"][0] = f"{lg.tail}_out"
            emitted.add(lg.tail)
            lines.append(f"{inner_pad}if _attempt_{lg.header} < {lg.max_iterations - 1}:")
            lines.append(f"{break_pad}await asyncio.sleep(3)")
            # After unconditional loop: follow sequential successors of tail outside the loop
            loop_inner = ctx["loop_inner_members"]
            seq_after = [
                e.target
                for e in ctx["out_edges"].get(lg.tail, [])
                if e.type == "sequential" and e.target not in loop_inner
            ]
            after_loop_nid = seq_after[0] if seq_after else None

    return lines, after_loop_nid


def _emit_subgraph(
    start_nid: str,
    indent: int,
    emitted: set[str],
    stop_set: set[str],
    ctx: dict,
) -> list[str]:
    """Walk the graph from start_nid emitting code, stopping at stop_set or dead ends."""
    lines: list[str] = []
    nid: str | None = start_nid

    while nid and nid not in emitted and nid not in stop_set:
        node = ctx["nodes_by_id"].get(nid)
        if not node:
            break

        pad = "    " * indent
        out = ctx["out_edges"].get(nid, [])

        # ── ForEach ───────────────────────────────────────────────────────────
        if node.type == "ForEach":
            emitted.add(nid)
            loop_var = (node.config.get("loop_var") or "item").strip()
            items_expr = (node.config.get("items_expr") or "[]").strip()
            lines.append(f"{pad}report_node({nid!r}, 'running')")
            lines.append(f"{pad}for {loop_var} in ({items_expr}):")

            # Body: all non-foreach_done, non-loop_back outgoing edges
            body_targets = [
                e.target for e in out if e.type not in ("foreach_done", "loop_back")
            ]
            for body_start in body_targets:
                lines.extend(
                    _emit_subgraph(body_start, indent + 1, emitted, stop_set, ctx)
                )

            lines.append(f"{pad}report_node({nid!r}, 'success')")

            # After loop: foreach_done edge
            done = [e.target for e in out if e.type == "foreach_done"]
            nid = done[0] if done else None
            continue

        # ── Loop header ───────────────────────────────────────────────────────
        if nid in ctx["loop_headers"]:
            lg = ctx["loop_headers"][nid]
            emitted.add(nid)
            loop_lines, after_nid = _emit_loop_group(lg, indent, emitted, ctx)
            lines.extend(loop_lines)
            nid = after_nid
            continue

        # ── Conditional Check (standalone, not a loop inner member) ───────────
        if nid in ctx["conditionals"] and nid not in ctx["loop_inner_members"]:
            emitted.add(nid)
            cond = ctx["conditionals"][nid]
            check_expr = _emit_check_expr(node, ctx["global_cfg"], ctx["nodes_by_id"])

            lines.append(f'{pad}report_node("{nid}", "running")')
            lines.append(f'{pad}print("--- {nid}: {node.label} ---")')
            lines.append(f"{pad}if {check_expr}:")

            true_target = cond.get("true")
            false_target = cond.get("false")
            merge = _find_merge_point(
                true_target, false_target, ctx["out_edges"], ctx["topo_order"]
            )
            branch_stop = stop_set | ({merge} if merge else set())

            true_lines = (
                _emit_subgraph(true_target, indent + 1, emitted, branch_stop, ctx)
                if true_target
                else []
            )
            if true_lines:
                lines.extend(true_lines)
            else:
                lines.append(f"{'    ' * (indent + 1)}pass")

            if false_target:
                false_lines = _emit_subgraph(
                    false_target, indent + 1, emitted, branch_stop, ctx
                )
                if false_lines:
                    lines.append(f"{pad}else:")
                    lines.extend(false_lines)

            lines.append(f'{pad}report_node("{nid}", "success")')
            nid = merge
            continue

        # ── Skip loop inner members (body/tail handled by _emit_loop_group) ───
        if nid in ctx["loop_inner_members"]:
            break

        # ── Regular node ──────────────────────────────────────────────────────
        emitted.add(nid)
        node_lines = _emit_node(
            node,
            ctx["global_cfg"],
            ctx["nodes_by_id"],
            indent,
            ctx["log_file_path"],
            ctx["prev_output_var"][0],
        )
        lines.extend(node_lines)
        if node.output_schema:
            ctx["prev_output_var"][0] = f"{nid}_out"

        # Follow first sequential edge
        seq = [e.target for e in out if e.type == "sequential"]
        nid = seq[0] if seq else None

    return lines


# ── Main generation ──────────────────────────────────────────────────────────


def generate(graph_data: dict, log_file_path: str | None = None, inputs: dict | None = None) -> str:
    """Generate workflow.py source code from graph JSON."""
    global_cfg, nodes, edges = parse_graph(graph_data)
    nodes_by_id = {n.id: n for n in nodes}
    out_edges, in_edges = _build_adjacency(nodes, edges)
    loops = _detect_loops(edges)
    order = _topo_sort(nodes, edges, loops)

    # Build lookup structures
    loop_headers = {lg.header: lg for lg in loops}
    # loop_inner_members: body + tail (NOT headers — headers trigger loop emission)
    loop_inner_members: set[str] = set()
    for lg in loops:
        loop_inner_members.update(lg.body)
        loop_inner_members.add(lg.tail)

    # Detect conditional branches: Check nodes with true/false edges
    conditionals: dict[str, dict] = {}
    for nid, oe_list in out_edges.items():
        node = nodes_by_id.get(nid)
        if not node or node.type != "Condition":
            continue
        true_targets = [e.target for e in oe_list if e.type == "conditional_true"]
        false_targets = [e.target for e in oe_list if e.type == "conditional_false"]
        if true_targets or false_targets:
            conditionals[nid] = {
                "true": true_targets[0] if true_targets else None,
                "false": false_targets[0] if false_targets else None,
            }

    # ── Loop validation (fail fast on unsupported patterns) ───────────────
    # Reject duplicate loop_back edges with the same target — the loop_headers
    # dict would silently drop all but one.
    loop_back_targets: dict[str, int] = {}
    for e in edges:
        if e.type == "loop_back":
            loop_back_targets[e.target] = loop_back_targets.get(e.target, 0) + 1
    for header_nid, count in loop_back_targets.items():
        if count > 1:
            label = nodes_by_id[header_nid].label if header_nid in nodes_by_id else header_nid
            raise CodegenError(
                f"Node {header_nid!r} ({label!r}) is the target of {count} loop_back "
                f"edges. Only one loop_back can target a given node. Delete the extras "
                f"or restructure into nested loops (not yet supported)."
            )

    # Reject body patterns the loop emitter doesn't handle correctly.
    # _emit_loop_group iterates body nodes via _emit_node directly, which does
    # not understand conditional branches, ForEach iteration, or nested loops.
    # Surface these as clear errors instead of emitting silently-wrong code.
    for lg in loops:
        if lg.header == lg.tail:
            continue  # self-loops handled specially
        for body_nid in lg.body:
            body_node = nodes_by_id.get(body_nid)
            if body_node is None:
                continue
            label = body_node.label or body_nid
            if body_node.type == "ForEach":
                raise CodegenError(
                    f"Loop body contains a ForEach node {body_nid!r} ({label!r}). "
                    f"ForEach inside a loop_back loop is not yet supported. Move the "
                    f"ForEach outside the loop, or use ForEach instead of loop_back."
                )
            if body_nid in conditionals:
                raise CodegenError(
                    f"Loop body contains a conditional Check node {body_nid!r} "
                    f"({label!r}). Conditional branches inside a loop body are not "
                    f"yet supported. Restructure so the Check is the loop header or tail."
                )
            if body_nid in loop_headers:
                raise CodegenError(
                    f"Nested loop_back detected: node {body_nid!r} ({label!r}) is a "
                    f"loop header inside another loop's body. Nested loops are not "
                    f"yet supported."
                )

    # ── Build emission context ────────────────────────────────────────────
    ctx = {
        "nodes_by_id": nodes_by_id,
        "out_edges": out_edges,
        "in_edges": in_edges,
        "loop_headers": loop_headers,
        "loop_inner_members": loop_inner_members,
        "conditionals": conditionals,
        "global_cfg": global_cfg,
        "log_file_path": log_file_path,
        "topo_order": order,
        "prev_output_var": [
            None
        ],  # mutable single-element list so recursive calls share state
    }

    # ── Emit file ─────────────────────────────────────────────────────────
    lines: list[str] = []

    # Header
    lines.append("# AUTO-GENERATED by codegen.py -- do not edit by hand")
    lines.append("# Source: /workspace/workflow.json")
    lines.append("")
    lines.append("from dotenv import load_dotenv")
    lines.append("load_dotenv()")
    lines.append("")
    lines.append("import asyncio")

    # Conditional imports
    # Check if any node config references {{secrets.*}}
    def _has_secrets(cfg: dict) -> bool:
        return any(
            _SECRETS_RE.search(str(v))
            for v in cfg.values()
            if isinstance(v, (str, dict))
            for v in ([v] if isinstance(v, str) else v.values())
        )

    has_secrets = any(_has_secrets(n.config) for n in nodes)
    if has_secrets:
        lines.append("import os")

    has_schema = any(n.output_schema for n in nodes)
    if has_schema:
        lines.append("import json")
        lines.append("from pydantic import BaseModel")

    # MCP toolset import (only if any node uses mcp_servers)
    has_mcp = any(n.config.get("mcp_servers") for n in nodes)
    if has_mcp:
        lines.append(
            "from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset as _MCPToolset"
        )
        lines.append(
            "from google.adk.tools.mcp_tool.mcp_toolset import StdioServerParameters as _StdioParams"
        )
        lines.append(
            "from google.adk.tools.mcp_tool.mcp_toolset import SseServerParams as _SseServerParams"
        )

    # ForEach/Setup generate plain Python — no mobius_core verb class to import
    verb_types = {n.type for n in nodes if n.type not in ("Code", "Agent", "ForEach", "Setup")}
    verb_imports = sorted(verb_types)
    has_agent_nodes = any(n.type == "Agent" for n in nodes)
    has_setup_nodes = any(n.type == "Setup" for n in nodes)
    if has_agent_nodes:
        # BaseActionAgent must also come from mobius_core
        mobius_core_imports = sorted(verb_types | {"BaseActionAgent"})
    else:
        mobius_core_imports = verb_imports
    if mobius_core_imports:
        lines.append(f"from mobius_core import {', '.join(mobius_core_imports)}, session")
    else:
        lines.append("from mobius_core import session")
    if has_setup_nodes:
        lines.append("from mobius_core import Setup")
    lines.append("from state import pause_event, report_node, report_node_output, report_node_log")
    lines.append(f"_inputs = {repr(inputs or {})}")
    lines.append("")
    if log_file_path:
        lines.append(f"_LOG_FILE = {log_file_path!r}")
        lines.append("")

    # Pydantic models (for nodes with output_schema, excluding Agent nodes which handle their own)
    model_lines = _emit_pydantic_models([n for n in nodes if n.type != "Agent"])
    if model_lines:
        lines.append("")
        lines.extend(model_lines)

    # Custom Agent verb classes
    agent_nodes = [n for n in nodes if n.type == "Agent"]
    for node in agent_nodes:
        class_name = node.config.get("class_name", "").strip()
        prompt_template = node.config.get("prompt_template", "").strip()
        if not class_name:
            raise CodegenError(f"Agent node {node.id!r}: class_name is required")
        if not class_name.isidentifier():
            raise CodegenError(
                f"Agent node {node.id!r}: {class_name!r} is not a valid Python identifier"
            )
        if not prompt_template:
            raise CodegenError(f"Agent node {node.id!r}: prompt_template is required")
        # Pydantic output model
        if node.output_schema:
            out_cls = node.output_schema.class_name(node.id)
            lines.append(f"class {out_cls}(BaseModel):")
            for f in node.output_schema.fields:
                lines.append(f"    {f.name}: {f.python_type()}")
            lines.append("")
        # Verb class
        body = prompt_template.replace("{task}", "{self._task}")
        lines.append(f"class {class_name}(BaseActionAgent):")
        lines.append(f"    def __init__(self, task: str, **kw):")
        lines.append(f"        super().__init__(**kw)")
        lines.append(f"        self._task = task")
        lines.append(f"")
        lines.append(f"    def task_prompt(self) -> str:")
        lines.append(f'        return f"{body}"')
        if node.output_schema:
            out_cls = node.output_schema.class_name(node.id)
            lines.append(f"")
            lines.append(f"    def output_schema(self):")
            lines.append(f"        return {out_cls}")
        lines.append("")

    # Main function
    lines.append("")
    lines.append("async def main(pause_event):")
    lines.append(f'    model = "{global_cfg.llm}"')
    lines.append(f"    verbose = {global_cfg.verbose}")
    lines.append("")
    lines.append("    import sys as _sys")
    if log_file_path:
        lines.append(
            '    _log_fh = open(_LOG_FILE, "w", encoding="utf-8", buffering=1)'
        )
        lines.append("    class _WFTee:")
        lines.append("        def __init__(self, a, b): self._a, self._b = a, b")
        lines.append("        def write(self, d): self._a.write(d); self._b.write(d)")
        lines.append("        def flush(self): self._a.flush(); self._b.flush()")
        lines.append("        def __getattr__(self, n): return getattr(self._a, n)")
        lines.append("    _sys.stdout = _WFTee(_sys.__stdout__, _log_fh)")
        lines.append("    _sys.stderr = _WFTee(_sys.__stderr__, _log_fh)")
    # Per-node stdout capture — wraps whatever stdout is now (plain or Tee'd)
    # and streams each printed line to the UI via report_node_log.
    lines.append("    _current_log_node = [None]")
    lines.append("    class _NodeLogTee:")
    lines.append("        def __init__(self, w): self._w = w")
    lines.append("        def write(self, s):")
    lines.append("            self._w.write(s)")
    lines.append("            self._w.flush()")
    lines.append("            nid = _current_log_node[0]")
    lines.append("            if nid and s.strip():")
    lines.append("                report_node_log(nid, s.rstrip('\\n'))")
    lines.append("        def flush(self): self._w.flush()")
    lines.append("        def __getattr__(self, n): return getattr(self._w, n)")
    lines.append("    _sys.stdout = _NodeLogTee(_sys.stdout)")
    lines.append("    _sys.stderr = _NodeLogTee(_sys.stderr)")
    # Re-attach logging handlers so they pick up the new stderr wrapper.
    lines.append("    import logging as _logging")
    lines.append("    for _lname in ('mobius_core', 'root'):")
    lines.append("        _lg = _logging.getLogger(_lname) if _lname != 'root' else _logging.getLogger()")
    lines.append("        for _h in list(_lg.handlers):")
    lines.append("            if isinstance(_h, _logging.StreamHandler) and not isinstance(_h, _logging.FileHandler):")
    lines.append("                _h.stream = _sys.stderr")
    lines.append("")
    lines.append("    async with session() as s:")

    # Find root nodes: no incoming DAG edges (excluding loop_back) and not loop inner members
    dag_in_degree: dict[str, int] = {n.id: 0 for n in nodes}
    for e in edges:
        if e.type != "loop_back":
            dag_in_degree[e.target] = dag_in_degree.get(e.target, 0) + 1

    # A node that is a loop header (i.e. the target of a loop_back edge) is also
    # a valid entry point even if it's also a tail/body member of that same loop
    # (self-loop case). So allow loop_headers through the inner-members filter.
    roots = [
        nid
        for nid in order
        if dag_in_degree.get(nid, 0) == 0
        and (nid not in loop_inner_members or nid in loop_headers)
    ]

    # Emit all subgraphs from roots using recursive emission
    emitted: set[str] = set()
    for root in roots:
        if root not in emitted:
            lines.extend(_emit_subgraph(root, 2, emitted, set(), ctx))

    lines.append("        report_node('__workflow__', 'success')")
    lines.append(
        "        print('Workflow completed. Holding screen open... (Click Stop in UI to exit)')"
    )
    lines.append("        try:")
    lines.append("            while True:")
    lines.append("                await asyncio.sleep(1)")
    lines.append("        except asyncio.CancelledError:")
    lines.append("            pass")

    # Add blank line and __main__ block
    lines.append("")
    lines.append("")
    lines.append('if __name__ == "__main__":')
    lines.append("    asyncio.run(main(pause_event))")
    lines.append("")

    return "\n".join(lines)


# ── CLI entry point ──────────────────────────────────────────────────────────


def generate_from_file(
    input_path: str = "/workspace/workflow.json",
    output_path: str = "/workspace/workflow.py",
) -> str:
    """Read graph JSON, generate workflow.py, write to disk."""
    data = json.loads(Path(input_path).read_text())
    code = generate(data)
    Path(output_path).write_text(code)
    return code


if __name__ == "__main__":
    code = generate_from_file()
    print(f"Generated {len(code)} bytes -> /workspace/workflow.py")
