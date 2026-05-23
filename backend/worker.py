"""
Ziklo Python Worker — communicates with the VS Code extension host
via JSON-RPC over stdin/stdout.
"""
import sys
import json
import traceback
import sqlite3
import os
import time
import uuid
from pathlib import Path
import asyncio
import threading
import importlib.util
import codegen
import state

DB_PATH = Path(os.environ.get("ZIKLO_DB_PATH", "ziklo.db"))
active_run_task = None
active_run_id = None
input_queue = asyncio.Queue()


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS workflows (
                id      TEXT PRIMARY KEY,
                name    TEXT NOT NULL DEFAULT 'Untitled',
                graph   TEXT NOT NULL DEFAULT '{}',
                created REAL NOT NULL,
                updated REAL NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id            TEXT PRIMARY KEY,
                workflow_id   TEXT NOT NULL,
                workflow_name TEXT NOT NULL DEFAULT '',
                started_at    REAL NOT NULL,
                finished_at   REAL,
                status        TEXT,
                log           TEXT DEFAULT ''
            )
        """)


def handle_request(req: dict) -> dict | list | None:
    method = req.get("method")
    params = req.get("params", {})

    if method == "ping":
        return {"status": "ok", "version": "0.1.0"}

    elif method == "list_workflows":
        with _db() as con:
            rows = con.execute(
                "SELECT id, name, created, updated FROM workflows ORDER BY updated DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    elif method == "load_workflow":
        wf_id = params.get("id")
        if not wf_id:
            return None
        with _db() as con:
            row = con.execute("SELECT * FROM workflows WHERE id = ?", (wf_id,)).fetchone()
            if row:
                wf = dict(row)
                graph = json.loads(wf["graph"])
                wf["nodes"] = graph.get("nodes", []) if isinstance(graph, dict) else []
                wf["edges"] = graph.get("edges", []) if isinstance(graph, dict) else []
                return wf
            return None

    elif method == "save_workflow":
        wf_id = params.get("id") or str(uuid.uuid4())
        name = params.get("name", "Untitled")
        nodes = params.get("nodes", [])
        edges = params.get("edges", [])
        graph = json.dumps({"nodes": nodes, "edges": edges})
        now = time.time()

        with _db() as con:
            con.execute(
                """INSERT INTO workflows (id, name, graph, created, updated)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name = COALESCE(excluded.name, workflows.name),
                     graph = excluded.graph,
                     updated = excluded.updated""",
                (wf_id, name, graph, now, now),
            )
            con.commit()
        return {"success": True, "id": wf_id}

    elif method == "delete_workflow":
        wf_id = params.get("id")
        with _db() as con:
            con.execute("DELETE FROM workflows WHERE id = ?", (wf_id,))
            con.commit()
        return {"success": True}

    elif method == "rename_workflow":
        wf_id = params.get("id")
        name = params.get("name", "Untitled")
        now = time.time()
        with _db() as con:
            con.execute(
                "UPDATE workflows SET name = ?, updated = ? WHERE id = ?",
                (name, now, wf_id),
            )
            con.commit()
        return {"success": True, "id": wf_id, "name": name}

    elif method == "list_runs":
        wf_id = params.get("workflowId", "")
        with _db() as con:
            rows = con.execute(
                "SELECT * FROM runs WHERE workflow_id = ? ORDER BY started_at DESC LIMIT 50",
                (wf_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    elif method == "get_run_log":
        run_id = params.get("runId")
        with _db() as con:
            row = con.execute("SELECT log FROM runs WHERE id = ?", (run_id,)).fetchone()
            return row["log"] if row else ""

    elif method == "get_status":
        global active_run_task, active_run_id
        is_running = active_run_task is not None and not active_run_task.done()
        return {"running": is_running, "run_id": active_run_id if is_running else None}

    elif method == "preview_workflow":
        wf_id = params.get("id")
        with _db() as con:
            row = con.execute("SELECT graph FROM workflows WHERE id = ?", (wf_id,)).fetchone()
        if not row:
            return {"code": "# Workflow not found."}
        try:
            graph_data = json.loads(row["graph"])
            code = codegen.generate(graph_data, log_file_path=None, inputs={})
            return {"code": code}
        except Exception as e:
            return {"code": f"# Codegen failed: {e}"}

    elif method == "list_files":
        return {"entries": []}

    elif method == "delete_file":
        return {"success": True}

    else:
        raise ValueError(f"Unknown method: {method}")


async def run_workflow_async(req_id, params):
    global active_run_task, active_run_id
    wf_id = params.get("id")
    if active_run_task and not active_run_task.done():
        return {"success": False, "error": "A workflow is already running"}

    # Reset execution state
    state.reset_execution()
    state.pause_event.set()

    # Load workflow graph from DB
    with _db() as con:
        row = con.execute("SELECT name, graph FROM workflows WHERE id = ?", (wf_id,)).fetchone()
    if not row:
        return {"success": False, "error": "Workflow not found"}

    name = row["name"]
    graph_data = json.loads(row["graph"])

    # Compile to python
    run_id = str(uuid.uuid4())
    active_run_id = run_id
    try:
        code = codegen.generate(graph_data, log_file_path=None, inputs=params.get("inputs", {}))
        run_py = Path(__file__).parent / "workflow_run.py"
        run_py.write_text(code, encoding="utf-8")
    except Exception as e:
        return {"success": False, "error": f"Codegen failed: {e}"}

    # Import the compiled module
    try:
        if "workflow_run" in sys.modules:
            del sys.modules["workflow_run"]
        spec = importlib.util.spec_from_file_location("workflow_run", str(run_py))
        wf_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wf_mod)
    except Exception as e:
        return {"success": False, "error": f"Import failed: {e}"}

    # Start execution in background
    now = time.time()
    with _db() as con:
        con.execute(
            """INSERT INTO runs (id, workflow_id, workflow_name, started_at, status, log)
               VALUES (?, ?, ?, ?, 'running', '')""",
            (run_id, wf_id, name, now),
        )
        con.commit()

    async def run_wrapper():
        global active_run_id
        final_status = "success"
        error_msg = ""
        try:
            state.report_node("__workflow__", "running")
            await wf_mod.main(pause_event=state.pause_event)
            state.report_node("__workflow__", "success")
        except asyncio.CancelledError:
            final_status = "stopped"
            state.report_node("__workflow__", "stopped")
        except Exception as e:
            final_status = "error"
            error_msg = traceback.format_exc()
            state.report_node("__workflow__", "error")
            print(f"Error during run: {e}\n{error_msg}")
        finally:
            # Accumulate logs from state.py
            logs = state.get_node_logs()
            log_text = json.dumps(logs)
            
            # Update DB
            with _db() as con:
                con.execute(
                    "UPDATE runs SET finished_at = ?, status = ?, log = ? WHERE id = ?",
                    (time.time(), final_status, log_text, run_id),
                )
                con.commit()
            
            # Send run_ended event to stdout
            state._send_event("workflow_ended", {"run_id": run_id, "status": final_status})

    active_run_task = asyncio.create_task(run_wrapper())
    return {"success": True, "run_id": run_id}


async def stop_workflow_async():
    global active_run_task
    if active_run_task and not active_run_task.done():
        active_run_task.cancel()
        try:
            await active_run_task
        except asyncio.CancelledError:
            pass
        return {"success": True}
    return {"success": False, "error": "No workflow running"}


def stdin_thread(loop, queue):
    for line in sys.stdin:
        loop.call_soon_threadsafe(queue.put_nowait, line)


async def main_async():
    init_db()
    loop = asyncio.get_running_loop()

    # Start stdin thread
    t = threading.Thread(target=stdin_thread, args=(loop, input_queue), daemon=True)
    t.start()

    while True:
        line = await input_queue.get()
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params", {})

            if method == "run_workflow":
                result = await run_workflow_async(req_id, params)
            elif method == "stop_workflow":
                result = await stop_workflow_async()
            else:
                result = handle_request(req)

            resp = {"id": req_id, "result": result}
        except Exception as e:
            resp = {
                "id": req.get("id") if "req" in locals() else None,
                "error": str(e),
                "traceback": traceback.format_exc(),
            }

        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

