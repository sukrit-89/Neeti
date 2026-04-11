import React, { useCallback, useRef, useEffect } from 'react';
import Editor from '@monaco-editor/react';
import { codingApi } from '@/lib/api';
import { Button } from './Button';
import { Play, CheckCircle, XCircle, Terminal, Eye } from 'lucide-react';
import { useAuthStore } from '@/store/useAuthStore';
import { useWebSocketContext } from '@/lib/websocket';
import type { WebSocketMessage } from '@/lib/websocket';

interface CodeEditorProps {
  sessionId: number;
  language: string;
  value: string;
  onChange: (value: string) => void;
  readOnly?: boolean;
}

export const CodeEditor: React.FC<CodeEditorProps> = React.memo(({
  sessionId,
  language,
  value,
  onChange,
  readOnly = false,
}) => {
  const [isExecuting, setIsExecuting] = React.useState(false);
  const [output, setOutput] = React.useState('');
  const [error, setError] = React.useState('');
  const typingTimeout = useRef<number | undefined>(undefined);

  const { user } = useAuthStore();
  const { sendMessage, onMessage } = useWebSocketContext();
  const isRecruiter = user?.role === 'recruiter';

  // Recruiter listens for execution output via WebSocket
  useEffect(() => {
    if (!isRecruiter) return;

    const unsubscribe = onMessage((message: WebSocketMessage) => {
      if (message.type === 'code_executed' || message.type === 'code.executed') {
        if (message.data?.output !== undefined && message.data?.output !== null) {
          setOutput(String(message.data.output));
        }
        if (message.data?.error !== undefined && message.data?.error !== null) {
          setError(String(message.data.error));
        } else {
          setError('');
        }
      }
    });

    return unsubscribe;
  }, [onMessage, isRecruiter]);

  // Track paste vs keystroke using Monaco's onDidPaste
  const lastPasteTimestamp = useRef<number>(0);
  const editorRef = useRef<unknown>(null);

  const handleEditorMount = useCallback((editor: unknown) => {
    editorRef.current = editor;

    // Monaco fires onDidPaste when user pastes content
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (editor as any).onDidPaste?.((e: { range: unknown }) => {
      lastPasteTimestamp.current = Date.now();
    });
  }, []);

  // Track tab visibility changes — switching away during interview is suspicious
  useEffect(() => {
    if (readOnly || isRecruiter) return;

    const handleVisibilityChange = () => {
      if (document.hidden) {
        codingApi.createEvent({
          session_id: sessionId,
          event_type: 'tab_away',
          code_snapshot: value,
          language,
          metadata: { timestamp: Date.now() },
        }).catch(() => {});
      } else {
        codingApi.createEvent({
          session_id: sessionId,
          event_type: 'tab_return',
          code_snapshot: value,
          language,
          metadata: { timestamp: Date.now() },
        }).catch(() => {});
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
  }, [sessionId, language, readOnly, isRecruiter, value]);

  const handleChange = useCallback(
    (newValue: string | undefined) => {
      if (newValue === undefined || readOnly) return;
      onChange(newValue);

      // Debounce: send code update via WebSocket for real-time sync to recruiter
      if (typingTimeout.current) clearTimeout(typingTimeout.current);
      typingTimeout.current = window.setTimeout(() => {
        // Send via WebSocket for real-time recruiter view
        sendMessage({
          type: 'code.changed',
          data: { code: newValue, language },
        });

        // Detect if this was a paste (within 1s of Monaco's onDidPaste event)
        const isPaste = (Date.now() - lastPasteTimestamp.current) < 1000;
        const eventType = isPaste ? 'paste' : 'keystroke';

        // Persist via REST API for AI anomaly detection
        codingApi
          .createEvent({
            session_id: sessionId,
            event_type: eventType,
            code_snapshot: newValue,
            language,
            metadata: isPaste ? { detected_via: 'monaco_onDidPaste' } : undefined,
          })
          .catch((err: unknown) =>
            console.error('Failed to track coding event:', err)
          );
      }, 500);
    },
    [sessionId, onChange, language, sendMessage, readOnly]
  );

  const handleExecute = async () => {
    setIsExecuting(true);
    setOutput('');
    setError('');

    try {
      const result = await codingApi.executeCode(sessionId, value, language);
      if (result.error) setError(result.error);
      else setOutput(result.output || 'Code executed successfully');

      // Broadcast execution result via WebSocket so recruiter sees output
      sendMessage({
        type: 'code.executed',
        data: { code: value, output: result.output, error: result.error, language },
      });

      await codingApi.createEvent({
        session_id: sessionId,
        event_type: 'execute',
        code_snapshot: value,
        language,
        execution_output: result.output,
        execution_error: result.error,
      });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Execution failed');
    } finally {
      setIsExecuting(false);
    }
  };

  return (
    <div className="h-full flex flex-col rounded-lg overflow-hidden border border-neeti-border">
      {/* Read-only indicator for recruiter */}
      {readOnly && (
        <div className="flex items-center gap-2 px-4 py-1.5 bg-primary/10 border-b border-primary/20">
          <Eye className="w-3.5 h-3.5 text-primary" />
          <span className="text-xs font-mono text-primary">Live View — Candidate's Code</span>
          <span className="ml-auto w-2 h-2 rounded-full bg-status-success animate-pulse" />
        </div>
      )}

      <div className="flex-1 relative min-h-0">
        <Editor
          height="100%"
          language={getMonacoLanguage(language)}
          value={value}
          onChange={readOnly ? undefined : handleChange}
          onMount={handleEditorMount}
          theme="vs-dark"
          options={{
            minimap: { enabled: false },
            fontSize: 14,
            fontFamily: "'IBM Plex Mono', Consolas, monospace",
            lineNumbers: 'on',
            roundedSelection: false,
            scrollBeyondLastLine: false,
            automaticLayout: true,
            padding: { top: 16, bottom: 16 },
            wordWrap: 'on',
            tabSize: 2,
            readOnly: readOnly,
            domReadOnly: readOnly,
            quickSuggestions: !readOnly,
            suggestOnTriggerCharacters: !readOnly,
            // Recruiter view styling hints
            renderLineHighlight: readOnly ? 'none' : 'line',
            cursorStyle: readOnly ? 'line-thin' : 'line',
            cursorBlinking: readOnly ? 'solid' : 'blink',
          }}
        />
      </div>

      <div className="flex-shrink-0 border-t border-neeti-border bg-neeti-surface/60">
        <div className="px-4 py-2 border-b border-neeti-border flex items-center justify-between">
          <span className="inline-flex items-center gap-2 text-sm font-medium text-ink-secondary">
            <Terminal className="w-3.5 h-3.5" />
            Output
          </span>
          {!readOnly && (
            <Button
              size="sm"
              variant="primary"
              onClick={handleExecute}
              disabled={isExecuting || !value}
            >
              <Play className="w-3 h-3" />
              {isExecuting ? 'Running…' : 'Run Code'}
            </Button>
          )}
        </div>

        <div className="p-4 h-32 overflow-y-auto font-mono text-sm">
          {output && (
            <div className="flex items-start gap-2 text-status-success">
              <CheckCircle className="w-4 h-4 mt-0.5 shrink-0" />
              <pre className="whitespace-pre-wrap">{output}</pre>
            </div>
          )}
          {error && (
            <div className="flex items-start gap-2 text-status-critical">
              <XCircle className="w-4 h-4 mt-0.5 shrink-0" />
              <pre className="whitespace-pre-wrap">
                {typeof error === 'string' ? error : String(error)}
              </pre>
            </div>
          )}
          {!output && !error && (
            <p className="text-ink-ghost">
              {readOnly ? 'Waiting for candidate to run code…' : 'Click "Run Code" to execute your code'}
            </p>
          )}
        </div>
      </div>
    </div>
  );
});

function getMonacoLanguage(lang: string): string {
  const map: Record<string, string> = {
    typescript: 'typescript',
    javascript: 'javascript',
    python: 'python',
    java: 'java',
    cpp: 'cpp',
    go: 'go',
    rust: 'rust',
    csharp: 'csharp',
    ruby: 'ruby',
    php: 'php',
  };
  return map[lang] || 'typescript';
}
