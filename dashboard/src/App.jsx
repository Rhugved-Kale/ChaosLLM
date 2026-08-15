import { useEffect, useState } from "react";
import Sparkline from "./Sparkline.jsx";
import ComparisonShowcase from "./ComparisonShowcase.jsx";
import HowItWorks from "./HowItWorks.jsx";
import TriggerButton from "./TriggerButton.jsx";
import { describeRun } from "./describeRun.js";

// The one config knob this whole app has: which proxy's control API to
// watch. Set VITE_PROXY_URL at build time (Vercel env var for the hosted
// dashboard); defaults to a local proxy for `npm run dev`.
const PROXY_URL = import.meta.env.VITE_PROXY_URL || "http://localhost:8000";

const POLL_INTERVAL_MS = 3000;

function BigStat({ label, value, tone }) {
  return (
    <div className="big-stat">
      <div className={`big-stat-value${tone ? ` tone-${tone}` : ""}`}>{value}</div>
      <div className="big-stat-label">{label}</div>
    </div>
  );
}

export default function App() {
  const [runId, setRunId] = useState(null);
  const [status, setStatus] = useState("waiting"); // waiting | connecting | live | complete
  const [phase, setPhase] = useState(null);
  const [totalCount, setTotalCount] = useState(0);
  const [successCount, setSuccessCount] = useState(0);
  const [degradedRate, setDegradedRate] = useState(null);
  const [completedAt, setCompletedAt] = useState(null);
  const [latencyHistory, setLatencyHistory] = useState([]);
  const [faultFireCounts, setFaultFireCounts] = useState({});
  const [assertions, setAssertions] = useState([]);
  const [warnings, setWarnings] = useState([]);

  // Poll continuously (not just while waiting) for the latest run_id, and
  // switch only when it's actually different from the one we're showing.
  // A completed run's `latest` stays the same run_id until a new one
  // starts, so gating on "no runId yet" would immediately rediscover the
  // just-completed run, reset its state to blank, and reopen an SSE stream
  // that will never receive anything (that run already finished publishing)
  // — leaving the UI stuck showing "live" with no data forever.
  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const response = await fetch(`${PROXY_URL}/control/runs/latest`);
        const data = await response.json();
        if (!cancelled && data.run_id && data.run_id !== runId) {
          setPhase(null);
          setTotalCount(0);
          setSuccessCount(0);
          setDegradedRate(null);
          setCompletedAt(null);
          setLatencyHistory([]);
          setFaultFireCounts({});
          setAssertions([]);
          setWarnings([]);
          setRunId(data.run_id);
        }
      } catch {
        // Proxy unreachable; keep polling silently.
      }
    };

    poll();
    const interval = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [runId]);

  // Subscribed to a run: stream progress over SSE.
  useEffect(() => {
    if (!runId) return undefined;
    setStatus("connecting");

    const source = new EventSource(`${PROXY_URL}/control/runs/${runId}/events`);

    source.onopen = () => setStatus("live");

    source.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.phase) setPhase(data.phase);
      if (typeof data.total_count === "number") setTotalCount(data.total_count);
      if (typeof data.success_count === "number") setSuccessCount(data.success_count);
      if (data.degraded_rate != null) setDegradedRate(data.degraded_rate);
      if (data.latency_p95_ms != null) {
        setLatencyHistory((prev) => [...prev.slice(-29), data.latency_p95_ms]);
      }
      if (data.fault_fire_counts && Object.keys(data.fault_fire_counts).length > 0) {
        setFaultFireCounts(data.fault_fire_counts);
      }
      if (data.assertions && data.assertions.length > 0) setAssertions(data.assertions);
      if (data.warnings && data.warnings.length > 0) setWarnings(data.warnings);

      if (data.type === "run_complete") {
        setStatus("complete");
        setCompletedAt(data.completed_at || new Date().toISOString());
        source.close();
      }
    };

    source.onerror = () => {
      setStatus((current) => (current === "complete" ? current : "connecting"));
    };

    return () => source.close();
  }, [runId]);

  const successRate = totalCount > 0 ? (successCount / totalCount) * 100 : null;
  const isRunLive = status === "live" || status === "connecting";
  const story =
    status === "complete"
      ? describeRun({ successRate, degradedRate, faultFireCounts, completedAt })
      : null;

  return (
    <div className="page">
      <header className="hero">
        <h1>ChaosLLM</h1>
        <p className="pitch">
          Chaos engineering for <em>LLM apps</em>
        </p>
      </header>

      <HowItWorks />

      <section className="live-panel">
        <div className="live-panel-header">
          <span className={`status-pill status-${status}`}>{status}</span>
          {runId && <span className="run-id">{runId}</span>}
        </div>

        <TriggerButton proxyUrl={PROXY_URL} isRunLive={isRunLive} />

        {!runId && (
          <p className="empty-state">
            No experiment has run yet. Click the button above to start one, or check back,
            a new run also fires automatically every hour.
          </p>
        )}

        {runId && (
          <>
            {story && <p className="run-story">{story}</p>}

            <div className="phase-row">
              <span className="phase-indicator">{phase ?? "—"}</span>
              <span className="phase-count">{totalCount.toLocaleString()} requests</span>
            </div>

            <div className="big-stat-row">
              <BigStat
                label="success rate"
                value={successRate != null ? `${successRate.toFixed(0)}%` : "—"}
                tone="pass"
              />
              <BigStat
                label="degraded rate"
                value={degradedRate != null ? `${(degradedRate * 100).toFixed(0)}%` : "—"}
                tone="warn"
              />
            </div>

            <Sparkline values={latencyHistory} label="p95 latency (ms)" />

            <div className="fault-ticker">
              <div className="section-label">Faults fired</div>
              {Object.keys(faultFireCounts).length === 0 ? (
                <p className="fault-ticker-empty">none yet</p>
              ) : (
                <ul className="fault-ticker-list">
                  {Object.entries(faultFireCounts).map(([id, count]) => (
                    <li key={id}>
                      <code>{id}</code>
                      <span className="count">{count}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {assertions.length > 0 && (
              <div className="assertion-chips">
                {assertions.map((assertion) => (
                  <span
                    key={assertion.type}
                    className={`chip ${assertion.passed ? "chip-pass" : "chip-fail"}`}
                    title={assertion.detail}
                  >
                    {assertion.passed ? "PASS" : "FAIL"} · {assertion.type}
                  </span>
                ))}
              </div>
            )}

            {warnings.length > 0 && (
              <div className="warnings">
                <ul>
                  {warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </section>

      <ComparisonShowcase />

      <footer className="footer">
        <a href="https://github.com/Rhugved-Kale/ChaosLLM" target="_blank" rel="noreferrer">
          GitHub
        </a>
        <span>Rhugved Kale</span>
      </footer>
    </div>
  );
}
