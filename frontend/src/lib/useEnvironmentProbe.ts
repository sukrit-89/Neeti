/**
 * useEnvironmentProbe — Runs once on session join to detect
 * virtual machines and virtual cameras via browser-native APIs.
 *
 * Signals detected:
 *   - WebGL renderer string (VM GPU signatures)
 *   - navigator.hardwareConcurrency (low core count)
 *   - navigator.deviceMemory (low memory)
 *   - navigator.platform vs user-agent mismatch
 *   - MediaDevices labels (OBS, ManyCam, Snap Camera, etc.)
 *   - Screen dimensions (non-standard VM resolutions)
 *
 * Emits 'environment_anomaly' events via codingApi — never blocks access.
 */
import { useEffect, useRef, useState } from 'react';
import { codingApi } from './api';

// ── Known VM GPU renderer substrings ──
const VM_GPU_SIGNATURES = [
  'virtualbox',
  'vmware',
  'svga3d',
  'llvmpipe',         // Mesa software renderer (common in VMs)
  'swiftshader',      // Google's software GPU
  'microsoft basic',  // Hyper-V fallback adapter
  'parallels',
  'qemu',
  'virgl',            // Virgil 3D (KVM/QEMU)
  'chromium',         // Headless Chromium sometimes exposes this
];

// ── Known virtual camera device label substrings ──
const VIRTUAL_CAMERA_SIGNATURES = [
  'obs virtual',
  'obs-camera',
  'manycam',
  'xsplit',
  'snap camera',
  'e2esoft',
  'virtual cam',
  'virtualcam',
  'droidcam',
  'iriun',
  'epoccam',
  'newtek ndi',
  'chromacam',
  'mmhmm',
  'prezi video',
  'camo',
  'fake',
];

interface EnvironmentAnomaly {
  type: 'vm_gpu' | 'virtual_camera' | 'low_hardware' | 'platform_mismatch' | 'suspicious_screen';
  evidence: string;
  confidence: 'high' | 'medium' | 'low';
  raw_value?: string;
}

export interface EnvironmentWarnings {
  vmDetected: boolean;
  virtualCameraDetected: boolean;
  warnings: string[];
}

/**
 * Probe WebGL renderer for VM signatures.
 */
function probeWebGLRenderer(): EnvironmentAnomaly | null {
  try {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
    if (!gl) return null;

    const debugInfo = (gl as WebGLRenderingContext).getExtension('WEBGL_debug_renderer_info');
    if (!debugInfo) return null;

    const renderer = (gl as WebGLRenderingContext)
      .getParameter(debugInfo.UNMASKED_RENDERER_WEBGL) as string;
    const vendor = (gl as WebGLRenderingContext)
      .getParameter(debugInfo.UNMASKED_VENDOR_WEBGL) as string;

    const combined = `${vendor} ${renderer}`.toLowerCase();

    for (const sig of VM_GPU_SIGNATURES) {
      if (combined.includes(sig)) {
        return {
          type: 'vm_gpu',
          evidence: `WebGL renderer matches VM signature: "${sig}"`,
          confidence: 'high',
          raw_value: `${vendor} | ${renderer}`,
        };
      }
    }
  } catch {
    // WebGL not available — not an anomaly by itself
  }
  return null;
}

/**
 * Check hardware concurrency and device memory.
 */
function probeHardware(): EnvironmentAnomaly | null {
  const cores = navigator.hardwareConcurrency || 0;
  const memory = (navigator as any).deviceMemory || 0;

  if (cores > 0 && cores <= 2 && memory > 0 && memory <= 4) {
    return {
      type: 'low_hardware',
      evidence: `Very low hardware: ${cores} cores, ${memory}GB RAM — common in VMs`,
      confidence: 'medium',
      raw_value: `cores=${cores}, memory=${memory}GB`,
    };
  }

  if (cores === 1) {
    return {
      type: 'low_hardware',
      evidence: `Single CPU core detected — unusual for modern hardware, common in VMs`,
      confidence: 'medium',
      raw_value: `cores=${cores}`,
    };
  }

  return null;
}

/**
 * Detect platform vs user-agent mismatch.
 */
function probePlatformMismatch(): EnvironmentAnomaly | null {
  const platform = (navigator.platform || '').toLowerCase();
  const ua = navigator.userAgent.toLowerCase();

  const platformIsWindows = platform.includes('win');
  const platformIsLinux = platform.includes('linux');
  const platformIsMac = platform.includes('mac');

  const uaIsWindows = ua.includes('windows');
  const uaIsLinux = ua.includes('linux') && !ua.includes('android');
  const uaIsMac = ua.includes('macintosh') || ua.includes('mac os');

  if (
    (platformIsWindows && !uaIsWindows) ||
    (platformIsLinux && !uaIsLinux && !ua.includes('android')) ||
    (platformIsMac && !uaIsMac)
  ) {
    return {
      type: 'platform_mismatch',
      evidence: `navigator.platform="${navigator.platform}" conflicts with user-agent OS`,
      confidence: 'medium',
      raw_value: `platform="${navigator.platform}", ua_os="${uaIsWindows ? 'Windows' : uaIsLinux ? 'Linux' : uaIsMac ? 'Mac' : 'other'}"`,
    };
  }

  return null;
}

/**
 * Check screen dimensions for VM-typical values.
 */
function probeScreen(): EnvironmentAnomaly | null {
  const { width, height } = window.screen;

  const vmResolutions = [
    [800, 600],
    [1024, 768],
    [1280, 800],
  ];

  for (const [w, h] of vmResolutions) {
    if (width === w && height === h) {
      return {
        type: 'suspicious_screen',
        evidence: `Screen resolution ${width}×${height} is commonly used in virtual machines`,
        confidence: 'low',
        raw_value: `${width}x${height}`,
      };
    }
  }

  return null;
}

/**
 * Scan media devices for virtual camera labels.
 */
async function probeVirtualCameras(): Promise<EnvironmentAnomaly[]> {
  const anomalies: EnvironmentAnomaly[] = [];

  try {
    if (!navigator.mediaDevices?.enumerateDevices) return anomalies;

    const devices = await navigator.mediaDevices.enumerateDevices();
    const videoInputs = devices.filter(d => d.kind === 'videoinput');

    for (const device of videoInputs) {
      const label = (device.label || '').toLowerCase();
      if (!label) continue;

      for (const sig of VIRTUAL_CAMERA_SIGNATURES) {
        if (label.includes(sig)) {
          anomalies.push({
            type: 'virtual_camera',
            evidence: `Camera device label matches virtual camera: "${sig}"`,
            confidence: 'high',
            raw_value: device.label,
          });
          break;
        }
      }
    }

    if (videoInputs.length > 3) {
      anomalies.push({
        type: 'virtual_camera',
        evidence: `Unusually many video inputs detected (${videoInputs.length})`,
        confidence: 'low',
        raw_value: `video_input_count=${videoInputs.length}`,
      });
    }
  } catch {
    // Permission denied or API not available
  }

  return anomalies;
}

/**
 * Main hook. Runs once per session join, emits anomaly events,
 * and returns reactive warning state for the UI.
 */
export function useEnvironmentProbe(sessionId: number | null): EnvironmentWarnings {
  const hasRun = useRef(false);
  const [envWarnings, setEnvWarnings] = useState<EnvironmentWarnings>({
    vmDetected: false,
    virtualCameraDetected: false,
    warnings: [],
  });

  useEffect(() => {
    if (!sessionId || hasRun.current) return;
    hasRun.current = true;

    const runProbe = async () => {
      const anomalies: EnvironmentAnomaly[] = [];

      const gpuAnomaly = probeWebGLRenderer();
      if (gpuAnomaly) anomalies.push(gpuAnomaly);

      const hwAnomaly = probeHardware();
      if (hwAnomaly) anomalies.push(hwAnomaly);

      const platformAnomaly = probePlatformMismatch();
      if (platformAnomaly) anomalies.push(platformAnomaly);

      const screenAnomaly = probeScreen();
      if (screenAnomaly) anomalies.push(screenAnomaly);

      const cameraAnomalies = await probeVirtualCameras();
      anomalies.push(...cameraAnomalies);

      if (anomalies.length === 0) {
        console.log('[EnvironmentProbe] No anomalies detected ✓');
        return;
      }

      console.warn(
        `[EnvironmentProbe] ${anomalies.length} anomaly(ies) detected:`,
        anomalies
      );

      // Set UI-visible warnings
      const hasVM = anomalies.some(a => a.type === 'vm_gpu' || a.type === 'low_hardware');
      const hasVirtualCam = anomalies.some(a => a.type === 'virtual_camera');
      const warningMessages: string[] = [];

      if (hasVM) {
        warningMessages.push('Virtual machine environment detected. This session is being monitored for integrity.');
      }
      if (hasVirtualCam) {
        warningMessages.push('Virtual camera detected (e.g. OBS). Your real camera feed is required for this interview.');
      }
      if (anomalies.some(a => a.type === 'platform_mismatch')) {
        warningMessages.push('OS environment mismatch detected. This has been flagged for review.');
      }

      setEnvWarnings({
        vmDetected: hasVM,
        virtualCameraDetected: hasVirtualCam,
        warnings: warningMessages,
      });

      // Emit to backend
      try {
        await codingApi.createEvent({
          session_id: sessionId,
          event_type: 'environment_anomaly',
          metadata: {
            anomaly_count: anomalies.length,
            anomalies: anomalies.map(a => ({
              type: a.type,
              evidence: a.evidence,
              confidence: a.confidence,
              raw_value: a.raw_value,
            })),
            has_vm_signals: hasVM,
            has_virtual_camera: hasVirtualCam,
            probe_timestamp: new Date().toISOString(),
            user_agent: navigator.userAgent,
            screen: `${window.screen.width}x${window.screen.height}`,
          },
        });
        console.log('[EnvironmentProbe] Anomaly event sent to backend');
      } catch (err) {
        console.warn('[EnvironmentProbe] Failed to send anomaly event:', err);
      }
    };

    const timer = setTimeout(runProbe, 2000);
    return () => clearTimeout(timer);
  }, [sessionId]);

  return envWarnings;
}

