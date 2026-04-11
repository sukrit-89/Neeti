import React, { createContext, useContext, useEffect, useCallback, useRef, useState } from 'react';
/* eslint-disable react-refresh/only-export-components */
import { supabase } from './supabase';

const WS_BASE_URL = import.meta.env.VITE_WS_URL || 'ws://localhost:8000';

export interface WebSocketMessage {
    type: string;
    timestamp: string;
    data: Record<string, unknown>;
}

interface WebSocketContextType {
    isConnected: boolean;
    connectionFailed: boolean;
    sendMessage: (message: Record<string, unknown>) => void;
    onMessage: (listener: (msg: WebSocketMessage) => void) => () => void;
    reconnect: () => void;
}

const WebSocketContext = createContext<WebSocketContextType | null>(null);

export const WebSocketProvider: React.FC<{ sessionId: number | null; children: React.ReactNode }> = ({ sessionId, children }) => {
    const [isConnected, setIsConnected] = useState(false);
    const [connectionFailed, setConnectionFailed] = useState(false);
    const wsRef = useRef<WebSocket | null>(null);
    const reconnectTimeoutRef = useRef<number | null>(null);
    const reconnectAttemptsRef = useRef(0);
    const listenersRef = useRef<Set<(msg: WebSocketMessage) => void>>(new Set());
    const connectingRef = useRef(false);
    const pendingMessagesRef = useRef<string[]>([]);

    const MAX_RECONNECT_ATTEMPTS = 5;
    const BASE_RECONNECT_DELAY = 1000;
    const MAX_RECONNECT_DELAY = 30000;

    const cleanup = useCallback(() => {
        if (reconnectTimeoutRef.current) {
            clearTimeout(reconnectTimeoutRef.current);
            reconnectTimeoutRef.current = null;
        }
        if (wsRef.current) {
            // Remove handlers to prevent reconnect on intentional close
            wsRef.current.onclose = null;
            wsRef.current.onerror = null;
            wsRef.current.onmessage = null;
            wsRef.current.onopen = null;
            if (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING) {
                wsRef.current.close();
            }
            wsRef.current = null;
        }
    }, []);

    const connect = useCallback(async () => {
        if (!sessionId || connectingRef.current) return;
        connectingRef.current = true;

        // Close existing connection FIRST to prevent stacking
        cleanup();

        try {
            const { data: { session } } = await supabase.auth.getSession();
            const authToken = session?.access_token || '';

            const url = authToken
                ? `${WS_BASE_URL}/api/ws/session/${sessionId}?token=${authToken}`
                : `${WS_BASE_URL}/api/ws/session/${sessionId}`;

            const ws = new WebSocket(url);

            ws.onopen = () => {
                console.log('[WS Provider] Connected to session', sessionId);
                setIsConnected(true);
                setConnectionFailed(false);
                reconnectAttemptsRef.current = 0;
                connectingRef.current = false;

                // Flush any messages that were queued while disconnected
                if (pendingMessagesRef.current.length > 0) {
                    console.log(`[WS Provider] Flushing ${pendingMessagesRef.current.length} queued messages`);
                    for (const msg of pendingMessagesRef.current) {
                        ws.send(msg);
                    }
                    pendingMessagesRef.current = [];
                }
            };

            ws.onmessage = (event) => {
                try {
                    const message = JSON.parse(event.data);
                    listenersRef.current.forEach(listener => listener(message));
                } catch (error) {
                    console.error('Failed to parse WebSocket message:', error);
                }
            };

            ws.onclose = (event) => {
                console.log('[WS Provider] Disconnected', event.code);
                setIsConnected(false);
                wsRef.current = null;
                connectingRef.current = false;

                // Auto-reconnect on unexpected closures
                // Include 1008 (auth) for retry — token may have refreshed since last attempt
                if (event.code !== 1000 && reconnectAttemptsRef.current < MAX_RECONNECT_ATTEMPTS) {
                    const delay = Math.min(
                        BASE_RECONNECT_DELAY * Math.pow(2, reconnectAttemptsRef.current),
                        MAX_RECONNECT_DELAY
                    );
                    reconnectAttemptsRef.current += 1;
                    console.log(`[WS Provider] Reconnecting in ${delay}ms (attempt ${reconnectAttemptsRef.current}/${MAX_RECONNECT_ATTEMPTS})`);
                    reconnectTimeoutRef.current = window.setTimeout(() => {
                        connect();
                    }, delay);
                } else if (reconnectAttemptsRef.current >= MAX_RECONNECT_ATTEMPTS) {
                    console.error('[WS Provider] Max reconnect attempts reached');
                    setConnectionFailed(true);
                }
            };

            ws.onerror = () => {
                connectingRef.current = false;
            };

            wsRef.current = ws;
        } catch (err) {
            console.error('[WS Provider] Connect error:', err);
            connectingRef.current = false;
        }
    }, [sessionId, cleanup]);

    const disconnect = useCallback(() => {
        cleanup();
        setIsConnected(false);
        connectingRef.current = false;
    }, [cleanup]);

    const sendMessage = useCallback((message: Record<string, unknown>) => {
        const payload = JSON.stringify(message);
        if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(payload);
        } else {
            // Queue message for delivery when connection is restored
            console.warn('[WS Provider] WebSocket not open, queueing message:', message.type);
            pendingMessagesRef.current.push(payload);
            // Keep queue bounded to prevent memory leaks
            if (pendingMessagesRef.current.length > 50) {
                pendingMessagesRef.current = pendingMessagesRef.current.slice(-50);
            }
        }
    }, []);

    const onMessage = useCallback((listener: (msg: WebSocketMessage) => void) => {
        listenersRef.current.add(listener);
        return () => { listenersRef.current.delete(listener); };
    }, []);

    const reconnect = useCallback(() => {
        reconnectAttemptsRef.current = 0;
        setConnectionFailed(false);
        connect();
    }, [connect]);

    useEffect(() => {
        connect();
        return () => disconnect();
    }, [connect, disconnect]);

    return (
        <WebSocketContext.Provider value={{ isConnected, connectionFailed, sendMessage, onMessage, reconnect }}>
            {children}
        </WebSocketContext.Provider>
    );
};

export const useWebSocketContext = () => {
    const context = useContext(WebSocketContext);
    if (!context) {
        throw new Error('useWebSocketContext must be used within a WebSocketProvider');
    }
    return context;
};

// Standalone hook for components not wrapped in WebSocketProvider
export function useWebSocket(sessionId: number | null) {
    const [isConnected, setIsConnected] = useState(false);
    const [connectionFailed, setConnectionFailed] = useState(false);
    const wsRef = useRef<WebSocket | null>(null);
    const reconnectTimeoutRef = useRef<number | null>(null);
    const reconnectAttemptsRef = useRef(0);
    const listenersRef = useRef<Set<(msg: WebSocketMessage) => void>>(new Set());
    const connectingRef = useRef(false);

    const MAX_RECONNECT_ATTEMPTS = 5;
    const BASE_RECONNECT_DELAY = 1000;
    const MAX_RECONNECT_DELAY = 30000;

    const cleanup = useCallback(() => {
        if (reconnectTimeoutRef.current) {
            clearTimeout(reconnectTimeoutRef.current);
            reconnectTimeoutRef.current = null;
        }
        if (wsRef.current) {
            wsRef.current.onclose = null;
            wsRef.current.onerror = null;
            wsRef.current.onmessage = null;
            wsRef.current.onopen = null;
            if (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING) {
                wsRef.current.close();
            }
            wsRef.current = null;
        }
    }, []);

    const connect = useCallback(async () => {
        if (!sessionId || connectingRef.current) return;
        connectingRef.current = true;
        cleanup();

        try {
            const { data: { session } } = await supabase.auth.getSession();
            const authToken = session?.access_token || '';
            const url = authToken
                ? `${WS_BASE_URL}/api/ws/session/${sessionId}?token=${authToken}`
                : `${WS_BASE_URL}/api/ws/session/${sessionId}`;
            const ws = new WebSocket(url);

            ws.onopen = () => { setIsConnected(true); setConnectionFailed(false); reconnectAttemptsRef.current = 0; connectingRef.current = false; };
            ws.onmessage = (event) => {
                try {
                    const message = JSON.parse(event.data);
                    listenersRef.current.forEach(l => l(message));
                } catch (e) { console.error(e); }
            };
            ws.onclose = (event) => {
                setIsConnected(false); wsRef.current = null; connectingRef.current = false;
                if (event.code !== 1000 && event.code !== 1008 && reconnectAttemptsRef.current < MAX_RECONNECT_ATTEMPTS) {
                    const delay = Math.min(BASE_RECONNECT_DELAY * Math.pow(2, reconnectAttemptsRef.current), MAX_RECONNECT_DELAY);
                    reconnectAttemptsRef.current += 1;
                    reconnectTimeoutRef.current = window.setTimeout(() => connect(), delay);
                } else if (reconnectAttemptsRef.current >= MAX_RECONNECT_ATTEMPTS) { setConnectionFailed(true); }
            };
            ws.onerror = () => { connectingRef.current = false; };
            wsRef.current = ws;
        } catch { connectingRef.current = false; }
    }, [sessionId, cleanup]);

    const disconnect = useCallback(() => {
        cleanup();
        setIsConnected(false);
        connectingRef.current = false;
    }, [cleanup]);

    const sendMessage = useCallback((message: Record<string, unknown>) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify(message));
    }, []);

    const onMessage = useCallback((listener: (msg: WebSocketMessage) => void) => {
        listenersRef.current.add(listener);
        return () => { listenersRef.current.delete(listener); };
    }, []);

    useEffect(() => {
        connect();
        return () => disconnect();
    }, [connect, disconnect]);

    return {
        isConnected, sendMessage, onMessage, connectionFailed,
        reconnect: () => { reconnectAttemptsRef.current = 0; setConnectionFailed(false); connect(); }
    };
}

export function useLiveMonitoring(sessionId: number | null) {
    const [isConnected, setIsConnected] = useState(false);
    const [metrics, setMetrics] = useState<Record<string, unknown> | null>(null);
    const [flags, setFlags] = useState<Array<{ message: string; severity: string; timestamp: string }>>([]);
    const wsRef = useRef<WebSocket | null>(null);
    const pingIntervalRef = useRef<number | null>(null);
    const reconnectTimeoutRef = useRef<number | null>(null);
    const reconnectAttemptsRef = useRef(0);
    const connectingRef = useRef(false);

    const MAX_RECONNECT_ATTEMPTS = 5;

    const cleanup = useCallback(() => {
        if (pingIntervalRef.current) { clearInterval(pingIntervalRef.current); pingIntervalRef.current = null; }
        if (reconnectTimeoutRef.current) { clearTimeout(reconnectTimeoutRef.current); reconnectTimeoutRef.current = null; }
        if (wsRef.current) {
            wsRef.current.onclose = null;
            wsRef.current.onerror = null;
            wsRef.current.onmessage = null;
            wsRef.current.onopen = null;
            if (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING) {
                wsRef.current.close();
            }
            wsRef.current = null;
        }
    }, []);

    const connect = useCallback(async () => {
        if (!sessionId || connectingRef.current) return;
        connectingRef.current = true;
        cleanup();

        try {
            const { data: { session } } = await supabase.auth.getSession();
            const authToken = session?.access_token || '';
            const url = authToken
                ? `${WS_BASE_URL}/api/ws/session/${sessionId}?token=${authToken}`
                : `${WS_BASE_URL}/api/ws/session/${sessionId}`;
            const ws = new WebSocket(url);

            ws.onopen = () => {
                setIsConnected(true);
                reconnectAttemptsRef.current = 0;
                connectingRef.current = false;
                // Request metrics periodically (every 10s)
                pingIntervalRef.current = window.setInterval(() => {
                    if (ws.readyState === WebSocket.OPEN) {
                        ws.send(JSON.stringify({ type: 'request_metrics' }));
                    }
                }, 10000);
                // Initial metrics request
                ws.send(JSON.stringify({ type: 'request_metrics' }));
            };

            ws.onmessage = (event) => {
                try {
                    const message = JSON.parse(event.data);
                    if (message.type === 'metrics_update') {
                        setMetrics(message.data);
                    } else if (message.type === 'code.changed' || message.type === 'code.executed') {
                        setMetrics(prev => ({ ...prev, type: 'coding', ...message.data }));
                    } else if (message.type === 'flag' || message.type === 'anomaly') {
                        setFlags(prev => [
                            { message: message.data?.message || 'Flag detected', severity: message.data?.severity || 'warning', timestamp: message.timestamp || new Date().toISOString() },
                            ...prev,
                        ].slice(0, 50));
                    }
                } catch (e) { console.error('Monitor WS parse error:', e); }
            };

            ws.onclose = (event) => {
                setIsConnected(false);
                wsRef.current = null;
                connectingRef.current = false;
                if (pingIntervalRef.current) { clearInterval(pingIntervalRef.current); pingIntervalRef.current = null; }
                if (event.code !== 1000 && reconnectAttemptsRef.current < MAX_RECONNECT_ATTEMPTS) {
                    const delay = Math.min(1000 * Math.pow(2, reconnectAttemptsRef.current), 30000);
                    reconnectAttemptsRef.current += 1;
                    reconnectTimeoutRef.current = window.setTimeout(() => connect(), delay);
                }
            };

            ws.onerror = () => { connectingRef.current = false; };
            wsRef.current = ws;
        } catch { connectingRef.current = false; }
    }, [sessionId, cleanup]);

    const disconnect = useCallback(() => {
        cleanup();
        setIsConnected(false);
        connectingRef.current = false;
    }, [cleanup]);

    const requestMetrics = useCallback(() => {
        if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify({ type: 'request_metrics' }));
    }, []);

    useEffect(() => { connect(); return () => disconnect(); }, [connect, disconnect]);
    return { isConnected, metrics, requestMetrics, flags };
}
