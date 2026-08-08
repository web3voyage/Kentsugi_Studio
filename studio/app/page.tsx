"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";

type SceneObject = {
  id: string;
  asset: string;
  color?: number[];
  appearance?: { metallic?: number; roughness?: number; emission_strength?: number } | null;
};
type Job = {
  id: string;
  prompt: string;
  status: "preview_ready" | "queued" | "rendering" | "complete" | "failed";
  error: string | null;
  preview_url: string;
  video_url: string | null;
  plan: {
    title: string;
    duration: number;
    fps: number;
    objects: SceneObject[];
    camera: { type: string; focal_mm: number };
    environment: { preset: string; time_of_day: string; weather: string };
  };
};

const API_BASE = (process.env.NEXT_PUBLIC_KENTSUGI_API_URL ?? "http://127.0.0.1:8765").replace(/\/$/, "");

const EXAMPLES = [
  "Create a luxury perfume advertisement at sunset with a slow camera orbit",
  "A boy walks toward a red car in a rainy city at night",
  "Create a clean product reveal with soft daylight and a cinematic orbit",
];

function statusLabel(status: Job["status"]) {
  return {
    preview_ready: "Preview ready",
    queued: "Queued",
    rendering: "Rendering in Blender",
    complete: "Video complete",
    failed: "Render failed",
  }[status];
}

export default function Home() {
  const [prompt, setPrompt] = useState(EXAMPLES[0]);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState<"preview" | "render" | null>(null);
  const [message, setMessage] = useState("Local creative engine connected");
  const [planner, setPlanner] = useState<"demo" | "agentrouter" | "agentrouter-responses" | "offline">("offline");
  const [quality, setQuality] = useState<"preview" | "final">("preview");
  const [engine, setEngine] = useState<"auto" | "cycles" | "eevee">("auto");

  const previewUrl = useMemo(
    () => (job ? `${API_BASE}${job.preview_url}?v=${job.id}` : null),
    [job],
  );

  useEffect(() => {
    fetch(`${API_BASE}/api/health`)
      .then((response) => response.json())
      .then((health) => {
        const active = health.planner === "agentrouter-responses"
          ? "agentrouter-responses"
          : health.planner === "agentrouter"
            ? "agentrouter"
            : "demo";
        setPlanner(active);
        setMessage(active === "agentrouter-responses"
          ? "AgentRouter GPT creative engine connected"
          : active === "agentrouter"
            ? "AgentRouter Claude creative engine connected"
            : "Demo creative engine connected");
      })
      .catch(() => {
        setPlanner("offline");
        setMessage("Creative engine is offline");
      });
  }, []);

  useEffect(() => {
    if (!job || !["queued", "rendering"].includes(job.status)) return;
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch(`${API_BASE}/api/jobs/${job.id}`);
        if (response.ok) setJob(await response.json());
      } catch {
        setMessage("Waiting for the render worker to reconnect");
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [job]);

  async function submit(render: boolean) {
    if (!prompt.trim() || busy) return;
    setBusy(render ? "render" : "preview");
    setMessage(render ? "Sending the shot to Blender" : "Planning your scene");
    try {
      const response = await fetch(`${API_BASE}/api/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, render, quality, engine }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "The scene could not be created");
      setJob(payload);
      setMessage(render ? "Render worker started" : "Scene validated and ready");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "The creative engine is unavailable");
    } finally {
      setBusy(null);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void submit(false);
  }

  return (
    <main className="studio-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="Kentsugi Studio home">
          <span className="brand-mark">K</span>
          <span>Kentsugi <em>Studio</em></span>
        </a>
        <div className="topbar-actions">
          <span className="connection"><i />{message}</span>
          <button className="avatar" aria-label="Account menu">BC</button>
        </div>
      </header>

      <div className="workspace" id="top">
        <aside className="rail" aria-label="Project navigation">
          <button className="rail-button active"><span>+</span> New scene</button>
          <nav>
            <a href="#composer" className="selected"><span>✦</span> Create</a>
            <a href="#preview"><span>◫</span> Preview</a>
            <a href="#scene-plan"><span>≡</span> Scene plan</a>
          </nav>
          <div className="rail-note">
            <span>{planner === "agentrouter-responses" ? "AgentRouter · GPT-5.6 Sol" : planner === "agentrouter" ? "AgentRouter · Claude" : planner === "demo" ? "Demo planner" : "Worker offline"}</span>
            <p>{planner === "agentrouter-responses" ? "Prompts are compiled through the OpenAI Responses-compatible route." : planner === "agentrouter" ? "Prompts are compiled through the Anthropic Messages-compatible route." : "The deterministic planner keeps previews available without credentials."}</p>
          </div>
        </aside>

        <section className="stage">
          <div className="hero-copy">
            <span className="eyebrow">Prompt-driven 3D production</span>
            <h1>Describe the shot.<br /><span>We build the scene.</span></h1>
            <p>Turn an idea into a validated 3D plan, inspect the composition, then render it on your Blender worker.</p>
          </div>

          <form className="composer" id="composer" onSubmit={onSubmit}>
            <label htmlFor="prompt">What do you want to create?</label>
            <textarea
              id="prompt"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              maxLength={2000}
              placeholder="A cinematic product reveal in a dark studio…"
            />
            <div className="composer-footer">
              <div className="composer-options" aria-label="Render options">
                <label>Quality<select value={quality} onChange={(event) => setQuality(event.target.value as "preview" | "final")}><option value="preview">Preview</option><option value="final">Final</option></select></label>
                <label>Engine<select value={engine} onChange={(event) => setEngine(event.target.value as "auto" | "cycles" | "eevee")}><option value="auto">Auto-safe</option><option value="cycles">Cycles</option><option value="eevee">EEVEE override</option></select></label>
                <div className="chips" aria-label="Scene defaults">
                  <span>3D animation</span><span>16:9</span><span>2 seconds</span>
                </div>
              </div>
              <div className="composer-actions">
                <button type="submit" className="secondary" disabled={busy !== null}>
                  {busy === "preview" ? "Planning…" : "Generate preview"}
                </button>
                <button type="button" className="primary" disabled={busy !== null} onClick={() => void submit(true)}>
                  {busy === "render" ? "Submitting…" : "Render video"}<span>→</span>
                </button>
              </div>
            </div>
          </form>

          <div className="example-row" aria-label="Example prompts">
            <span>Try</span>
            {EXAMPLES.slice(1).map((example) => (
              <button key={example} onClick={() => setPrompt(example)}>{example}</button>
            ))}
          </div>

          <section className="result-grid" aria-live="polite">
            <article className="preview-card" id="preview">
              <div className="card-heading">
                <div><span className="step">01</span><h2>Composition preview</h2></div>
                {job && <span className={`status status-${job.status}`}>{statusLabel(job.status)}</span>}
              </div>
              <div className="preview-frame">
                {job && previewUrl ? (
                  <object data={previewUrl} type="image/svg+xml" aria-label="Top-down scene composition preview" />
                ) : (
                  <div className="empty-preview">
                    <div className="orb"><span /></div>
                    <strong>Your scene appears here</strong>
                    <p>Generate a preview to inspect placement, camera motion, and timing before rendering.</p>
                  </div>
                )}
              </div>
              {job?.status === "complete" && job.video_url && (
                <a className="download" href={`${API_BASE}${job.video_url}`} target="_blank" rel="noreferrer">Open rendered video ↗</a>
              )}
              {job?.error && <p className="error-message">{job.error}</p>}
            </article>

            <article className="plan-card" id="scene-plan">
              <div className="card-heading"><div><span className="step">02</span><h2>Scene plan</h2></div></div>
              {job ? (
                <div className="plan-content">
                  <div className="plan-stat"><span>Environment</span><strong>{job.plan.environment.preset.replace("_", " ")}</strong><small>{job.plan.environment.time_of_day} · {job.plan.environment.weather}</small></div>
                  <div className="plan-stat"><span>Camera</span><strong>{job.plan.camera.type}</strong><small>{job.plan.camera.focal_mm}mm lens</small></div>
                  <div className="plan-stat"><span>Timeline</span><strong>{job.plan.duration}s</strong><small>{job.plan.fps} fps · {Math.round(job.plan.duration * job.plan.fps)} frames</small></div>
                  <div className="object-list">
                    <span>Objects</span>
                    {job.plan.objects.map((object, index) => (
                      <div key={object.id}><b>{String(index + 1).padStart(2, "0")}</b>{object.color && <i className="object-swatch" style={{ background: `rgb(${Math.round((object.color[0] ?? 0) * 255)}, ${Math.round((object.color[1] ?? 0) * 255)}, ${Math.round((object.color[2] ?? 0) * 255)})` }} />}<p><strong>{object.id}</strong><small>{object.asset.replaceAll("_", " ")}{object.appearance?.metallic !== undefined ? ` · metal ${object.appearance.metallic}` : ""}</small></p></div>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="empty-plan">
                  <p>The planner will resolve your prompt into:</p>
                  <ul><li>Environment and lighting</li><li>Catalog assets</li><li>Spatial relationships</li><li>Animation and camera keys</li></ul>
                </div>
              )}
            </article>
          </section>
        </section>
      </div>
    </main>
  );
}
