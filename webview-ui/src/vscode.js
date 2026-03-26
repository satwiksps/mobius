const vscodeApi = window.acquireVsCodeApi ? window.acquireVsCodeApi() : null;

let nextReqId = 1;
const pendingRequests = new Map();
const eventListeners = new Set();

// Listen for messages from the extension host
window.addEventListener("message", (event) => {
  const message = event.data;
  
  if (message.type === "rpc-response") {
    const pending = pendingRequests.get(message.id);
    if (pending) {
      pendingRequests.delete(message.id);
      if (message.error) {
        pending.reject(new Error(message.error));
      } else {
        pending.resolve(message.result);
      }
    }
  } else if (message.type === "event") {
    // Broadcast event to all listeners
    for (const listener of eventListeners) {
      listener(message.data);
    }
  }
});

// Generic RPC call
export function rpc(method, params = {}) {
  return new Promise((resolve, reject) => {
    if (!vscodeApi) {
      return reject(new Error("VS Code API not available. Are you running in a webview?"));
    }
    
    const id = String(nextReqId++);
    pendingRequests.set(id, { resolve, reject });
    
    vscodeApi.postMessage({
      type: "rpc",
      id,
      method,
      params
    });
    
    // Timeout after 30s
    setTimeout(() => {
      if (pendingRequests.has(id)) {
        pendingRequests.delete(id);
        reject(new Error(`RPC ${method} timed out`));
      }
    }, 30000);
  });
}

// Subscribe to backend events (stdout JSON lines with "event")
export function onEvent(callback) {
  eventListeners.add(callback);
  return () => eventListeners.delete(callback);
}

// High-level API bindings
export const api = {
  listWorkflows: () => rpc("list_workflows"),
  loadWorkflow: (id) => rpc("load_workflow", { id }),
  saveWorkflow: (id, name, nodes, edges) => rpc("save_workflow", { id, name, nodes, edges }),
  deleteWorkflow: (id) => rpc("delete_workflow", { id }),
  runWorkflow: (id) => rpc("run_workflow", { id }),
  stopWorkflow: () => rpc("stop_workflow"),
  getStatus: () => rpc("get_status") // Check if something is currently running
};
