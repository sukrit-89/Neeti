/**
 * useMediaCapture — captures webcam frames + browser speech recognition
 * during an interview and sends data to the backend.
 *
 * - Video: captures frames every 3s, sends to /api/vision/analyze-frame
 * - Audio: uses browser SpeechRecognition API for real-time transcription,
 *          sends transcribed text to /api/speech/segment (no Whisper needed)
 */
import { useEffect, useRef, useState, useCallback } from 'react';
import axios from 'axios';
import { supabase } from './supabase';
import { codingApi } from './api';

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const VISION_CAPTURE_INTERVAL = 3000; // send a frame every 3 seconds

interface MediaCaptureOptions {
    enableAudio?: boolean;
    enableVideo?: boolean;
}

interface MediaCaptureState {
    isCapturing: boolean;
    hasPermission: boolean;
    audioSegments: number;
    visionFrames: number;
    error: string | null;
}

// Browser SpeechRecognition types
type SpeechRecognitionType = typeof window extends { SpeechRecognition: infer T } ? T : unknown;

async function getAuthHeaders(): Promise<Record<string, string>> {
    const { data: { session } } = await supabase.auth.getSession();
    if (session?.access_token) {
        return { Authorization: `Bearer ${session.access_token}` };
    }
    return {};
}

export function useMediaCapture(
    sessionId: number | null,
    options: MediaCaptureOptions = {}
) {
    const { enableAudio = true, enableVideo = true } = options;

    const [state, setState] = useState<MediaCaptureState>({
        isCapturing: false,
        hasPermission: false,
        audioSegments: 0,
        visionFrames: 0,
        error: null,
    });

    const videoRef = useRef<HTMLVideoElement | null>(null);
    const canvasRef = useRef<HTMLCanvasElement | null>(null);
    const streamRef = useRef<MediaStream | null>(null);
    const recognitionRef = useRef<InstanceType<SpeechRecognitionType & (new () => unknown)> | null>(null);
    const visionIntervalRef = useRef<number | null>(null);
    const startTimeRef = useRef<number>(0);
    const activeRef = useRef(false);
    const segmentCountRef = useRef(0);
    const frameCountRef = useRef(0);

    // ── Send a single webcam frame to /api/vision/analyze-frame ──
    const captureAndSendFrame = useCallback(async () => {
        if (!activeRef.current || !sessionId) return;

        const video = videoRef.current;
        const canvas = canvasRef.current;
        if (!video || !canvas || video.readyState < 2) return;

        try {
            canvas.width = 320;
            canvas.height = 240;
            const ctx = canvas.getContext('2d');
            if (!ctx) return;

            ctx.drawImage(video, 0, 0, 320, 240);

            const blob = await new Promise<Blob | null>((resolve) =>
                canvas.toBlob(resolve, 'image/jpeg', 0.6)
            );
            if (!blob) return;

            const arrayBuffer = await blob.arrayBuffer();
            const base64 = btoa(
                String.fromCharCode(...new Uint8Array(arrayBuffer))
            );

            const headers = await getAuthHeaders();
            const elapsed = (Date.now() - startTimeRef.current) / 1000;

            await axios.post(
                `${API_BASE_URL}/api/vision/analyze-frame`,
                {
                    session_id: sessionId,
                    frame_data: base64,
                    timestamp_offset: elapsed,
                },
                { headers, timeout: 5000 }
            );

            frameCountRef.current += 1;
            setState((prev) => ({ ...prev, visionFrames: frameCountRef.current }));
        } catch {
            // Non-fatal — just skip this frame
        }
    }, [sessionId]);

    // ── Send a transcribed speech segment to /api/speech/segment ──
    const sendSpeechSegment = useCallback(
        async (transcript: string, duration: number, confidence: number) => {
            if (!sessionId || !transcript.trim()) return;

            try {
                const elapsed = (Date.now() - startTimeRef.current) / 1000;
                const headers = await getAuthHeaders();

                const formData = new FormData();
                formData.append('session_id', String(sessionId));
                formData.append('transcript', transcript.trim());
                formData.append('start_time', String(Math.max(0, elapsed - duration)));
                formData.append('duration', String(duration));
                formData.append('confidence', String(confidence));

                await axios.post(`${API_BASE_URL}/api/speech/segment`, formData, {
                    headers: { ...headers, 'Content-Type': 'multipart/form-data' },
                    timeout: 10000,
                });

                segmentCountRef.current += 1;
                setState((prev) => ({ ...prev, audioSegments: segmentCountRef.current }));
            } catch {
                // Non-fatal
                console.warn('[MediaCapture] Speech segment send failed');
            }
        },
        [sessionId]
    );

    // ── Start browser SpeechRecognition ──
    const startSpeechRecognition = useCallback(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
        if (!SpeechRecognition) {
            console.warn('[MediaCapture] SpeechRecognition not supported in this browser');
            return;
        }

        const recognition = new SpeechRecognition();
        recognition.continuous = true;
        recognition.interimResults = false;
        recognition.lang = 'en-US';
        recognition.maxAlternatives = 1;

        let segmentStartTime = Date.now();

        recognition.onresult = (event: { resultIndex: number; results: { length: number; [key: number]: { isFinal: boolean; [key: number]: { transcript: string; confidence: number } } } }) => {
            for (let i = event.resultIndex; i < event.results.length; i++) {
                if (event.results[i].isFinal) {
                    const result = event.results[i][0];
                    const duration = (Date.now() - segmentStartTime) / 1000;
                    segmentStartTime = Date.now();

                    if (result.transcript.trim()) {
                        sendSpeechSegment(
                            result.transcript,
                            Math.max(1, duration),
                            result.confidence || 0.85
                        );
                    }
                }
            }
        };

        recognition.onerror = (event: { error: string }) => {
            if (event.error === 'no-speech') return; // Normal — just silence
            console.warn('[MediaCapture] SpeechRecognition error:', event.error);
        };

        recognition.onend = () => {
            // Auto-restart if still active (browser may stop after silence)
            if (activeRef.current) {
                try {
                    recognition.start();
                } catch {
                    // Already started
                }
            }
        };

        recognition.start();
        recognitionRef.current = recognition;
        console.log('[MediaCapture] SpeechRecognition started');
    }, [sendSpeechSegment]);

    const start = useCallback(async () => {
        if (!sessionId || activeRef.current) return;

        try {
            let stream: MediaStream | undefined;
            let videoEnabled = enableVideo;
            let audioEnabled = enableAudio;
            const constraints: MediaStreamConstraints = {};
            if (videoEnabled) constraints.video = { width: 320, height: 240, frameRate: 5 };
            if (audioEnabled) constraints.audio = { echoCancellation: true, noiseSuppression: true };

            try {
                // Safely attempt to initialize navigator streams if present
                if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                    throw new Error("MediaDevices API not supported in this browser");
                }
                stream = await navigator.mediaDevices.getUserMedia(constraints);
            } catch (err: any) {
                console.warn("[MediaCapture] Primary constraint acquisition failed, degrading...", err.message);
                
                // Fallback to audio only if video failed (e.g., no webcam)
                if (videoEnabled && audioEnabled) {
                    try {
                        console.warn("[MediaCapture] Attempting audio-only fallback...");
                        stream = await navigator.mediaDevices.getUserMedia({ audio: constraints.audio, video: false });
                        videoEnabled = false; // Disable video feature dynamically since it failed
                    } catch (audioErr) {
                         // Fallback to video only if audio failed (e.g. no mic)
                        try {
                            console.warn("[MediaCapture] Attempting video-only fallback...");
                            stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: constraints.video });
                            audioEnabled = false; // Disable audio feature dynamically
                        } catch (videoErr) {
                            throw new Error("No media devices found. Missing both camera and microphone.");
                        }
                    }
                } else {
                    throw err; // If we were only requesting one device type and it failed, throw immediately
                }
            }

            if (!stream) throw new Error("Could not initialize media stream.");
            streamRef.current = stream;

            // Set up video element for frame capture
            if (videoEnabled) {
                const video = document.createElement('video');
                video.srcObject = stream;
                video.muted = true;
                video.playsInline = true;
                await video.play();
                videoRef.current = video;

                if (!canvasRef.current) {
                    canvasRef.current = document.createElement('canvas');
                }
            }

            activeRef.current = true;
            startTimeRef.current = Date.now();
            segmentCountRef.current = 0;
            frameCountRef.current = 0;

            setState({
                isCapturing: true,
                hasPermission: true,
                audioSegments: 0,
                visionFrames: 0,
                error: null,
            });

            // Start periodic vision capture
            if (videoEnabled) {
                visionIntervalRef.current = window.setInterval(
                    captureAndSendFrame,
                    VISION_CAPTURE_INTERVAL
                );
            }

            // Start browser speech recognition (no Whisper needed)
            if (audioEnabled) {
                startSpeechRecognition();
            }

            console.log('[MediaCapture] Started —', {
                video: videoEnabled,
                audio: audioEnabled,
                sessionId,
            });
        } catch (err: unknown) {
            const message =
                err instanceof Error ? err.message : 'Camera/microphone access denied';
            console.error('[MediaCapture] Permission error:', message);
            setState((prev) => ({
                ...prev,
                hasPermission: false,
                error: message,
            }));
        }
    }, [sessionId, enableAudio, enableVideo, captureAndSendFrame, startSpeechRecognition]);

    // ── Stop capturing ──
    const stop = useCallback(() => {
        activeRef.current = false;

        // Clear vision interval
        if (visionIntervalRef.current) {
            clearInterval(visionIntervalRef.current);
            visionIntervalRef.current = null;
        }

        // Stop speech recognition
        if (recognitionRef.current) {
            try {
                recognitionRef.current.stop();
            } catch { /* already stopped */ }
            recognitionRef.current = null;
        }

        // Stop media stream tracks
        if (streamRef.current) {
            streamRef.current.getTracks().forEach((t) => t.stop());
            streamRef.current = null;
        }

        // Clean up video element
        if (videoRef.current) {
            videoRef.current.pause();
            videoRef.current.srcObject = null;
            videoRef.current = null;
        }

        setState((prev) => ({ ...prev, isCapturing: false }));
        console.log('[MediaCapture] Stopped —', {
            audioSegments: segmentCountRef.current,
            visionFrames: frameCountRef.current,
        });
    }, []);

    // Auto-start when sessionId changes, auto-stop on unmount
    useEffect(() => {
        if (sessionId) {
            start();
        }
        return () => {
            stop();
        };
    }, [sessionId, start, stop]);

    // ── Peripheral and Multiple Display Detection ──
    const lastDeviceStr = useRef<string | null>(null);
    const lastExtended = useRef<boolean | null>(null);

    useEffect(() => {
        if (!sessionId) return;

        // 1. Detect USB/Bluetooth media device plug/unplug (Headphones, extra cameras, etc.)
        const handleDeviceChange = async () => {
            try {
                const devices = await navigator.mediaDevices.enumerateDevices();
                // Create a deterministic signature of current devices
                const deviceNames = devices.map(d => `${d.kind}:${d.label || d.deviceId}`).sort();
                const deviceStr = deviceNames.join('|');
                
                if (lastDeviceStr.current !== null && lastDeviceStr.current !== deviceStr) {
                    await codingApi.createEvent({
                        session_id: sessionId,
                        event_type: 'peripheral_change',
                        metadata: { 
                            detected_via: 'devicechange',
                            device_count: devices.length,
                            devices: deviceNames,
                        }
                    });
                }
                lastDeviceStr.current = deviceStr;
            } catch (err) {
                console.warn('[MediaCapture] Failed to enumerate devices on change', err);
            }
        };

        // 2. Detect multiple monitors via Screen API
        const checkExtendedDisplay = async () => {
            // @ts-expect-error - isExtended is a relatively new web API
            const isExtended = window.screen.isExtended;
            
            if (isExtended === true && lastExtended.current !== true) {
                await codingApi.createEvent({
                    session_id: sessionId,
                    event_type: 'peripheral_change',
                    metadata: { 
                        detected_via: 'screen_api',
                        message: 'Multiple monitors detected (Extended display)'
                    }
                }).catch(() => {});
            }
            lastExtended.current = !!isExtended;
        };

        navigator.mediaDevices.addEventListener('devicechange', handleDeviceChange);
        
        // Initial state population (does not trigger event as lastRefs are null)
        handleDeviceChange();
        checkExtendedDisplay();

        // Optional periodically check monitor since no event for window.screen
        const displayInterval = setInterval(checkExtendedDisplay, 5000);

        return () => {
            navigator.mediaDevices.removeEventListener('devicechange', handleDeviceChange);
            clearInterval(displayInterval);
        };
    }, [sessionId]);

    return {
        ...state,
        start,
        stop,
    };
}
