import React, { useState, useEffect, useCallback } from "react";
import {
  Shield,
  Database,
  HardDrive,
  Activity,
  RefreshCw,
  Lock,
  Unlock,
  AlertTriangle,
  CheckCircle,
  XCircle,
  RotateCw,
} from "lucide-react";

const CAPTURE_POLL_MS = 5000;

/** Human-readable byte formatter. */
function fmtBytes(n) {
  if (!n || n < 0) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
}

function fmtPercent(n) {
  if (!n || n <= 0) return "0%";
  return `${(n * 100).toFixed(1)}%`;
}

/** Small status badge pill. */
function Badge({ active, label }) {
  const cls = active
    ? "bg-emerald-900/40 border-emerald-700 text-emerald-400"
    : "bg-slate-800 border-slate-700 text-slate-400";
  return (
    <span className={`inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full border text-xs font-medium ${cls}`}>
      {active ? <CheckCircle className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
      {label}
    </span>
  );
}

/** Stat card for capture metrics. */
function CaptureStatCard({ title, value, unit, icon: Icon }) {
  return (
    <div className="bg-slate-900 border border-slate-800 p-6 rounded-xl shadow-lg">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-slate-400 text-sm font-medium">{title}</h3>
        <Icon className="w-5 h-5 text-indigo-500" />
      </div>
      <div className="flex items-end space-x-2">
        <span className="text-2xl font-bold text-white">{value}</span>
        {unit && <span className="text-slate-500 mb-1 text-sm">{unit}</span>}
      </div>
    </div>
  );
}

const CapturePanel = ({ apiKey }) => {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [rotating, setRotating] = useState(false);

  const authHeader = apiKey ? `Bearer ${apiKey}` : "";

  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch("/api/capture/status", {
        headers: { Authorization: authHeader },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setStatus(data);
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [authHeader]);

  useEffect(() => {
    fetchStatus();
    const id = setInterval(fetchStatus, CAPTURE_POLL_MS);
    return () => clearInterval(id);
  }, [fetchStatus]);

  const handleRotate = async () => {
    setRotating(true);
    try {
      await fetch("/api/capture/rotate", {
        method: "POST",
        headers: { Authorization: authHeader },
      });
      await fetchStatus();
    } catch (e) {
      setError(e.message);
    } finally {
      setRotating(false);
    }
  };

  if (loading && !status) {
    return (
      <div className="flex items-center justify-center h-64 text-slate-400">
        Loading capture status...
      </div>
    );
  }

  if (error && !status) {
    return (
      <div className="bg-rose-950/30 border border-rose-800 rounded-xl p-6 mt-6">
        <div className="flex items-center gap-2 text-rose-400 mb-2">
          <AlertTriangle className="w-5 h-5" />
          <span className="font-semibold">Capture API Error</span>
        </div>
        <p className="text-rose-300 text-sm">{error}</p>
        <p className="text-slate-500 text-xs mt-2">
          Ensure the Guardian API key is set in localStorage under "guardian_key"
          and the Guardian service is running.
        </p>
      </div>
    );
  }

  if (!status) return null;

  const { config: cfg, sink, writer, disk } = status;
  const sinkMetrics = sink?.metrics || {};
  const diskUsageRatio = disk?.bytes_budget ? disk.bytes_used / disk.bytes_budget : 0;

  return (
    <div className="space-y-6">
      {/* Header row */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Shield className="w-6 h-6 text-indigo-500" />
          <h2 className="text-2xl font-bold text-white">Capture Subsystem</h2>
          <Badge active={cfg?.enabled} label={cfg?.enabled ? "Enabled" : "Disabled"} />
          <Badge active={cfg?.active} label={cfg?.active ? "Active" : "Inactive"} />
        </div>
        <button
          onClick={handleRotate}
          disabled={rotating || !cfg?.enabled}
          className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <RotateCw className={`w-4 h-4 ${rotating ? "animate-spin" : ""}`} />
          Force Rotate
        </button>
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-6">
        <CaptureStatCard
          title="Total Events"
          value={sinkMetrics.guardian_capture_events_total ?? 0}
          icon={Activity}
        />
        <CaptureStatCard
          title="Dropped Events"
          value={sinkMetrics.guardian_capture_events_dropped_total ?? 0}
          icon={AlertTriangle}
        />
        <CaptureStatCard
          title="Queue Depth"
          value={sinkMetrics.guardian_capture_queue_depth ?? 0}
          icon={Database}
        />
        <CaptureStatCard
          title="Disk Usage"
          value={fmtBytes(disk?.bytes_used ?? 0)}
          unit={`/ ${fmtBytes(disk?.bytes_budget ?? 0)}`}
          icon={HardDrive}
        />
      </div>

      {/* Disk usage bar */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-6">
        <h3 className="text-sm font-medium text-slate-400 mb-3">Disk Budget</h3>
        <div className="w-full bg-slate-800 rounded-full h-4 overflow-hidden">
          <div
            className={`h-full transition-all duration-500 ${
              diskUsageRatio > 0.9
                ? "bg-rose-500"
                : diskUsageRatio > 0.7
                  ? "bg-amber-500"
                  : "bg-emerald-500"
            }`}
            style={{ width: `${Math.min(diskUsageRatio * 100, 100)}%` }}
          />
        </div>
        <div className="flex justify-between mt-2 text-xs text-slate-500">
          <span>{fmtPercent(diskUsageRatio)} used</span>
          <span>Retention: {disk?.retention_days ?? cfg?.retention_days ?? 7} days</span>
        </div>
      </div>

      {/* Writer status */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-6">
        <h3 className="text-lg font-semibold text-white mb-4">Writer</h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <div>
            <span className="text-slate-500">Running:</span>
            <span className="ml-2">
              <Badge active={writer?.running} label={writer?.running ? "Yes" : "No"} />
            </span>
          </div>
          <div>
            <span className="text-slate-500">Bytes Written:</span>
            <span className="ml-2 text-white font-mono">{fmtBytes(writer?.bytes_written ?? 0)}</span>
          </div>
          <div>
            <span className="text-slate-500">Files Rotated:</span>
            <span className="ml-2 text-white font-mono">{writer?.files_rotated ?? 0}</span>
          </div>
          <div>
            <span className="text-slate-500">Write Failures:</span>
            <span className="ml-2 text-white font-mono">{writer?.write_failures ?? 0}</span>
          </div>
        </div>
      </div>

      {/* Configuration */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-6">
        <h3 className="text-lg font-semibold text-white mb-4">Configuration</h3>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-x-8 gap-y-3 text-sm">
          <ConfigRow label="Local Capture" value={cfg?.local_capture ? "Yes" : "No"} />
          <ConfigRow label="Cloud Capture" value={cfg?.cloud_capture ? "Yes" : "No"} />
          <ConfigRow label="Per-Client Opt-In" value={cfg?.per_client_opt_in ? "Yes" : "No"} />
          <ConfigRow label="Policy Version" value={cfg?.policy_version} />
          <ConfigRow label="Instance ID" value={cfg?.instance_id} mono />
          <ConfigRow label="Capture Root" value={disk?.root ?? cfg?.capture_root} mono />
          <ConfigRow label="Max File Size" value={fmtBytes(cfg?.max_file_bytes ?? 0)} />
          <ConfigRow label="Max File Age" value={`${Math.round((cfg?.max_file_age_seconds ?? 0) / 60)} min`} />
          <ConfigRow label="Max Pending Events" value={cfg?.max_pending_events ?? 0} />
        </div>
      </div>

      {/* Field policies */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-6">
        <h3 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
          <Lock className="w-4 h-4 text-slate-500" />
          Field Capture Policies
        </h3>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
          {Object.entries(cfg?.field_policies || {}).map(([field, policy]) => (
            <div
              key={field}
              className="flex items-center justify-between px-4 py-3 rounded-lg bg-slate-800/50 border border-slate-800"
            >
              <span className="text-slate-300 text-sm">{field.replace(/_/g, " ")}</span>
              <PolicyBadge policy={policy} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

function ConfigRow({ label, value, mono }) {
  return (
    <div className="flex items-center justify-between border-b border-slate-800/50 pb-1">
      <span className="text-slate-500">{label}</span>
      <span className={`text-white ${mono ? "font-mono text-xs" : ""}`}>{value ?? "—"}</span>
    </div>
  );
}

function PolicyBadge({ policy }) {
  const variants = {
    capture: { cls: "bg-emerald-900/40 border-emerald-700 text-emerald-400", icon: Unlock },
    strip: { cls: "bg-slate-800 border-slate-700 text-slate-400", icon: Lock },
  };
  const v = variants[policy] || { cls: "bg-amber-900/40 border-amber-700 text-amber-400", icon: AlertTriangle };
  const Icon = v.icon;
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-xs font-mono ${v.cls}`}>
      <Icon className="w-3 h-3" />
      {policy}
    </span>
  );
}

export default CapturePanel;
