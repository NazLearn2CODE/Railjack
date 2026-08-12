import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ModuleConfig } from "../store";

/**
 * FIRESIDE STUDIO — video annotation & production cue stamping tool.
 *
 * Workflow:
 * 1. Load local video file or video URL (native controls, object/web URL).
 * 2. Paste TV episode script & click PROPOSE CUES (calls /api/fireside/propose).
 * 3. Auto-align cues via Groq Whisper (/api/fireside/align) or manually stamp timestamps.
 * 4. Add/edit/reorder manual or AI cues on the timeline and marker bar.
 * 5. Export deliverables: Print cue-sheet (Ben's convention), Copy text, or Download JSON.
 */

export type CueType = "chapter" | "broll" | "onscreen" | "note";

export interface Cue {
  id: string;
  t: number | null;
  type: CueType;
  text: string;
  source: "ai" | "manual";
  beat?: string;
  script_anchor?: string;
}

const CT = { "content-type": "application/json" } as const;

async function post<T>(url: string, body: unknown): Promise<{ ok: boolean; data?: T; error?: string }> {
  const res = await fetch(url, { method: "POST", headers: CT, body: JSON.stringify(body) });
  if (!res.ok) {
    const d = await res.json().catch(() => ({ detail: res.statusText }));
    return { ok: false, error: typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail) };
  }
  return { ok: true, data: (await res.json()) as T };
}

/** seconds → m:ss.s (or h:mm:ss.s past an hour); "–" for non-finite. */
function fmtTs(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s) || s < 0) return "–";
  const tenths = Math.floor((s * 10) % 10);
  const ss = Math.floor(s % 60);
  const mm = Math.floor(s / 60) % 60;
  const hh = Math.floor(s / 3600);
  const pad = (n: number) => String(n).padStart(2, "0");
  return hh > 0 ? `${hh}:${pad(mm)}:${pad(ss)}.${tenths}` : `${mm}:${pad(ss)}.${tenths}`;
}

/** localStorage-backed state hook */
function usePersistentState<T>(key: string, initial: T) {
  const [state, setState] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw != null ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(state));
    } catch {
      /* quota / private mode — fallback to in-memory */
    }
  }, [key, state]);
  return [state, setState] as const;
}

function CuePip({ type }: { type: CueType }) {
  const cls =
    type === "chapter" ? "pip pip--signal" :
    type === "onscreen" ? "pip pip--go" :
    type === "broll" ? "pip pip--hazard" : "pip pip--crit";
  return <span className={cls} title={type} />;
}

export default function FiresideStudioPanel({ module: _module }: { module: ModuleConfig }) {
  // Video player state
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [videoName, setVideoName] = useState<string>("");
  const [videoFile, setVideoFile] = useState<File | null>(null);
  const [inputUrl, setInputUrl] = useState<string>("");
  const [duration, setDuration] = useState<number>(0);
  const [currentTime, setCurrentTime] = useState<number>(0);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Script and cue state (persisted in localStorage)
  const [title, setTitle] = usePersistentState<string>("fireside.title", "");
  const [script, setScript] = usePersistentState<string>("fireside.script", "");
  const [cues, setCues] = usePersistentState<Cue[]>("fireside.cues", []);
  const [showScriptEditor, setShowScriptEditor] = useState<boolean>(true);

  // Propose & Align status
  const [loading, setLoading] = useState<boolean>(false);
  const [aligning, setAligning] = useState<boolean>(false);
  const [alignStatus, setAlignStatus] = useState<string | null>(null);
  const [mode, setMode] = useState<"direct" | "degraded" | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [copied, setCopied] = useState<boolean>(false);

  // Cleanup object URL on unmount or URL change
  useEffect(() => {
    return () => {
      if (videoUrl && videoUrl.startsWith("blob:")) {
        URL.revokeObjectURL(videoUrl);
      }
    };
  }, [videoUrl]);

  const handleVideoFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (videoUrl && videoUrl.startsWith("blob:")) {
      URL.revokeObjectURL(videoUrl);
    }
    const url = URL.createObjectURL(file);
    setVideoFile(file);
    setVideoUrl(url);
    setVideoName(file.name);
    setCurrentTime(0);
  };

  const handleLoadUrl = () => {
    const trimmed = inputUrl.trim();
    if (!trimmed) return;
    if (videoUrl && videoUrl.startsWith("blob:")) {
      URL.revokeObjectURL(videoUrl);
    }
    // ponytail: direct browser playback; add a server /api/fireside/proxy stream if CORS/Drive-auth blocks playback
    setVideoFile(null);
    let hostLabel = trimmed;
    try {
      const u = new URL(trimmed);
      hostLabel = u.hostname;
    } catch {
      /* fallback if url is not a valid absolute URL */
    }
    setVideoUrl(trimmed);
    setVideoName(`URL: ${hostLabel}`);
    setCurrentTime(0);
  };

  const seekTo = useCallback((t: number | null) => {
    if (t == null || !Number.isFinite(t) || !videoRef.current) return;
    videoRef.current.currentTime = t;
    setCurrentTime(t);
  }, []);

  const handlePropose = async () => {
    if (!script.trim() || loading) return;
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await post<{
        cues: Array<{ type: CueType; text: string; beat?: string; script_anchor?: string }>;
        mode: "direct" | "degraded";
      }>(
        "/api/fireside/propose",
        { script: script.trim(), title: title.trim() || undefined }
      );
      if (!res.ok || !res.data) {
        setErrorMsg(res.error || "Proposal request failed");
        setMode("degraded");
        return;
      }
      setMode(res.data.mode);
      const incoming: Cue[] = (res.data.cues || []).map((c, i) => ({
        id: `ai-${Date.now()}-${i}-${Math.random().toString(36).slice(2, 7)}`,
        t: null,
        type: (["chapter", "broll", "onscreen", "note"].includes(c.type) ? c.type : "note") as CueType,
        text: c.text || "",
        source: "ai",
        beat: c.beat,
        script_anchor: c.script_anchor || "",
      }));
      if (incoming.length > 0) {
        setCues((prev) => [...prev, ...incoming]);
      } else if (res.data.mode === "degraded") {
        setErrorMsg("LLM returned degraded/empty cues. Please verify script content.");
      }
    } catch (e: unknown) {
      setErrorMsg(e instanceof Error ? e.message : "Network error");
      setMode("degraded");
    } finally {
      setLoading(false);
    }
  };

  const handleAutoStamp = async () => {
    if (aligning || cues.length === 0) return;
    if (!videoFile && !videoUrl) {
      setErrorMsg("Please load a video file or enter a video URL first.");
      return;
    }
    setAligning(true);
    setErrorMsg(null);
    setAlignStatus(null);
    try {
      const formData = new FormData();
      if (videoFile) {
        formData.append("video", videoFile);
      } else if (videoUrl) {
        formData.append("video_url", videoUrl);
      }
      formData.append("cues", JSON.stringify(cues));

      const res = await fetch("/api/fireside/align", {
        method: "POST",
        body: formData,
      });

      if (!res.ok) {
        const d = await res.json().catch(() => ({ detail: res.statusText }));
        const err = typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail);
        setErrorMsg(err || "Alignment request failed");
        return;
      }

      const data = (await res.json()) as {
        cues: Array<Cue>;
        mode: "aligned" | "degraded";
        matched?: number;
        total?: number;
        hint?: string;
      };

      if (data.mode === "degraded") {
        setErrorMsg(data.hint ? `Auto-stamp degraded: ${data.hint}` : "Auto-stamp degraded. Could not align timestamps.");
      }

      if (Array.isArray(data.cues)) {
        const returnedCues = data.cues;
        setCues((prev) =>
          prev.map((oldCue, idx) => {
            const ret = returnedCues.find((rc) => rc.id === oldCue.id) || returnedCues[idx];
            if (ret && ret.t != null) {
              return {
                ...oldCue,
                t: ret.t,
                script_anchor: ret.script_anchor ?? oldCue.script_anchor,
              };
            }
            return oldCue;
          })
        );
        const matched = data.matched ?? data.cues.filter((c) => c.t != null).length;
        const total = data.total ?? data.cues.length;
        setAlignStatus(`Auto-stamped ${matched}/${total}`);
        setTimeout(() => setAlignStatus(null), 5000);
      }
    } catch (e: unknown) {
      setErrorMsg(e instanceof Error ? e.message : "Network error during alignment");
    } finally {
      setAligning(false);
    }
  };

  const stampCue = (id: string) => {
    const t = videoRef.current ? videoRef.current.currentTime : currentTime;
    setCues((prev) => prev.map((c) => (c.id === id ? { ...c, t } : c)));
  };

  const addManualCue = () => {
    const t = videoRef.current ? videoRef.current.currentTime : currentTime;
    const newCue: Cue = {
      id: `manual-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      t: Number.isFinite(t) ? t : 0,
      type: "onscreen",
      text: "",
      source: "manual",
    };
    setCues((prev) => [...prev, newCue]);
  };

  const updateCueText = (id: string, text: string) => {
    setCues((prev) => prev.map((c) => (c.id === id ? { ...c, text } : c)));
  };

  const updateCueType = (id: string, type: CueType) => {
    setCues((prev) => prev.map((c) => (c.id === id ? { ...c, type } : c)));
  };

  const deleteCue = (id: string) => {
    setCues((prev) => prev.filter((c) => c.id !== id));
  };

  const clearAllCues = () => {
    if (window.confirm("Clear all cues from the session?")) {
      setCues([]);
    }
  };

  // Split and sort cues
  const unassignedCues = useMemo(() => cues.filter((c) => c.t === null), [cues]);
  const assignedCues = useMemo(
    () => cues.filter((c): c is Cue & { t: number } => c.t !== null).sort((a, b) => a.t - b.t),
    [cues]
  );

  // Copy plain-text cue list
  const handleCopy = () => {
    const lines: string[] = [];
    if (title) lines.push(`THE FIRESIDE — ${title.toUpperCase()}`);
    lines.push(`Generated: ${new Date().toLocaleDateString()}`);
    lines.push("");

    if (assignedCues.length > 0) {
      lines.push("=== TIMECODED PRODUCTION CUES ===");
      assignedCues.forEach((c) => {
        lines.push(`[${fmtTs(c.t)}] [${c.type.toUpperCase()}] ${c.text}`);
      });
      lines.push("");
    }

    if (unassignedCues.length > 0) {
      lines.push("=== UNASSIGNED (TO STAMP) ===");
      unassignedCues.forEach((c) => {
        lines.push(`[UNSET] [${c.type.toUpperCase()}] ${c.text}`);
      });
    }

    navigator.clipboard.writeText(lines.join("\n"));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  // Download JSON blob
  const handleDownloadJSON = () => {
    const exportData = {
      title: title || "The Fireside Episode",
      video_file: videoName || undefined,
      exported_at: new Date().toISOString(),
      cues: [...assignedCues, ...unassignedCues],
    };
    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const safeTitle = (title || "fireside").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    a.href = url;
    a.download = `${safeTitle}-cues.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // Print deliverable
  const handlePrint = () => {
    window.print();
  };

  // Seek on clicking marker track
  const handleMarkerTrackClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!duration || !videoRef.current) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const ratio = Math.max(0, Math.min(1, clickX / rect.width));
    seekTo(ratio * duration);
  };

  return (
    <div className="flex h-full w-full flex-col overflow-hidden bg-vacuum">
      {/* Top Action & Export Bar */}
      <header className="hud hud--bracket flex shrink-0 items-center justify-between gap-3 border-b border-edge bg-panel px-4 py-2">
        <div className="flex items-center gap-3">
          <span className="panel-title">FIRESIDE STUDIO</span>
          <span className="label text-muted">|</span>
          <span className="mono text-xs text-phosphor-dim">
            {assignedCues.length} stamped · {unassignedCues.length} to stamp
          </span>
          {alignStatus && (
            <span className="mono text-xs font-bold text-go bg-go/10 border border-go/30 px-2 py-0.5 rounded">
              {alignStatus}
            </span>
          )}
          {mode === "degraded" && (
            <span className="flex items-center gap-1.5 text-xs text-hazard" title="Degraded mode: fallback or empty response">
              <span className="pip pip--hazard" />
              <span className="label">DEGRADED</span>
            </span>
          )}
          {mode === "direct" && (
            <span className="flex items-center gap-1.5 text-xs text-go" title="Direct LLM response">
              <span className="pip pip--go" />
              <span className="label">READY</span>
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            className="btn btn--compact btn--signal flex items-center gap-1.5"
            disabled={aligning || cues.length === 0 || (!videoFile && !videoUrl)}
            onClick={handleAutoStamp}
            title="Auto-transcribe audio with Groq Whisper & align cue timestamps"
          >
            <span>{aligning ? "ALIGNING..." : "⚡ AUTO-STAMP"}</span>
          </button>
          <button
            type="button"
            className="btn btn--compact btn--signal flex items-center gap-1.5"
            onClick={addManualCue}
            title="Add a manual cue at current video time"
          >
            <span>+ ADD CUE</span>
          </button>
          <button
            type="button"
            className="btn btn--compact flex items-center gap-1.5"
            onClick={handleCopy}
            title="Copy plain-text timecoded cue sheet"
          >
            <span>{copied ? "COPIED ✓" : "COPY"}</span>
          </button>
          <button
            type="button"
            className="btn btn--compact flex items-center gap-1.5"
            onClick={handleDownloadJSON}
            title="Download JSON cue sheet"
          >
            <span>JSON</span>
          </button>
          <button
            type="button"
            className="btn btn--compact btn--signal flex items-center gap-1.5"
            onClick={handlePrint}
            title="Print production cue sheet (Ben's convention)"
          >
            <span>PRINT CUE-SHEET</span>
          </button>
          {cues.length > 0 && (
            <button
              type="button"
              className="btn btn--compact btn--crit opacity-75 hover:opacity-100"
              onClick={clearAllCues}
              title="Clear all cues"
            >
              CLEAR
            </button>
          )}
        </div>
      </header>

      {/* Main 2-Bay Working Area */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 p-3 lg:grid-cols-12 overflow-hidden">
        {/* Left Column: Video Loader + Player + Marker Bar + Script/Propose (6 cols) */}
        <section className="hud hud--glass flex min-h-0 flex-col gap-3 p-3 lg:col-span-6 overflow-y-auto">
          {/* Video Player Bay */}
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between flex-wrap gap-2">
              <span className="label">1. VIDEO PLAYER</span>
              <div className="flex items-center gap-2">
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="video/*"
                  onChange={handleVideoFile}
                  className="hidden"
                  id="fireside-video-upload"
                />
                <button
                  type="button"
                  className="btn btn--compact flex items-center gap-1"
                  onClick={() => fileInputRef.current?.click()}
                >
                  <span>{videoFile ? "CHANGE FILE" : "LOAD LOCAL VIDEO"}</span>
                </button>
                {videoName && (
                  <span className="mono max-w-[180px] truncate text-xs text-muted" title={videoName}>
                    {videoName}
                  </span>
                )}
              </div>
            </div>

            {/* URL Input Row */}
            <div className="flex items-center gap-2">
              <input
                type="url"
                className="input text-xs flex-1"
                placeholder="Or paste video MP4 / Google Drive URL..."
                value={inputUrl}
                onChange={(e) => setInputUrl(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    handleLoadUrl();
                  }
                }}
              />
              <button
                type="button"
                className="btn btn--compact text-xs shrink-0"
                onClick={handleLoadUrl}
                disabled={!inputUrl.trim()}
              >
                LOAD FROM URL
              </button>
            </div>

            {/* Video Element or Dropzone */}
            <div className="relative flex aspect-video w-full items-center justify-center overflow-hidden rounded border border-edge bg-void">
              {videoUrl ? (
                <video
                  ref={videoRef}
                  src={videoUrl}
                  controls
                  className="h-full w-full object-contain"
                  onLoadedMetadata={(e) => {
                    const dur = e.currentTarget.duration;
                    setDuration(Number.isFinite(dur) ? dur : 0);
                  }}
                  onTimeUpdate={(e) => {
                    setCurrentTime(e.currentTarget.currentTime);
                  }}
                />
              ) : (
                <div
                  onClick={() => fileInputRef.current?.click()}
                  className="flex cursor-pointer flex-col items-center justify-center gap-2 p-6 text-center text-muted hover:text-signal transition-colors"
                >
                  <span className="text-3xl">▶</span>
                  <span className="label">CLICK TO LOAD LOCAL VIDEO OR PASTE URL ABOVE</span>
                  <span className="mono text-xs opacity-75">MP4, WebM, MOV, or direct URL stream</span>
                </div>
              )}
            </div>

            {/* Marker Bar (Thin Interactive Timeline Under Video) */}
            <div className="flex flex-col gap-1">
              <div className="flex items-center justify-between text-xs mono">
                <span className="text-signal font-bold">{fmtTs(currentTime)}</span>
                <span className="text-muted">/ {fmtTs(duration)}</span>
              </div>
              <div
                onClick={handleMarkerTrackClick}
                className="relative h-6 w-full cursor-pointer rounded border border-edge bg-void select-none"
                title="Click timeline to seek"
              >
                {/* Track progress fill */}
                {duration > 0 && (
                  <div
                    className="absolute top-0 bottom-0 left-0 bg-signal/15 pointer-events-none"
                    style={{ width: `${Math.min(100, (currentTime / duration) * 100)}%` }}
                  />
                )}

                {/* Live Playhead Line */}
                {duration > 0 && (
                  <div
                    className="absolute top-0 bottom-0 w-0.5 bg-signal pointer-events-none z-20 shadow-[0_0_8px_#38e0ff]"
                    style={{ left: `${Math.min(100, (currentTime / duration) * 100)}%` }}
                  />
                )}

                {/* Stamped Cue Pips on Marker Bar */}
                {duration > 0 &&
                  assignedCues.map((c) => {
                    const pct = Math.max(0, Math.min(100, (c.t / duration) * 100));
                    const color =
                      c.type === "chapter" ? "#38e0ff" :
                      c.type === "broll" ? "#ffb648" :
                      c.type === "onscreen" ? "#3ddc97" : "#ff4d6d";
                    return (
                      <button
                        key={c.id}
                        type="button"
                        className="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 h-3.5 w-3.5 rounded-full z-10 transition-transform hover:scale-150 focus:outline-none"
                        style={{
                          left: `${pct}%`,
                          backgroundColor: color,
                          boxShadow: `0 0 6px ${color}`,
                        }}
                        onClick={(e) => {
                          e.stopPropagation();
                          seekTo(c.t);
                        }}
                        title={`[${fmtTs(c.t)}] (${c.type.toUpperCase()}) ${c.text}`}
                      />
                    );
                  })}
              </div>
              <div className="flex items-center justify-between text-[10px] mono text-faint">
                <span>0:00.0</span>
                <div className="flex items-center gap-3">
                  <span className="flex items-center gap-1"><span className="pip pip--signal" /> Chapter</span>
                  <span className="flex items-center gap-1"><span className="pip pip--go" /> Onscreen</span>
                  <span className="flex items-center gap-1"><span className="pip pip--hazard" /> B-roll</span>
                  <span className="flex items-center gap-1"><span className="pip pip--crit" /> Note</span>
                </div>
                <span>{fmtTs(duration)}</span>
              </div>
            </div>
          </div>

          {/* Script + Propose Bay */}
          <div className="flex flex-col gap-2 border-t border-edge pt-3">
            <div className="flex items-center justify-between">
              <span className="label">2. EPISODE SCRIPT & AI CUE PROPOSER</span>
              <button
                type="button"
                className="btn btn--compact text-xs"
                onClick={() => setShowScriptEditor((v) => !v)}
              >
                {showScriptEditor ? "COLLAPSE" : "EXPAND"}
              </button>
            </div>

            {showScriptEditor && (
              <div className="flex flex-col gap-2">
                <div>
                  <label className="label block mb-1 text-[11px]">Episode Title (Optional)</label>
                  <input
                    type="text"
                    className="input text-xs"
                    placeholder="e.g. EP 42 — High Speed Rail & Digital Nomad Visas"
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                  />
                </div>

                <div>
                  <label className="label block mb-1 text-[11px]">Draft Script</label>
                  <textarea
                    rows={6}
                    className="input text-xs font-mono resize-y"
                    placeholder="Paste the episode script here (Ben + co-host dialogue)..."
                    value={script}
                    onChange={(e) => setScript(e.target.value)}
                  />
                </div>

                <div className="flex items-center justify-between gap-2">
                  <button
                    type="button"
                    className="btn btn--signal flex-1 text-xs"
                    disabled={loading || !script.trim()}
                    onClick={handlePropose}
                  >
                    {loading ? "PROPOSING CUES (GLM-5)..." : "PROPOSE PRODUCTION CUES"}
                  </button>
                </div>

                {errorMsg && (
                  <div className="mono text-xs text-critical bg-critical/10 border border-critical/30 p-2 rounded">
                    {errorMsg}
                  </div>
                )}
              </div>
            )}
          </div>
        </section>

        {/* Right Column: Cue Timeline, TO STAMP Bucket & Assigned Cues (6 cols) */}
        <section className="hud hud--glass flex min-h-0 flex-col gap-3 p-3 lg:col-span-6 overflow-hidden">
          <div className="flex items-center justify-between border-b border-edge pb-2">
            <div className="flex items-center gap-2">
              <span className="label">3. PRODUCTION CUE SHEET</span>
              <span className="mono text-xs text-muted">
                ({assignedCues.length + unassignedCues.length} total)
              </span>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="btn btn--compact btn--signal text-xs"
                disabled={aligning || cues.length === 0 || (!videoFile && !videoUrl)}
                onClick={handleAutoStamp}
                title="Auto-align cue timestamps using Groq Whisper speech-to-text"
              >
                {aligning ? "ALIGNING..." : "⚡ AUTO-STAMP"}
              </button>
              <button
                type="button"
                className="btn btn--compact"
                onClick={addManualCue}
              >
                + STAMP NEW CUE
              </button>
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto pr-1">
            {/* TO STAMP Bucket (Unassigned cues from AI proposal) */}
            {unassignedCues.length > 0 && (
              <div className="flex flex-col gap-2 rounded border border-hazard/30 bg-hazard-deep/10 p-2.5">
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <span className="label text-hazard flex items-center gap-1.5">
                    <span className="pip pip--hazard" />
                    TO STAMP ({unassignedCues.length})
                  </span>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      className="btn btn--compact btn--signal text-xs"
                      disabled={aligning || (!videoFile && !videoUrl)}
                      onClick={handleAutoStamp}
                      title="Auto-align unassigned cues to spoken transcript"
                    >
                      {aligning ? "ALIGNING..." : "⚡ AUTO-STAMP"}
                    </button>
                    <span className="mono text-[11px] text-muted">
                      or stamp manually while watching
                    </span>
                  </div>
                </div>

                <div className="flex flex-col gap-2 max-h-56 overflow-y-auto pr-1">
                  {unassignedCues.map((cue) => (
                    <div
                      key={cue.id}
                      className="hud hud--glass flex flex-col gap-1.5 p-2 rounded border border-edge"
                    >
                      <div className="flex items-center gap-2">
                        <CuePip type={cue.type} />
                        <select
                          className="input text-xs py-0.5 px-1.5 w-28 uppercase font-mono"
                          value={cue.type}
                          onChange={(e) => updateCueType(cue.id, e.target.value as CueType)}
                        >
                          <option value="chapter">CHAPTER</option>
                          <option value="onscreen">ONSCREEN</option>
                          <option value="broll">B-ROLL</option>
                          <option value="note">NOTE</option>
                        </select>
                        {cue.beat && (
                          <span className="label text-[10px] text-muted truncate max-w-[140px]" title={cue.beat}>
                            {cue.beat}
                          </span>
                        )}
                        <div className="ml-auto flex items-center gap-1.5">
                          <button
                            type="button"
                            className="btn btn--compact btn--signal text-xs font-bold"
                            onClick={() => stampCue(cue.id)}
                            title="Stamp with current video playback timestamp"
                          >
                            STAMP {fmtTs(currentTime)}
                          </button>
                          <button
                            type="button"
                            className="btn btn--compact btn--crit text-xs"
                            onClick={() => deleteCue(cue.id)}
                            title="Delete cue"
                          >
                            ✕
                          </button>
                        </div>
                      </div>
                      <input
                        type="text"
                        className="input text-xs py-1"
                        value={cue.text}
                        onChange={(e) => updateCueText(cue.id, e.target.value)}
                        placeholder="Cue text..."
                      />
                      {cue.script_anchor && (
                        <div className="mono text-[10px] text-phosphor-dim truncate" title={`Anchor: "${cue.script_anchor}"`}>
                          ⚓ &ldquo;{cue.script_anchor}&rdquo;
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Assigned Cues (Chronological Timeline) */}
            <div className="flex min-h-0 flex-1 flex-col gap-2">
              <div className="flex items-center justify-between">
                <span className="label flex items-center gap-1.5">
                  <span className="pip pip--go" />
                  TIMECODED CUES ({assignedCues.length})
                </span>
                <span className="mono text-[11px] text-muted">
                  Click timecode to seek video
                </span>
              </div>

              {assignedCues.length === 0 ? (
                <div className="flex flex-1 flex-col items-center justify-center p-8 text-center text-muted border border-dashed border-edge rounded">
                  <span className="text-xl mb-1">⏱</span>
                  <span className="label">NO TIMECODED CUES YET</span>
                  <span className="mono text-xs opacity-75 mt-1">
                    Paste an episode script on the left to propose cues, or click "+ STAMP NEW CUE" to record markers live while watching.
                  </span>
                </div>
              ) : (
                <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto pr-1">
                  {assignedCues.map((cue) => (
                    <div
                      key={cue.id}
                      className="hud hud--glass flex flex-col gap-1.5 p-2 rounded border border-edge hover:border-signal/50 transition-colors"
                    >
                      <div className="flex items-center gap-2 flex-wrap">
                        <CuePip type={cue.type} />
                        <button
                          type="button"
                          className="btn btn--compact mono text-xs font-bold text-signal hover:underline"
                          onClick={() => seekTo(cue.t)}
                          title="Seek video to this timestamp"
                        >
                          ▸ {fmtTs(cue.t)}
                        </button>
                        <select
                          className="input text-xs py-0.5 px-1.5 w-28 uppercase font-mono"
                          value={cue.type}
                          onChange={(e) => updateCueType(cue.id, e.target.value as CueType)}
                        >
                          <option value="chapter">CHAPTER</option>
                          <option value="onscreen">ONSCREEN</option>
                          <option value="broll">B-ROLL</option>
                          <option value="note">NOTE</option>
                        </select>
                        <span className="label text-[10px] text-faint uppercase">
                          {cue.source}
                        </span>
                        <div className="ml-auto flex items-center gap-1.5">
                          <button
                            type="button"
                            className="btn btn--compact text-xs"
                            onClick={() => stampCue(cue.id)}
                            title="Update timestamp to current video playhead"
                          >
                            RE-STAMP
                          </button>
                          <button
                            type="button"
                            className="btn btn--compact btn--crit text-xs"
                            onClick={() => deleteCue(cue.id)}
                            title="Delete cue"
                          >
                            ✕
                          </button>
                        </div>
                      </div>

                      {/* Cue Text Input */}
                      <input
                        type="text"
                        className="input text-xs py-1"
                        value={cue.text}
                        onChange={(e) => updateCueText(cue.id, e.target.value)}
                        placeholder="Cue description or lower-third copy..."
                      />

                      {cue.script_anchor && (
                        <div className="mono text-[10px] text-phosphor-dim truncate" title={`Anchor: "${cue.script_anchor}"`}>
                          ⚓ &ldquo;{cue.script_anchor}&rdquo;
                        </div>
                      )}

                      {/* Ben's Convention Visual Style Preview */}
                      <div className="pt-0.5">
                        {cue.type === "chapter" && (
                          <div className="hud hud--bracket border border-signal/40 bg-panel-2 px-2.5 py-1 text-xs font-display font-bold uppercase tracking-wider text-signal">
                            CARD BLOCK: {cue.text || "—"}
                          </div>
                        )}
                        {cue.type === "onscreen" && (
                          <div className="rounded bg-white px-2 py-0.5 text-xs font-mono font-bold text-black inline-block shadow-sm">
                            LOWER-THIRD: {cue.text || "—"}
                          </div>
                        )}
                        {cue.type === "broll" && (
                          <div className="rounded border border-hazard/30 bg-hazard-deep/20 px-2 py-0.5 text-xs font-mono text-hazard">
                            B-ROLL FOOTAGE: {cue.text || "—"}
                          </div>
                        )}
                        {cue.type === "note" && (
                          <div className="rounded border border-critical/30 bg-black/40 px-2 py-0.5 text-xs font-mono italic text-critical">
                            PRODUCTION NOTE: {cue.text || "—"}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </section>
      </div>

      {/* Hidden Print-Stylesheet Cue-Sheet Deliverable (Ben's convention) */}
      <div className="fireside-print-deliverable hidden print:block">
        <style>{`
          @media print {
            body * {
              visibility: hidden;
            }
            .fireside-print-deliverable, .fireside-print-deliverable * {
              visibility: visible;
            }
            .fireside-print-deliverable {
              position: absolute;
              left: 0;
              top: 0;
              width: 100%;
              background: #ffffff !important;
              color: #000000 !important;
              display: block !important;
              padding: 24px;
              font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans", sans-serif;
            }
          }
        `}</style>
        <div style={{ marginBottom: "20px", borderBottom: "2px solid #000000", paddingBottom: "12px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end" }}>
            <div>
              <h1 style={{ fontSize: "22px", margin: "0 0 4px 0", fontWeight: 800, textTransform: "uppercase", letterSpacing: "0.05em" }}>
                NBT WORLD · THE FIRESIDE
              </h1>
              <h2 style={{ fontSize: "16px", margin: "0", fontWeight: 600, color: "#333333" }}>
                {title ? title : "Episode Production Cue Sheet"}
              </h2>
            </div>
            <div style={{ textAlign: "right", fontSize: "12px", color: "#666666", fontFamily: "monospace" }}>
              <div>DATE: {new Date().toLocaleDateString()}</div>
              <div>CUES: {assignedCues.length} STAMPED</div>
            </div>
          </div>
        </div>

        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px" }}>
          <thead>
            <tr style={{ borderBottom: "2px solid #000000", textAlign: "left", fontSize: "11px", letterSpacing: "0.08em" }}>
              <th style={{ padding: "6px 8px", width: "90px" }}>TIMECODE</th>
              <th style={{ padding: "6px 8px", width: "100px" }}>TYPE</th>
              <th style={{ padding: "6px 8px" }}>PRODUCTION CUE & ON-SCREEN SPECIFICATION</th>
              <th style={{ padding: "6px 8px", width: "70px", textAlign: "right" }}>SOURCE</th>
            </tr>
          </thead>
          <tbody>
            {assignedCues.map((c) => (
              <tr key={c.id} style={{ borderBottom: "1px solid #e0e0e0" }}>
                <td style={{ padding: "8px", fontFamily: "monospace", fontWeight: "bold", fontSize: "12px" }}>
                  {fmtTs(c.t)}
                </td>
                <td style={{ padding: "8px", textTransform: "uppercase", fontWeight: "bold", fontSize: "11px" }}>
                  {c.type}
                </td>
                <td style={{ padding: "8px" }}>
                  {c.type === "chapter" ? (
                    <div style={{ background: "#f3f4f6", borderLeft: "4px solid #111827", padding: "6px 10px", fontWeight: "bold", fontSize: "14px", letterSpacing: "0.04em", textTransform: "uppercase" }}>
                      {c.text}
                    </div>
                  ) : c.type === "onscreen" ? (
                    <div style={{ display: "inline-block", color: "#000000", fontWeight: "bold", background: "#ffffff", border: "1.5px solid #000000", padding: "3px 8px", fontSize: "13px" }}>
                      {c.text}
                    </div>
                  ) : c.type === "broll" ? (
                    <div style={{ color: "#c2410c", fontWeight: 600, fontSize: "13px" }}>
                      {c.text}
                    </div>
                  ) : (
                    <div style={{ color: "#b91c1c", fontStyle: "italic", fontSize: "13px" }}>
                      {c.text}
                    </div>
                  )}
                </td>
                <td style={{ padding: "8px", textAlign: "right", fontSize: "10px", color: "#888888", textTransform: "uppercase" }}>
                  {c.source}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {unassignedCues.length > 0 && (
          <div style={{ marginTop: "24px", pageBreakBefore: "avoid" }}>
            <h3 style={{ fontSize: "13px", textTransform: "uppercase", color: "#888888", borderBottom: "1px solid #ccc", paddingBottom: "4px" }}>
              Unassigned Cues (To Stamp)
            </h3>
            <ul style={{ margin: "8px 0", paddingLeft: "20px", fontSize: "12px" }}>
              {unassignedCues.map((c) => (
                <li key={c.id} style={{ margin: "4px 0" }}>
                  <strong>[{c.type.toUpperCase()}]</strong> {c.text}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
