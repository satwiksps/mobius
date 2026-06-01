"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = require("vscode");
const path = require("path");
const fs = require("fs");
const child_process_1 = require("child_process");
const crypto = require("crypto");
// ── Python Worker ────────────────────────────────────────────────────────────
class PythonWorker {
    context;
    process;
    pendingRequests = new Map();
    nextId = 0;
    buffer = '';
    outputChannel;
    constructor(context) {
        this.context = context;
        this.outputChannel = vscode.window.createOutputChannel('Ziklo');
    }
    async ensureStarted() {
        if (this.process) {
            return;
        }
        const backendPath = path.join(this.context.extensionPath, 'backend');
        const venvPath = path.join(backendPath, '.venv');
        const isWin = process.platform === 'win32';
        const pyExe = isWin ? path.join(venvPath, 'Scripts', 'python.exe') : path.join(venvPath, 'bin', 'python');
        const pipExe = isWin ? path.join(venvPath, 'Scripts', 'pip.exe') : path.join(venvPath, 'bin', 'pip');
        // First-run setup with progress
        if (!fs.existsSync(pyExe)) {
            await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: 'Ziklo', cancellable: false }, async (progress) => {
                progress.report({ message: 'Creating Python environment...' });
                this.outputChannel.appendLine('Creating .venv...');
                const venvCode = await this.runProcess('python', ['-m', 'venv', '.venv'], backendPath);
                if (venvCode !== 0) {
                    throw new Error('Failed to create Python venv. Make sure Python 3.10+ is installed.');
                }
                progress.report({ message: 'Installing dependencies (this may take a minute)...' });
                this.outputChannel.appendLine('Installing dependencies...');
                const pipCode = await this.runProcess(pipExe, ['install', '-r', 'requirements.txt', '--quiet'], backendPath);
                if (pipCode !== 0) {
                    throw new Error('Failed to install Python dependencies. Check the Ziklo output channel for details.');
                }
                this.outputChannel.appendLine('Dependencies installed successfully.');
            });
        }
        // Ensure storage directory exists
        const storagePath = this.context.globalStorageUri.fsPath;
        if (!fs.existsSync(storagePath)) {
            fs.mkdirSync(storagePath, { recursive: true });
        }
        const dbPath = path.join(storagePath, 'ziklo.db');
        this.outputChannel.appendLine('Starting Ziklo worker...');
        this.process = (0, child_process_1.spawn)(pyExe, ['worker.py'], {
            cwd: backendPath,
            env: { ...process.env, ZIKLO_DB_PATH: dbPath, PYTHONUNBUFFERED: '1' }
        });
        this.process.stdout?.on('data', (data) => {
            this.buffer += data.toString();
            const lines = this.buffer.split('\n');
            this.buffer = lines.pop() || '';
            for (const line of lines) {
                if (!line.trim()) {
                    continue;
                }
                try {
                    const resp = JSON.parse(line);
                    if (resp && resp.event) {
                        if (activePanel) {
                            activePanel.webview.postMessage({ type: 'event', event: resp.event, data: resp });
                        }
                        continue;
                    }
                    const pending = this.pendingRequests.get(resp.id);
                    if (pending) {
                        this.pendingRequests.delete(resp.id);
                        if (resp.error) {
                            pending.reject(new Error(resp.error));
                        }
                        else {
                            pending.resolve(resp.result);
                        }
                    }
                }
                catch {
                    this.outputChannel.appendLine(`[stdout] ${line}`);
                }
            }
        });
        this.process.stderr?.on('data', (data) => {
            this.outputChannel.appendLine(`[stderr] ${data.toString()}`);
        });
        this.process.on('exit', (code) => {
            this.outputChannel.appendLine(`Worker exited with code ${code}`);
            this.process = undefined;
            // Reject all pending requests
            for (const [, pending] of this.pendingRequests) {
                pending.reject(new Error('Worker process exited'));
            }
            this.pendingRequests.clear();
        });
        // Wait for worker to be ready
        try {
            await this.send('ping');
            this.outputChannel.appendLine('Worker is ready.');
        }
        catch (e) {
            this.outputChannel.appendLine(`Worker failed to start: ${e}`);
            throw e;
        }
    }
    send(method, params = {}) {
        return new Promise((resolve, reject) => {
            if (!this.process?.stdin?.writable) {
                return reject(new Error('Worker not running'));
            }
            const id = String(++this.nextId);
            this.pendingRequests.set(id, { resolve, reject });
            const msg = JSON.stringify({ id, method, params }) + '\n';
            this.process.stdin.write(msg);
            // Timeout after 30s
            setTimeout(() => {
                if (this.pendingRequests.has(id)) {
                    this.pendingRequests.delete(id);
                    reject(new Error(`Request ${method} timed out`));
                }
            }, 30000);
        });
    }
    stop() {
        if (this.process) {
            this.process.kill();
            this.process = undefined;
        }
    }
    runProcess(cmd, args, cwd) {
        return new Promise((resolve) => {
            const proc = (0, child_process_1.spawn)(cmd, args, { cwd, shell: true });
            proc.stdout?.on('data', (d) => this.outputChannel.appendLine(d.toString()));
            proc.stderr?.on('data', (d) => this.outputChannel.appendLine(d.toString()));
            proc.on('close', (code) => resolve(code ?? 1));
        });
    }
}
// ── Webview HTML Generator ──────────────────────────────────────────────────
function getWebviewContent(webview, extensionPath, nonce) {
    const buildPath = path.join(extensionPath, 'webview-ui', 'build');
    const assetsPath = path.join(buildPath, 'assets');
    // Find the built JS and CSS files
    let jsFile = 'index.js';
    let cssFile = '';
    if (fs.existsSync(assetsPath)) {
        const files = fs.readdirSync(assetsPath);
        jsFile = files.find(f => f.endsWith('.js')) || jsFile;
        cssFile = files.find(f => f.endsWith('.css')) || '';
    }
    const scriptUri = webview.asWebviewUri(vscode.Uri.file(path.join(assetsPath, jsFile)));
    const cssTag = cssFile
        ? `<link rel="stylesheet" href="${webview.asWebviewUri(vscode.Uri.file(path.join(assetsPath, cssFile)))}">`
        : '';
    return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Security-Policy"
          content="default-src 'none';
                   style-src ${webview.cspSource} 'unsafe-inline';
                   script-src 'nonce-${nonce}';
                   img-src ${webview.cspSource} data:;
                   font-src ${webview.cspSource};
                   connect-src 'none';">
    ${cssTag}
    <title>Ziklo</title>
</head>
<body>
    <div id="root"></div>
    <script nonce="${nonce}" type="module" src="${scriptUri}"></script>
</body>
</html>`;
}
// ── Sidebar View Provider ───────────────────────────────────────────────────
class ZikloSidebarProvider {
    extensionPath;
    worker;
    view;
    constructor(extensionPath, worker) {
        this.extensionPath = extensionPath;
        this.worker = worker;
    }
    resolveWebviewView(webviewView, _context, _token) {
        this.view = webviewView;
        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [vscode.Uri.file(path.join(this.extensionPath, 'webview-ui', 'build'))]
        };
        const nonce = crypto.randomBytes(16).toString('hex');
        // Sidebar shows a mini launcher UI
        webviewView.webview.html = this.getSidebarHtml(webviewView.webview, nonce);
        webviewView.webview.onDidReceiveMessage(async (msg) => {
            if (msg.command === 'openDashboard') {
                vscode.commands.executeCommand('ziklo.openDashboard');
            }
        });
    }
    getSidebarHtml(webview, nonce) {
        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="Content-Security-Policy"
          content="default-src 'none';
                   style-src ${webview.cspSource} 'unsafe-inline';
                   script-src 'nonce-${nonce}';
                   img-src ${webview.cspSource} data:;">
    <style>
        body {
            padding: 12px;
            font-family: var(--vscode-font-family);
            color: var(--vscode-foreground);
            background: var(--vscode-sideBar-background);
        }
        .logo {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 16px;
        }
        .logo-icon {
            width: 28px;
            height: 28px;
            background: var(--vscode-button-background);
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .description {
            font-size: 12px;
            color: var(--vscode-descriptionForeground);
            margin-bottom: 16px;
            line-height: 1.5;
        }
        .open-btn {
            width: 100%;
            padding: 8px 14px;
            background: var(--vscode-button-background);
            color: var(--vscode-button-foreground);
            border: none;
            border-radius: 4px;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            margin-bottom: 12px;
        }
        .open-btn:hover {
            background: var(--vscode-button-hoverBackground);
        }
        hr {
            border: none;
            border-top: 1px solid var(--vscode-panel-border);
            margin: 16px 0;
        }
        .section-title {
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--vscode-descriptionForeground);
            margin-bottom: 8px;
        }
        .feature-list {
            list-style: none;
            padding: 0;
            margin: 0;
        }
        .feature-list li {
            font-size: 12px;
            padding: 4px 0;
            display: flex;
            align-items: center;
            gap: 6px;
            color: var(--vscode-foreground);
        }
        .feature-list li::before {
            content: '●';
            font-size: 6px;
            color: var(--vscode-button-background);
        }
    </style>
</head>
<body>
    <div class="logo">
        <div class="logo-icon">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <circle cx="7" cy="7" r="3.5" stroke="white" stroke-width="1.5" fill="none"/>
                <circle cx="7" cy="2" r="1.2" fill="white"/>
                <circle cx="7" cy="12" r="1.2" fill="white"/>
                <circle cx="2" cy="7" r="1.2" fill="white"/>
                <circle cx="12" cy="7" r="1.2" fill="white"/>
                <line x1="7" y1="3.2" x2="7" y2="5.5" stroke="white" stroke-width="1" stroke-linecap="round"/>
                <line x1="7" y1="8.5" x2="7" y2="10.8" stroke="white" stroke-width="1" stroke-linecap="round"/>
                <line x1="3.2" y1="7" x2="5.5" y2="7" stroke="white" stroke-width="1" stroke-linecap="round"/>
                <line x1="8.5" y1="7" x2="10.8" y2="7" stroke="white" stroke-width="1" stroke-linecap="round"/>
            </svg>
        </div>
        Ziklo
    </div>
    <p class="description">
        Build and run AI-powered desktop automation workflows directly inside VS Code.
    </p>
    <button class="open-btn" id="openBtn">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">
            <rect x="1" y="2" width="12" height="9" rx="1.5"/>
            <path d="M4 13h6M7 11v2"/>
        </svg>
        Open Workflow Editor
    </button>
    <hr>
    <div class="section-title">Capabilities</div>
    <ul class="feature-list">
        <li>Browse websites & desktop apps</li>
        <li>Extract structured data</li>
        <li>Input into forms & submit actions</li>
        <li>Run Python inline</li>
        <li>Build multi-step workflows</li>
    </ul>
    <script nonce="${nonce}">
        const vscode = acquireVsCodeApi();
        document.getElementById('openBtn').addEventListener('click', () => {
            vscode.postMessage({ command: 'openDashboard' });
        });
    </script>
</body>
</html>`;
    }
}
// ── Extension Activate / Deactivate ─────────────────────────────────────────
let worker;
let activePanel;
function activate(context) {
    worker = new PythonWorker(context);
    // Register sidebar
    const sidebarProvider = new ZikloSidebarProvider(context.extensionPath, worker);
    context.subscriptions.push(vscode.window.registerWebviewViewProvider('ziklo.sidebarView', sidebarProvider));
    // Register dashboard command
    context.subscriptions.push(vscode.commands.registerCommand('ziklo.openDashboard', async () => {
        try {
            await worker.ensureStarted();
        }
        catch (e) {
            vscode.window.showErrorMessage(`Ziklo: ${e.message}`);
            return;
        }
        const panel = vscode.window.createWebviewPanel('zikloDashboard', 'Ziklo Workflow Editor', vscode.ViewColumn.One, {
            enableScripts: true,
            retainContextWhenHidden: true,
            localResourceRoots: [vscode.Uri.file(path.join(context.extensionPath, 'webview-ui', 'build'))]
        });
        activePanel = panel;
        panel.onDidDispose(() => {
            if (activePanel === panel) {
                activePanel = undefined;
            }
        }, null, context.subscriptions);
        const nonce = crypto.randomBytes(16).toString('hex');
        panel.webview.html = getWebviewContent(panel.webview, context.extensionPath, nonce);
        panel.iconPath = vscode.Uri.file(path.join(context.extensionPath, 'media', 'logo.svg'));
        // Bridge: Webview → Python Worker
        panel.webview.onDidReceiveMessage(async (message) => {
            if (message.type !== 'rpc') {
                return;
            }
            try {
                const result = await worker.send(message.method, message.params);
                panel.webview.postMessage({ type: 'rpc-response', id: message.id, result });
            }
            catch (err) {
                panel.webview.postMessage({ type: 'rpc-response', id: message.id, error: err.message });
            }
        }, undefined, context.subscriptions);
    }));
}
function deactivate() {
    worker?.stop();
}
//# sourceMappingURL=extension.js.map