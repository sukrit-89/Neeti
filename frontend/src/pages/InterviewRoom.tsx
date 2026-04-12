import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { LiveKitRoom, VideoConference, RoomAudioRenderer } from '@livekit/components-react';
import '@livekit/components-styles';
import { useSessionStore } from '../store/useSessionStore';
import { useAuthStore } from '../store/useAuthStore';
import { WebSocketProvider, useWebSocketContext } from '../lib/websocket';
import type { WebSocketMessage } from '../lib/websocket';
import { useMediaCapture } from '../lib/useMediaCapture';
import { useEnvironmentProbe } from '../lib/useEnvironmentProbe';
import { CodeEditor } from '../components/CodeEditor';
import { Button } from '../components/Button';
import {
  LogOut, Code, Maximize2, Minimize2, FileText, Clock,
  AlertTriangle, Wifi, WifiOff, Camera, CameraOff, Mic, MicOff,
  ShieldAlert, MonitorX, Usb,
} from 'lucide-react';

const LIVEKIT_WS_URL = import.meta.env.VITE_LIVEKIT_WS_URL;

const InterviewTimer: React.FC = () => {
  const [elapsedTime, setElapsedTime] = useState(0);

  useEffect(() => {
    const t = setInterval(() => setElapsedTime(p => p + 1), 1000);
    return () => clearInterval(t);
  }, []);

  const fmt = (s: number) => `${Math.floor(s / 60).toString().padStart(2, '0')}:${(s % 60).toString().padStart(2, '0')}`;

  return <span className="font-mono tabular-nums text-sm font-semibold text-ink-primary">{fmt(elapsedTime)}</span>;
};

// ── RT-04 FIX: Only emit WS code.changed when the user actually typed ─────────
// WorkspaceEditor holds canonical `currentCode`. When a recruiter receives a
// WS message we update state (isProgrammaticUpdateRef = true). CodeEditor
// detects this ref and skips sendMessage, breaking the echo loop:
//   recruiter WS → setCurrentCode → new `value` prop → Monaco onChange
//                            ↑ (would echo back without the guard)
const WorkspaceEditor: React.FC<{ sessionId: number; language: string }> = React.memo(({ sessionId, language }) => {
  const [currentCode, setCurrentCode] = useState('');
  const { user } = useAuthStore();
  const { onMessage } = useWebSocketContext();
  const isRecruiter = user?.role === 'recruiter';
  const isProgrammaticUpdateRef = useRef(false);

  // Recruiter: listen for candidate's code changes via WebSocket
  useEffect(() => {
    if (!isRecruiter) return;

    const unsubscribe = onMessage((message: WebSocketMessage) => {
      if (
        message.type === 'code.changed' ||
        message.type === 'code_changed' ||
        message.type === 'code.executed' ||
        message.type === 'code_executed'
      ) {
        const code = message.data?.code || message.data?.code_snapshot;
        if (code && typeof code === 'string') {
          isProgrammaticUpdateRef.current = true;
          setCurrentCode(code);
        }
      }
    });

    return unsubscribe;
  }, [onMessage, isRecruiter]);

  const handleChange = useCallback((val: string) => {
    setCurrentCode(val);
  }, []);

  if (isRecruiter && !currentCode) {
    return (
      <>
        <div className="flex-1 p-3 flex items-center justify-center">
          <div className="text-center space-y-4 animate-fade-in">
            <div className="w-16 h-16 mx-auto rounded-2xl bg-primary/10 border border-primary/20 flex items-center justify-center">
              <Code className="w-7 h-7 text-primary/50" />
            </div>
            <div className="space-y-2">
              <h3 className="text-sm font-display font-semibold text-ink-secondary">
                Waiting for Candidate
              </h3>
              <p className="text-xs text-ink-ghost max-w-xs mx-auto">
                The candidate's code will appear here in real-time once they start typing.
              </p>
            </div>
            <div className="flex items-center justify-center gap-2 pt-2">
              <span className="w-1.5 h-1.5 rounded-full bg-primary animate-pulse" />
              <span className="text-[10px] font-mono text-primary/60 tracking-wider uppercase">Listening</span>
            </div>
          </div>
        </div>

        <div className="border-t border-neeti-border bg-neeti-surface/60 px-4 py-2">
          <div className="flex items-center gap-4 text-[10px] text-ink-ghost">
            <span>Status: <span className="font-mono text-ink-secondary">Waiting</span></span>
            <span>Lang: <span className="font-mono text-ink-secondary">{language.toUpperCase()}</span></span>
            <span className="ml-auto">
              <span className="w-1.5 h-1.5 rounded-full bg-primary inline-block mr-1 align-middle animate-pulse-dot" />
              Monitoring Candidate
            </span>
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      {/* RT-09 FIX: min-h-0 lets Monaco flex-child honour the container height */}
      <div className="flex-1 p-3 min-h-0">
        <CodeEditor
          value={currentCode}
          onChange={handleChange}
          language={language}
          sessionId={sessionId}
          readOnly={isRecruiter}
          isProgrammaticUpdate={isProgrammaticUpdateRef}
        />
      </div>

      <div className="border-t border-neeti-border bg-neeti-surface/60 px-4 py-2">
        <div className="flex items-center gap-4 text-[10px] text-ink-ghost">
          <span>Lines: <span className="font-mono text-ink-secondary">{currentCode.split('\n').length}</span></span>
          <span>Lang: <span className="font-mono text-ink-secondary">{language.toUpperCase()}</span></span>
          <span className="ml-auto">
            {isRecruiter ? (
              <>
                <span className="w-1.5 h-1.5 rounded-full bg-primary inline-block mr-1 align-middle animate-pulse-dot" />
                Monitoring Candidate
              </>
            ) : (
              <>
                <span className="w-1.5 h-1.5 rounded-full bg-status-success inline-block mr-1 align-middle animate-pulse-dot" />
                AI Monitoring Active
              </>
            )}
          </span>
        </div>
      </div>
    </>
  );
});

// ── RT-02 FIX: Proper React components instead of IIFE inside JSX ─────────────
// The original code called `useAuthStore.getState()` inside an IIFE `(() => {})()` in JSX.
// This is not technically a React hook (getState is synchronous zustand API), but the
// IIFE pattern makes conditional component rendering impossible to type-check or
// test, and DOES break React DevTools. Extract to proper named components.
interface RecruiterLeaveDialogProps {
  sessionId: number;
  onClose: () => void;
}
const RecruiterLeaveDialog: React.FC<RecruiterLeaveDialogProps> = ({ sessionId, onClose }) => {
  const navigate = useNavigate();
  return (
    <div className="dialog-overlay">
      <div className="dialog-panel max-w-md w-full mx-4 p-7 space-y-5">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-lg bg-status-warning/10">
            <AlertTriangle className="w-5 h-5 text-status-warning" />
          </div>
          <h2 className="text-lg font-display font-semibold text-ink-primary">Session Controls</h2>
        </div>
        <p className="text-sm text-ink-secondary">
          As the recruiter, you can end this session which will trigger the AI evaluation pipeline and generate candidate assessment.
        </p>
        <div className="flex flex-col gap-2 pt-1">
          <Button variant="critical" className="w-full" onClick={async () => {
            try {
              await useSessionStore.getState().endSession(sessionId);
              onClose();
              navigate(`/sessions/${sessionId}/results`);
            } catch (err) { console.error('Failed to end session:', err); }
          }}>
            End Session &amp; Run Evaluation
          </Button>
          <Button variant="secondary" className="w-full" onClick={() => { onClose(); navigate('/dashboard'); }}>
            Leave Without Ending
          </Button>
          <Button variant="ghost" className="w-full" onClick={onClose}>Stay</Button>
        </div>
      </div>
    </div>
  );
};

interface CandidateLeaveDialogProps {
  onClose: () => void;
  onLeave: () => void;
}
const CandidateLeaveDialog: React.FC<CandidateLeaveDialogProps> = ({ onClose, onLeave }) => (
  <div className="dialog-overlay">
    <div className="dialog-panel max-w-md w-full mx-4 p-7 space-y-5">
      <div className="flex items-center gap-3">
        <div className="p-2 rounded-lg bg-status-warning/10">
          <AlertTriangle className="w-5 h-5 text-status-warning" />
        </div>
        <h2 className="text-lg font-display font-semibold text-ink-primary">Leave Interview?</h2>
      </div>
      <p className="text-sm text-ink-secondary">
        Your progress has been saved but the interview session will remain active.
      </p>
      <div className="flex gap-3 pt-1">
        <Button variant="secondary" className="flex-1" onClick={onClose}>Stay</Button>
        <Button variant="critical" className="flex-1" onClick={onLeave}>Leave Interview</Button>
      </div>
    </div>
  </div>
);

export const InterviewRoom: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const { currentSession, fetchSession } = useSessionStore();
  const sessionFetchRequestedRef = useRef<number | null>(null);

  useEffect(() => {
    const sessionId = id ? parseInt(id, 10) : NaN;
    if (!currentSession && id && !Number.isNaN(sessionId) && sessionFetchRequestedRef.current !== sessionId) {
      sessionFetchRequestedRef.current = sessionId;
      fetchSession(sessionId).catch(() => {
        sessionFetchRequestedRef.current = null;
      });
    }
  }, [currentSession, id, fetchSession]);

  if (!currentSession) {
    return (
      <div className="min-h-screen bg-neeti-bg flex items-center justify-center">
        <div className="flex flex-col items-center gap-4 animate-fade-in">
          <div className="w-10 h-10 border-2 border-primary border-t-transparent rounded-full animate-spin" />
          <p className="text-ink-ghost text-sm font-mono">Loading session…</p>
        </div>
      </div>
    );
  }

  return (
    <WebSocketProvider sessionId={currentSession.id}>
      <InterviewRoomContent />
    </WebSocketProvider>
  );
};

const InterviewRoomContent: React.FC = () => {
  const navigate = useNavigate();
  // RT-05 FIX: always fetch a fresh LiveKit token on mount so a stale/expired
  // JWT from a previous navigation doesn't cause a silent room join failure.
  const { currentSession, roomToken, fetchRoomToken, clearRoomToken } = useSessionStore();
  const { user } = useAuthStore();
  const { isConnected } = useWebSocketContext();
  const isRecruiter = user?.role === 'recruiter';

  const [isCodeExpanded, setIsCodeExpanded] = useState(false);
  const [language, setLanguage] = useState('typescript');
  const [showLeaveDialog, setShowLeaveDialog] = useState(false);
  const [remoteEnvWarnings, setRemoteEnvWarnings] = useState<{has_vm_signals?: boolean; has_virtual_camera?: boolean} | null>(null);
  const roomTokenRequestedRef = useRef<number | null>(null);

  const mediaCapture = useMediaCapture(
    !isRecruiter && currentSession ? currentSession.id : null,
    { enableAudio: !isRecruiter, enableVideo: !isRecruiter }
  );

  const envWarnings = useEnvironmentProbe(!isRecruiter && currentSession ? currentSession.id : null);

  // RT-05: clear the stale token whenever the session changes so fetch always runs
  useEffect(() => {
    if (!currentSession) { navigate('/dashboard'); return; }
    clearRoomToken();
    roomTokenRequestedRef.current = null;
  }, [currentSession?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!currentSession) return;
    if (!roomToken && roomTokenRequestedRef.current !== currentSession.id) {
      roomTokenRequestedRef.current = currentSession.id;
      fetchRoomToken(currentSession.id).catch(() => {
        roomTokenRequestedRef.current = null;
        navigate('/dashboard');
      });
    }
  }, [currentSession, roomToken, navigate, fetchRoomToken]);

  const { onMessage } = useWebSocketContext();
  useEffect(() => {
    if (isRecruiter) {
      return onMessage((msg) => {
        if (msg.type === 'environment.anomaly') {
          setRemoteEnvWarnings(prev => ({
            ...prev,
            ...(msg.data as {has_vm_signals?: boolean; has_virtual_camera?: boolean})
          }));
        }
      });
    }
  }, [onMessage, isRecruiter]);

  if (!currentSession || !roomToken) {
    return (
      <div className="min-h-screen bg-neeti-bg flex items-center justify-center">
        <div className="flex flex-col items-center gap-4 animate-fade-in">
          <div className="w-10 h-10 border-2 border-primary border-t-transparent rounded-full animate-spin" />
          <p className="text-ink-ghost text-sm font-mono">Connecting to interview room…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen flex flex-col bg-neeti-bg overflow-hidden relative">
      <div className="ambient-orb ambient-orb-primary w-[400px] h-[400px] top-[-15%] right-[5%] z-0 opacity-40" />
      <div className="ambient-orb ambient-orb-blue w-[300px] h-[300px] bottom-[10%] left-[-5%] z-0 opacity-30" />

      {/* ── Candidate's own integrity warnings ── */}
      {!isRecruiter && envWarnings.vmDetected && (
        <div className="relative z-20 bg-red-900/90 border-b border-red-500/40 px-4 py-2.5 flex items-center gap-3 animate-fade-in shrink-0">
          <ShieldAlert className="w-5 h-5 text-red-400 shrink-0" />
          <p className="text-red-200 text-xs font-medium">
            <span className="font-bold text-red-100">⚠ Virtual Machine Detected</span> — Your environment has been flagged. This session is being monitored for integrity compliance.
          </p>
        </div>
      )}

      {!isRecruiter && (envWarnings.virtualCameraDetected || mediaCapture.activeVirtualCameraWarning) && (
        <div className="relative z-20 bg-red-900/90 border-b border-red-500/40 px-4 py-2.5 flex items-center gap-3 animate-fade-in shrink-0">
          <MonitorX className="w-5 h-5 text-red-400 shrink-0" />
          <p className="text-red-200 text-xs font-medium">
            <span className="font-bold text-red-100">⚠ Virtual Camera Detected</span> — A virtual camera (e.g. OBS) was identified. Please use your physical webcam for this interview.
          </p>
        </div>
      )}

      {/* ── Recruiter's view of candidate's integrity warnings ── */}
      {isRecruiter && remoteEnvWarnings?.has_vm_signals && (
        <div className="relative z-20 bg-red-900/90 border-b border-red-500/40 px-4 py-2.5 flex items-center gap-3 animate-fade-in shrink-0">
          <ShieldAlert className="w-5 h-5 text-red-400 shrink-0" />
          <p className="text-red-200 text-xs font-medium">
            <span className="font-bold text-red-100">⚠ Action Required: Virtual Machine</span> — The candidate's environment runs on a Virtual Machine. Integrity violation recorded.
          </p>
        </div>
      )}

      {isRecruiter && remoteEnvWarnings?.has_virtual_camera && (
        <div className="relative z-20 bg-red-900/90 border-b border-red-500/40 px-4 py-2.5 flex items-center gap-3 animate-fade-in shrink-0">
          <MonitorX className="w-5 h-5 text-red-400 shrink-0" />
          <p className="text-red-200 text-xs font-medium">
            <span className="font-bold text-red-100">⚠ Action Required: Virtual Camera</span> — The candidate is using a virtual camera driver (e.g. OBS). Possible bypass attempt.
          </p>
        </div>
      )}

      {!isRecruiter && mediaCapture.peripheralWarning && (
        <div className="relative z-20 bg-amber-900/80 border-b border-amber-500/30 px-4 py-2 flex items-center gap-3 animate-fade-in shrink-0">
          <Usb className="w-4 h-4 text-amber-400 shrink-0" />
          <p className="text-amber-200 text-xs font-medium">
            <span className="font-bold text-amber-100">Peripheral Change:</span> {mediaCapture.peripheralWarning}
          </p>
        </div>
      )}

      <header className="glass-header px-5 py-3 shrink-0 relative z-10">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-5">
            <div className="flex items-center gap-2.5">
              <FileText className="w-5 h-5 text-primary" />
              <div>
                <h1 className="text-sm font-display font-semibold text-ink-primary leading-tight">
                  {currentSession.title}
                </h1>
                <p className="text-[10px] text-ink-ghost font-mono">
                  Code: {currentSession.join_code}
                </p>
              </div>
            </div>

            <div className="rec-indicator">
              <span className="rec-dot" />
              <span className="text-[10px] font-mono font-semibold text-status-critical tracking-wider">REC</span>
            </div>

            <div className="flex items-center gap-1.5 text-xs text-ink-tertiary">
              <Clock className="w-3.5 h-3.5" />
              <InterviewTimer />
            </div>
          </div>

          <div className="flex items-center gap-3">
            <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-neeti-border bg-neeti-surface">
              {isConnected ? (
                <Wifi className="w-3.5 h-3.5 text-status-success" />
              ) : (
                <WifiOff className="w-3.5 h-3.5 text-status-critical" />
              )}
              <span className="text-[10px] uppercase tracking-wider text-ink-ghost font-mono">
                {isConnected ? 'Connected' : 'Reconnecting'}
              </span>
            </div>

            {!isRecruiter && mediaCapture.isCapturing && (
              <div className="flex items-center gap-2 px-2.5 py-1 rounded-full border border-neeti-border bg-neeti-surface">
                {mediaCapture.cameraMissing ? (
                  <CameraOff className="w-3 h-3 text-status-warning" />
                ) : (
                  <Camera className="w-3 h-3 text-status-success" />
                )}
                <span className="text-[10px] font-mono text-ink-ghost">{mediaCapture.cameraMissing ? 'ERR' : mediaCapture.visionFrames}</span>

                {mediaCapture.micMissing ? (
                  <MicOff className="w-3 h-3 text-status-warning" />
                ) : (
                  <Mic className="w-3 h-3 text-status-success" />
                )}
                <span className="text-[10px] font-mono text-ink-ghost">{mediaCapture.micMissing ? 'ERR' : mediaCapture.audioSegments}</span>
              </div>
            )}
            {!isRecruiter && mediaCapture.error && (
              <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-status-warning/30 bg-status-warning/5">
                <AlertTriangle className="w-3 h-3 text-status-warning" />
                <span className="text-[10px] font-mono text-status-warning">Capture Error</span>
              </div>
            )}

            <Button variant="ghost" size="sm" onClick={() => setIsCodeExpanded(!isCodeExpanded)}
              icon={isCodeExpanded ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}>
              {isCodeExpanded ? 'Minimize' : 'Expand'}
            </Button>

            <Button variant="critical" size="sm" onClick={() => setShowLeaveDialog(true)} icon={<LogOut className="w-4 h-4" />}>
              Leave
            </Button>
          </div>
        </div>
      </header>

      <div className="flex-1 flex overflow-hidden min-h-0">
        <div className={`${isCodeExpanded ? 'w-1/2' : 'w-2/3'} border-r border-neeti-border flex flex-col transition-all duration-300 min-h-0`}>
          <div className="flex-1 p-3 min-h-0">
            <LiveKitRoom video={!isRecruiter} audio={true} token={roomToken} serverUrl={LIVEKIT_WS_URL} connectOptions={{ autoSubscribe: true }}>
              <VideoConference className="h-full" />
              <RoomAudioRenderer />
            </LiveKitRoom>
          </div>

          <div className="border-t border-neeti-border bg-neeti-surface/60 px-4 py-2 shrink-0">
            <div className="flex items-center justify-between text-[10px]">
              <div className="flex items-center gap-3">
                <span className="text-ink-ghost">Status:</span>
                <span className={`px-2 py-0.5 rounded-md border text-[10px] font-semibold uppercase tracking-wider ${
                  currentSession.status === 'live'
                    ? 'bg-status-critical/10 text-status-critical border-status-critical/20'
                    : 'bg-neeti-elevated text-ink-secondary border-neeti-border'
                }`}>
                  {currentSession.status === 'live' && <span className="inline-block w-1.5 h-1.5 rounded-full bg-status-critical animate-pulse mr-1 align-middle" />}
                  {currentSession.status}
                </span>
              </div>
              <div className="flex items-center gap-2 text-ink-ghost">
                <span>Language:</span>
                <span className="font-mono text-ink-secondary">{language.toUpperCase()}</span>
              </div>
            </div>
          </div>
        </div>

        <div className={`${isCodeExpanded ? 'w-1/2' : 'w-1/3'} flex flex-col transition-all duration-300 min-h-0`}>
          <div className="border-b border-neeti-border bg-neeti-surface/60 px-4 py-2.5 shrink-0">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Code className="w-4 h-4 text-primary" />
                <span className="text-xs font-semibold text-ink-primary">Code Editor</span>
              </div>
              <select
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
                className="text-xs bg-neeti-bg border border-neeti-border rounded-md px-2 py-1 text-ink-secondary focus:outline-none focus:border-primary transition-colors"
              >
                <option value="typescript">TypeScript</option>
                <option value="javascript">JavaScript</option>
                <option value="python">Python</option>
                <option value="java">Java</option>
                <option value="cpp">C++</option>
              </select>
            </div>
          </div>

          <WorkspaceEditor sessionId={currentSession?.id || 0} language={language} />
        </div>
      </div>

      {/* RT-02 FIX: proper components — no hook-violating IIFE */}
      {showLeaveDialog && (
        isRecruiter ? (
          <RecruiterLeaveDialog
            sessionId={currentSession.id}
            onClose={() => setShowLeaveDialog(false)}
          />
        ) : (
          <CandidateLeaveDialog
            onClose={() => setShowLeaveDialog(false)}
            onLeave={() => { mediaCapture.stop(); setShowLeaveDialog(false); navigate('/dashboard'); }}
          />
        )
      )}
    </div>
  );
};

// Synced for GitHub timestamp
