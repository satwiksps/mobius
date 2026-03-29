import React, { useState, useEffect, useCallback, useRef } from 'react';
import { 
  ReactFlow, 
  Controls, 
  Background, 
  useNodesState, 
  useEdgesState, 
  addEdge,
  Handle,
  Position
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { api, onEvent } from './vscode';

// --- Custom Node ---
const ZikloNode = ({ data, isConnectable, id, selected }) => {
  const isCheck = data.type === 'Condition';

  const style = {
    padding: '8px',
    minWidth: '150px',
    border: selected ? '1px solid var(--vscode-focusBorder)' : '1px solid var(--vscode-panel-border)',
    borderRadius: '4px',
    background: 'var(--vscode-sideBar-background)',
    color: 'var(--vscode-foreground)',
    fontSize: '12px'
  };

  if (data.status === 'running') {
    style.animation = 'ziklo-pulse 1s ease-in-out infinite';
  } else if (data.status === 'success') {
    style.borderLeft = '3px solid var(--vscode-testing-iconPassed)';
  } else if (data.status === 'failed') {
    style.borderLeft = '3px solid var(--vscode-testing-iconFailed)';
  }

  return (
    <div style={style}>
      <Handle type="target" position={Position.Top} isConnectable={isConnectable} />
      
      <div style={{ fontWeight: 'bold', marginBottom: '4px', display: 'flex', justifyContent: 'space-between' }}>
        <span>{data.type}</span>
        <span style={{ opacity: 0.5 }}>{data.label || id}</span>
      </div>
      
      <div style={{ fontSize: '11px', opacity: 0.8, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
        {data.task || data.target || data.condition || data.packages || 'No config'}
      </div>

      <Handle type="source" position={Position.Bottom} isConnectable={isConnectable} />
      
      {isCheck && (
        <>
          <Handle type="source" position={Position.Right} id="true" style={{ top: '30%', background: 'var(--vscode-testing-iconPassed)' }} isConnectable={isConnectable} />
          <Handle type="source" position={Position.Right} id="false" style={{ top: '70%', background: 'var(--vscode-testing-iconFailed)' }} isConnectable={isConnectable} />
        </>
      )}
    </div>
  );
};

const nodeTypes = { custom: ZikloNode };

export default function App() {
  // State
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [workflows, setWorkflows] = useState([]);
  const [currentWfId, setCurrentWfId] = useState('');
  const [selectedNode, setSelectedNode] = useState(null);
  const [logs, setLogs] = useState([]);
  const [isRunning, setIsRunning] = useState(false);
  const logEndRef = useRef(null);

  // Initialize
  useEffect(() => {
    loadWorkflows();
    const unsub = onEvent(handleEvent);
    return () => unsub();
  }, []);

  useEffect(() => {
    if (logEndRef.current) {
      logEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs]);

  // IPC handlers
  const loadWorkflows = async () => {
    try {
      const wfs = await api.listWorkflows();
      setWorkflows(wfs);
      if (wfs.length > 0 && !currentWfId) {
        handleLoadWorkflow(wfs[0].id);
      }
    } catch (e) {
      logError("Failed to list workflows: " + e.message);
    }
  };

  const handleLoadWorkflow = async (id) => {
    try {
      const wf = await api.loadWorkflow(id);
      setCurrentWfId(id);
      setNodes(wf.nodes || []);
      setEdges(wf.edges || []);
      setLogs([]);
      setSelectedNode(null);
    } catch (e) {
      logError("Failed to load workflow: " + e.message);
    }
  };

  const handleSaveWorkflow = async () => {
    if (!currentWfId) return;
    try {
      const wf = workflows.find(w => w.id === currentWfId);
      
      // Attempt to parse 'data' string in Input nodes
      const parsedNodes = nodes.map(n => {
        if (n.data.type === 'Input' && typeof n.data.data === 'string') {
          try {
            return { ...n, data: { ...n.data, data: JSON.parse(n.data.data) } };
          } catch {
            return n; // fallback to string if invalid
          }
        }
        return n;
      });

      await api.saveWorkflow(currentWfId, wf?.name || 'Untitled', parsedNodes, edges);
      logMsg(`Workflow saved.`);
    } catch (e) {
      logError("Save failed: " + e.message);
    }
  };

  const handleRun = async () => {
    if (!currentWfId) return;
    await handleSaveWorkflow();
    setIsRunning(true);
    setLogs([]);
    
    // Reset node statuses
    setNodes(nds => nds.map(n => ({ ...n, data: { ...n.data, status: 'idle' } })));
    
    try {
      await api.runWorkflow(currentWfId);
    } catch (e) {
      logError("Run failed: " + e.message);
      setIsRunning(false);
    }
  };

  const handleStop = async () => {
    try {
      await api.stopWorkflow();
      setIsRunning(false);
      logMsg("Workflow stopped.");
    } catch (e) {
      logError("Stop failed: " + e.message);
    }
  };

  // Real-time events
  const handleEvent = (evt) => {
    if (evt.event === 'log') {
      logMsg(evt.message, evt.level);
    } else if (evt.event === 'node_status') {
      const { node_id, status } = evt;
      setNodes(nds => nds.map(n => 
        n.id === node_id ? { ...n, data: { ...n.data, status } } : n
      ));
    } else if (evt.event === 'workflow_status') {
      if (evt.status === 'completed' || evt.status === 'failed' || evt.status === 'stopped') {
        setIsRunning(false);
        logMsg(`Workflow ${evt.status}.`);
      }
    }
  };

  const logMsg = (msg, level = 'info') => {
    setLogs(prev => [...prev, { time: new Date().toLocaleTimeString(), msg, level }]);
  };
  const logError = (msg) => logMsg(msg, 'error');

  // React Flow Handlers
  const onConnect = useCallback((params) => setEdges((eds) => addEdge(params, eds)), [setEdges]);
  const onNodeClick = (e, node) => setSelectedNode(node);
  const onPaneClick = () => setSelectedNode(null);

  const addNode = (type) => {
    const newNode = {
      id: `${type}-${Date.now()}`,
      type: 'custom',
      position: { x: Math.random() * 200 + 100, y: Math.random() * 200 + 100 },
      data: { type, label: '', status: 'idle' }
    };
    if (type === 'Browse') newNode.data.target = '';
    if (type === 'Action') newNode.data.task = '';
    if (type === 'Extract') newNode.data.task = '';
    if (type === 'Condition') newNode.data.condition = '';
    if (type === 'Input') { newNode.data.target = ''; newNode.data.data = {}; }
    if (type === 'Setup') newNode.data.packages = '';
    setNodes(nds => [...nds, newNode]);
  };

  const updateNodeData = (id, key, value) => {
    setNodes(nds => nds.map(n => {
      if (n.id === id) {
        n.data = { ...n.data, [key]: value };
        if (selectedNode?.id === id) setSelectedNode(n); // Update panel
      }
      return n;
    }));
  };

  // --- Layout Styles ---
  const layout = {
    display: 'flex',
    flexDirection: 'column',
    height: '100vh',
    background: 'var(--vscode-editor-background)',
    color: 'var(--vscode-editor-foreground)',
    fontFamily: 'var(--vscode-font-family)'
  };

  const toolbar = {
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    padding: '8px 16px',
    background: 'var(--vscode-sideBar-background)',
    borderBottom: '1px solid var(--vscode-panel-border)'
  };

  const btn = {
    background: 'var(--vscode-button-background)',
    color: 'var(--vscode-button-foreground)',
    border: 'none',
    padding: '4px 12px',
    borderRadius: '2px',
    cursor: 'pointer'
  };

  const select = {
    background: 'var(--vscode-input-background)',
    color: 'var(--vscode-input-foreground)',
    border: '1px solid var(--vscode-input-border)',
    padding: '4px 8px'
  };

  const input = { ...select, width: '100%', boxSizing: 'border-box' };

  return (
    <div style={layout}>
      {/* Toolbar */}
      <div style={toolbar}>
        <span style={{ fontWeight: 'bold' }}>Ziklo</span>
        
        <select style={select} value={currentWfId} onChange={e => handleLoadWorkflow(e.target.value)}>
          <option value="">Select workflow...</option>
          {workflows.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
        </select>
        
        <button style={btn} onClick={handleSaveWorkflow}>Save</button>
        
        <div style={{ flex: 1 }}></div>

        {!isRunning ? (
          <button style={{ ...btn, background: 'var(--vscode-testing-iconPassed)' }} onClick={handleRun}>▶ Run</button>
        ) : (
          <button style={{ ...btn, background: 'var(--vscode-testing-iconFailed)' }} onClick={handleStop}>■ Stop</button>
        )}
      </div>

      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        {/* React Flow Canvas */}
        <div style={{ flex: 1, position: 'relative' }}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            nodeTypes={nodeTypes}
            onNodeClick={onNodeClick}
            onPaneClick={onPaneClick}
            fitView
          >
            <Background />
            <Controls />
          </ReactFlow>
        </div>

        {/* Right Sidebar */}
        <div style={{ width: '300px', display: 'flex', flexDirection: 'column', borderLeft: '1px solid var(--vscode-panel-border)', background: 'var(--vscode-sideBar-background)' }}>
          
          {/* Palette */}
          <div style={{ padding: '12px', borderBottom: '1px solid var(--vscode-panel-border)' }}>
            <div style={{ fontSize: '11px', textTransform: 'uppercase', marginBottom: '8px', opacity: 0.7 }}>Add Node</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
              {['Setup', 'Browse', 'Action', 'Extract', 'Condition', 'Input'].map(t => (
                <button key={t} style={{ ...btn, background: 'var(--vscode-button-secondaryBackground)', color: 'var(--vscode-button-secondaryForeground)' }} onClick={() => addNode(t)}>{t}</button>
              ))}
            </div>
          </div>

          {/* Config Panel */}
          <div style={{ padding: '12px', borderBottom: '1px solid var(--vscode-panel-border)', flex: 1, overflowY: 'auto' }}>
            <div style={{ fontSize: '11px', textTransform: 'uppercase', marginBottom: '8px', opacity: 0.7 }}>Configuration</div>
            {selectedNode ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                <div>
                  <label style={{ fontSize: '11px', display: 'block', marginBottom: '4px' }}>Type</label>
                  <div>{selectedNode.data.type}</div>
                </div>
                <div>
                  <label style={{ fontSize: '11px', display: 'block', marginBottom: '4px' }}>Label</label>
                  <input style={input} value={selectedNode.data.label || ''} onChange={e => updateNodeData(selectedNode.id, 'label', e.target.value)} />
                </div>
                
                {selectedNode.data.type === 'Setup' && (
                  <div>
                    <label style={{ fontSize: '11px', display: 'block', marginBottom: '4px' }}>System Packages (comma-separated)</label>
                    <input style={input} value={selectedNode.data.packages || ''} onChange={e => updateNodeData(selectedNode.id, 'packages', e.target.value)} />
                  </div>
                )}

                {selectedNode.data.type === 'Browse' && (
                  <div>
                    <label style={{ fontSize: '11px', display: 'block', marginBottom: '4px' }}>URL / Target</label>
                    <input style={input} value={selectedNode.data.target || ''} onChange={e => updateNodeData(selectedNode.id, 'target', e.target.value)} />
                  </div>
                )}

                {(selectedNode.data.type === 'Action' || selectedNode.data.type === 'Extract') && (
                  <div>
                    <label style={{ fontSize: '11px', display: 'block', marginBottom: '4px' }}>Task Description</label>
                    <textarea style={{ ...input, height: '80px', resize: 'vertical' }} value={selectedNode.data.task || ''} onChange={e => updateNodeData(selectedNode.id, 'task', e.target.value)} />
                  </div>
                )}

                {selectedNode.data.type === 'Condition' && (
                  <div>
                    <label style={{ fontSize: '11px', display: 'block', marginBottom: '4px' }}>Condition</label>
                    <input style={input} value={selectedNode.data.condition || ''} onChange={e => updateNodeData(selectedNode.id, 'condition', e.target.value)} />
                  </div>
                )}
                
                {selectedNode.data.type === 'Input' && (
                  <>
                    <div>
                      <label style={{ fontSize: '11px', display: 'block', marginBottom: '4px' }}>Target Form</label>
                      <input style={input} value={selectedNode.data.target || ''} onChange={e => updateNodeData(selectedNode.id, 'target', e.target.value)} />
                    </div>
                    <div>
                      <label style={{ fontSize: '11px', display: 'block', marginBottom: '4px' }}>Data JSON</label>
                      <textarea 
                        style={{ ...input, height: '100px', resize: 'vertical', fontFamily: 'monospace' }} 
                        value={typeof selectedNode.data.data === 'string' ? selectedNode.data.data : JSON.stringify(selectedNode.data.data || {}, null, 2)}
                        onChange={e => {
                          try {
                            const val = e.target.value;
                            updateNodeData(selectedNode.id, 'data', val); // Store string for typing, parse on save
                          } catch {}
                        }}
                      />
                    </div>
                  </>
                )}
                
                <button 
                  style={{ ...btn, background: 'var(--vscode-testing-iconFailed)', marginTop: '8px' }}
                  onClick={() => {
                    setNodes(nds => nds.filter(n => n.id !== selectedNode.id));
                    setEdges(eds => eds.filter(e => e.source !== selectedNode.id && e.target !== selectedNode.id));
                    setSelectedNode(null);
                  }}
                >
                  Delete Node
                </button>
              </div>
            ) : (
              <div style={{ opacity: 0.5, fontSize: '12px' }}>Select a node to configure it.</div>
            )}
          </div>

          {/* Execution Log */}
          <div style={{ height: '200px', display: 'flex', flexDirection: 'column' }}>
            <div style={{ padding: '8px 12px', fontSize: '11px', textTransform: 'uppercase', background: 'var(--vscode-editor-background)', borderBottom: '1px solid var(--vscode-panel-border)', opacity: 0.7 }}>
              Execution Log
            </div>
            <div style={{ flex: 1, overflowY: 'auto', padding: '8px', fontSize: '12px', fontFamily: 'monospace', background: 'var(--vscode-editor-background)' }}>
              {logs.length === 0 && <div style={{ opacity: 0.5 }}>No logs yet...</div>}
              {logs.map((l, i) => (
                <div key={i} style={{ marginBottom: '4px', color: l.level === 'error' ? 'var(--vscode-testing-iconFailed)' : 'inherit' }}>
                  <span style={{ opacity: 0.5, marginRight: '8px' }}>[{l.time}]</span>
                  {l.msg}
                </div>
              ))}
              <div ref={logEndRef} />
            </div>
          </div>

        </div>
      </div>
    </div>
  );
}
