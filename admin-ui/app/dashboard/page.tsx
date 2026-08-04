"use client";

import { useEffect, useMemo, useState } from "react";
import {
  AgentWithTenant,
  ApiError,
  DashboardStats,
  LatencyStatWithTenant,
  listAllAgents,
  listAllDashboardStats,
  listAllLatencyStats,
  listAllTodaysActivity,
  listAllUsageTrend,
  listTenants,
  TodaysActivityPoint,
  Tenant,
  UsageTrendPoint,
} from "@/lib/api";

const RANGE_OPTIONS = [
  { label: "7 Days", hours: 24 * 7, days: 7 },
  { label: "30 Days", hours: 24 * 30, days: 30 },
  { label: "90 Days", hours: 24 * 90, days: 90 },
];

const STAT_ICONS: Record<string, React.ReactNode> = {
  minutes: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="8" cy="8" r="6.5" />
      <path d="M8 4.5V8l2.5 1.5" />
    </svg>
  ),
  agents: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="8" cy="5" r="3" />
      <path d="M2 14c0-3.314 2.686-5 6-5s6 1.686 6 5" />
    </svg>
  ),
  live: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M3 2h3l1.5 4-2 1.5a10 10 0 004.5 4.5L11.5 10l4 1.5v3a2 2 0 01-2 2C7.5 16.5 -0.5 8.5 1 3a2 2 0 012-1z" />
    </svg>
  ),
  success: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M1.5 12.5l4.5-5 3 3 5.5-6.5" />
      <path d="M10.5 4h4v4" />
    </svg>
  ),
};

function StatCard({
  icon, label, value, accent, live,
}: {
  icon: keyof typeof STAT_ICONS; label: string; value: string; accent: string; live?: boolean;
}) {
  return (
    <div className="card" style={{ padding: "14px 16px", flex: "1 1 180px", minWidth: 160 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
        <span style={{ width: 15, height: 15, color: accent, flexShrink: 0 }}>{STAT_ICONS[icon]}</span>
        <span style={{ fontSize: ".68rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: ".07em", color: "var(--text-3)" }}>
          {label}
        </span>
        {live && (
          <span style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 4, fontSize: ".64rem", color: "var(--green)" }}>
            <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--green)", display: "inline-block" }} />
            LIVE
          </span>
        )}
      </div>
      <div style={{ fontSize: "1.6rem", fontWeight: 700, color: accent, fontVariantNumeric: "tabular-nums" }}>{value}</div>
    </div>
  );
}

// Rough, named bands rather than a bare number — Retell/Vapi's own
// published benchmarks cluster around 500-600ms as "good" for a managed
// voice AI platform; this project has never gotten close to that on a
// full turn (STT + LLM + tool calls + TTS all sequential today), so the
// bands are calibrated to what's actually achievable on this stack, not
// an arbitrary universal target.
function latencyBand(ms: number | null): { label: string; badge: string } {
  if (ms == null) return { label: "—", badge: "gray" };
  if (ms < 1500) return { label: "Good", badge: "green" };
  if (ms < 3500) return { label: "Slow", badge: "amber" };
  return { label: "Very slow", badge: "red" };
}

function formatMs(ms: number | null): string {
  if (ms == null) return "—";
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

// A small dependency-free multi-series line chart — this is an internal
// admin tool with no charting library installed; two call sites (Usage
// Trends, Today's Activity) share this rather than each hand-rolling SVG.
// Each series is normalized to ITS OWN max, not a shared scale — Calls and
// Minutes (or Inbound/Outbound/Web) are different units, and letting one
// flatten to invisible near the x-axis because another series is 100x
// larger would be misleading, not honest.
function LineChart({
  series, xLabels, height = 160,
}: {
  series: { name: string; color: string; values: number[] }[];
  xLabels: string[];
  height?: number;
}) {
  const width = 100; // percentage-based viewBox, scales via SVG width=100%
  const n = xLabels.length;
  if (n === 0) return <div className="empty-state">No data in this window yet.</div>;

  const points = (values: number[]) => {
    const max = Math.max(1, ...values);
    return values.map((v, i) => {
      const x = n === 1 ? width / 2 : (i / (n - 1)) * width;
      const y = height - (v / max) * (height - 20) - 10;
      return { x, y };
    });
  };

  const xTickIdx = n <= 6 ? xLabels.map((_, i) => i) : [0, Math.floor((n - 1) / 2), n - 1];

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} preserveAspectRatio="none" style={{ overflow: "visible" }}>
        {[0.25, 0.5, 0.75].map((f) => (
          <line key={f} x1={0} x2={width} y1={height * f} y2={height * f} stroke="var(--border)" strokeWidth={0.3} />
        ))}
        {series.map((s) => {
          const pts = points(s.values);
          const path = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p.x},${p.y}`).join(" ");
          return (
            <g key={s.name}>
              <path d={path} fill="none" stroke={s.color} strokeWidth={0.6} vectorEffect="non-scaling-stroke" />
              {pts.map((p, i) => (
                <circle key={i} cx={p.x} cy={p.y} r={0.8} fill={s.color} />
              ))}
            </g>
          );
        })}
      </svg>
      <div style={{ display: "flex", justifyContent: "space-between", marginTop: 4 }}>
        {xTickIdx.map((i) => (
          <span key={i} style={{ fontSize: ".65rem", color: "var(--text-3)" }}>
            {xLabels[i]}
          </span>
        ))}
      </div>
      <div style={{ display: "flex", gap: 16, marginTop: 10 }}>
        {series.map((s) => (
          <div key={s.name} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: ".72rem", color: "var(--text-2)" }}>
            <span style={{ width: 8, height: 8, borderRadius: "50%", background: s.color, display: "inline-block" }} />
            {s.name}
          </div>
        ))}
      </div>
    </div>
  );
}

export default function DashboardPage() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [agents, setAgents] = useState<AgentWithTenant[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [range, setRange] = useState(RANGE_OPTIONS[1]); // 30 days default
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [statsLoading, setStatsLoading] = useState(true);

  const [trend, setTrend] = useState<UsageTrendPoint[]>([]);
  const [trendLoading, setTrendLoading] = useState(true);

  const [activity, setActivity] = useState<TodaysActivityPoint[]>([]);
  const [activityLoading, setActivityLoading] = useState(true);

  const [latencyStats, setLatencyStats] = useState<LatencyStatWithTenant[]>([]);
  const [latencyLoading, setLatencyLoading] = useState(true);
  const [latencyHours, setLatencyHours] = useState(24);

  useEffect(() => {
    listTenants().then(setTenants).catch((e) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  useEffect(() => {
    if (tenants.length === 0) return;
    listAllAgents(tenants).then(setAgents).catch(() => {});
  }, [tenants]);

  useEffect(() => {
    if (tenants.length === 0) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setStatsLoading(true);
    listAllDashboardStats(tenants, range.hours)
      .then(setStats)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setStatsLoading(false));
  }, [tenants, range]);

  useEffect(() => {
    if (tenants.length === 0) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setTrendLoading(true);
    listAllUsageTrend(tenants, range.days)
      .then(setTrend)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setTrendLoading(false));
  }, [tenants, range]);

  useEffect(() => {
    if (tenants.length === 0) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setActivityLoading(true);
    listAllTodaysActivity(tenants)
      .then(setActivity)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setActivityLoading(false));
  }, [tenants]);

  useEffect(() => {
    if (tenants.length === 0) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLatencyLoading(true);
    listAllLatencyStats(tenants, latencyHours)
      .then(setLatencyStats)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLatencyLoading(false));
  }, [tenants, latencyHours]);

  const activeAgents = agents.filter((a) => a.status === "active").length;
  const successPct = stats && stats.success_count + stats.failed_count > 0
    ? Math.round((stats.success_count / (stats.success_count + stats.failed_count)) * 100)
    : null;

  const trendSeries = useMemo(
    () => [
      { name: "Calls", color: "var(--cyan)", values: trend.map((p) => p.calls) },
      { name: "Minutes", color: "var(--indigo)", values: trend.map((p) => p.minutes) },
    ],
    [trend],
  );
  const trendLabels = trend.map((p) => new Date(p.date).toLocaleDateString(undefined, { month: "short", day: "numeric" }));

  const activitySeries = useMemo(
    () => [
      { name: "Inbound", color: "var(--cyan)", values: activity.map((p) => p.inbound) },
      { name: "Outbound", color: "var(--green)", values: activity.map((p) => p.outbound) },
      { name: "Web", color: "var(--indigo)", values: activity.map((p) => p.web) },
    ],
    [activity],
  );
  const activityLabels = activity.map((p) => `${String(p.hour).padStart(2, "0")}:00`);

  return (
    <>
      {error && <div className="error-banner">{error}</div>}

      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
        <div style={{ display: "flex", gap: 4 }}>
          {RANGE_OPTIONS.map((o) => (
            <button
              key={o.label}
              className={`btn btn-sm ${range.label === o.label ? "btn-primary" : "btn-ghost"}`}
              onClick={() => setRange(o)}
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, marginBottom: 16 }}>
        <StatCard icon="minutes" label="Total Minutes" value={statsLoading ? "—" : String(stats?.total_minutes ?? 0)} accent="var(--text)" />
        <StatCard icon="agents" label="Active Agents" value={agents.length === 0 && tenants.length === 0 ? "—" : String(activeAgents)} accent="var(--cyan)" />
        <StatCard icon="live" label="Live Calls" value={statsLoading ? "—" : String(stats?.live_calls ?? 0)} accent="var(--amber)" live={(stats?.live_calls ?? 0) > 0} />
        <StatCard icon="success" label="Success Rate" value={successPct === null ? "—" : `${successPct}%`} accent="var(--green)" />
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-hdr">
          <div className="card-title">Usage Trends</div>
          <div className="card-sub">Last {range.days} days</div>
        </div>
        <div style={{ padding: 16 }}>
          {trendLoading ? <div className="empty-state">Loading…</div> : <LineChart series={trendSeries} xLabels={trendLabels} />}
        </div>
      </div>

      <div style={{ display: "flex", gap: 16, marginBottom: 16, flexWrap: "wrap" }}>
        <div className="card" style={{ flex: "2 1 420px" }}>
          <div className="card-hdr">
            <div className="card-title">Today&apos;s Activity</div>
            <div className="card-sub">Call channels per hour</div>
          </div>
          <div style={{ padding: 16 }}>
            {activityLoading ? (
              <div className="empty-state">Loading…</div>
            ) : activity.length === 0 ? (
              <div className="empty-state">No calls yet today.</div>
            ) : (
              <LineChart series={activitySeries} xLabels={activityLabels} />
            )}
          </div>
        </div>

        <div className="card" style={{ flex: "1 1 260px" }}>
          <div className="card-hdr">
            <div className="card-title">Call Outcomes</div>
            <div className="card-sub">This window</div>
          </div>
          <div style={{ padding: 16 }}>
            {statsLoading ? (
              <div className="empty-state">Loading…</div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span style={{ color: "var(--text-2)" }}>Total</span>
                  <strong style={{ color: "var(--text)" }}>{stats?.total_calls ?? 0}</strong>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span><span className="badge green">success</span></span>
                  <strong style={{ color: "var(--text)" }}>{stats?.success_count ?? 0}</strong>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span><span className="badge red">failed</span></span>
                  <strong style={{ color: "var(--text)" }}>{stats?.failed_count ?? 0}</strong>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span><span className="badge amber">outbound</span></span>
                  <strong style={{ color: "var(--text)" }}>{stats?.outbound_count ?? 0}</strong>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-hdr">
          <div className="card-title">Voice Latency</div>
          <div className="card-sub">
            {latencyLoading ? "Loading…" : `${latencyStats.length} agent/engine combination${latencyStats.length === 1 ? "" : "s"}`}
          </div>
          <select
            className="form-select"
            style={{ marginLeft: 12, width: 150, padding: "3px 8px", fontSize: ".72rem" }}
            value={latencyHours}
            onChange={(e) => setLatencyHours(Number(e.target.value))}
          >
            <option value={24}>Last 24 hours</option>
            <option value={24 * 7}>Last 7 days</option>
            <option value={24 * 30}>Last 30 days</option>
          </select>
        </div>
        {latencyLoading ? (
          <div className="empty-state">Loading…</div>
        ) : latencyStats.length === 0 ? (
          <div className="empty-state">No turns with recorded latency in this window yet.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Account</th>
                <th>Agent</th>
                <th>LLM Engine</th>
                <th>Turns</th>
                <th>p50 STT</th>
                <th>p50 LLM</th>
                <th>p50 TTS</th>
                <th>p50 Voice-to-Voice</th>
                <th>p95 Voice-to-Voice</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {latencyStats.map((s, i) => {
                const band = latencyBand(s.p50_voice_to_voice_ms);
                return (
                  <tr key={`${s.agent_id}-${s.llm_engine}-${i}`}>
                    <td>{s.tenantName}</td>
                    <td>{s.agent_name || "—"}</td>
                    <td className="mono">{s.llm_engine || "—"}</td>
                    <td>{s.sample_count}</td>
                    <td className="mono">{formatMs(s.p50_stt_ms)}</td>
                    <td className="mono">{formatMs(s.p50_llm_ms)}</td>
                    <td className="mono">{formatMs(s.p50_tts_ms)}</td>
                    <td className="mono" style={{ fontWeight: 600 }}>{formatMs(s.p50_voice_to_voice_ms)}</td>
                    <td className="mono">{formatMs(s.p95_voice_to_voice_ms)}</td>
                    <td>
                      <span className={`badge ${band.badge}`}>{band.label}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
