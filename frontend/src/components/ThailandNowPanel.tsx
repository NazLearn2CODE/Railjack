import { useCallback, useMemo, useState } from "react";
import { usePolling } from "../api";
import type { ModuleConfig } from "../store";

/**
 * THAILAND NOW — monthly content pipeline. Two tabs sharing a desk-driven engine:
 *   WRITERS — bulk-create blank Docs + Trello cards per writer (Paul/Teerin/TIAN).
 *   EVENTS  — two-tier radar: SCOUT (instant keyless) + DEEP RESEARCH (NotebookLM),
 *             → publicity bundle → images → Doc + card with start/due dates.
 */

interface Desk {
  id: string;
  kind: string;
  count: number;
  doc_name: string;
  card_name: string;
  trello_list_name: string;
  labels: string[];
}
interface DesksResp {
  desks: Desk[];
  trello_board_short: string;
  ready: boolean;
}
interface TnEvent {
  title: string;
  url: string;
  start_date?: string;
  end_date?: string;
  signup_deadline?: string;
  location?: string;
  language?: string;
  summary?: string;
  source?: string;
}
interface ScoutResp {
  events: TnEvent[];
  count: number;
  errors: string[];
  window?: { from: string; to: string; weeks: number };
}
interface ImagesResp {
  images: { url: string; alt: string }[];
  count: number;
  errors: string[];
  note: string;
}
interface ProvisionItem {
  nn: number;
  doc_name: string;
  doc_url: string;
  card_name: string;
  card_url: string;
}
interface ProvisionResp {
  desk_id: string;
  count: number;
  yyyymm: string;
  items: ProvisionItem[];
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

/** Current month as YYYYMM (e.g. 202607) — the doc-naming format; recomputed each mount so it
 *  tracks the current month automatically across reloads. */
function currentYyyyMm(): string {
  const d = new Date();
  return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}`;
}
/** Desk kind shown in the UI, uppercased (TIAN → "EMPTY EVENT", else the kind in caps). */
const kindLabel = (d: Desk) => (d.kind === "event" ? "EMPTY EVENT" : d.kind.toUpperCase());

/** Human label for the weeks slider: "4 weeks" under 8, else "≈N months". */
function weeksLabel(n: number): string {
  if (n < 8) return `${n} week${n === 1 ? "" : "s"}`;
  const m = Math.max(1, Math.round(n / 4.33));
  return `≈${m} month${m === 1 ? "" : "s"}`;
}

export default function ThailandNowPanel({ module: _module }: { module: ModuleConfig }) {
  const [tab, setTab] = useState<"writers" | "events">("writers");
  const { data, error } = usePolling<DesksResp>("/api/thailandnow/desks", 15000);

  return (
    <div className="flex h-full flex-col gap-3 overflow-auto p-3">
      <div className="flex shrink-0 items-center gap-2">
        <button
          className={`btn btn--compact ${tab === "writers" ? "btn--signal" : ""}`}
          onClick={() => setTab("writers")}
        >
          WRITERS
        </button>
        <button
          className={`btn btn--compact ${tab === "events" ? "btn--signal" : ""}`}
          onClick={() => setTab("events")}
        >
          EVENTS
        </button>
      </div>

      {tab === "writers" && (
        <WritersTab desks={data?.desks ?? []} ready={data?.ready ?? false} loading={!data && !error} error={error} />
      )}
      {tab === "events" && <EventsTab />}
    </div>
  );
}

/* --------------------------------- WRITERS -------------------------------- */

function WritersTab({
  desks,
  ready,
  loading,
  error,
}: {
  desks: Desk[];
  ready: boolean;
  loading: boolean;
  error: string | null;
}) {
  const [deskId, setDeskId] = useState("");
  const [count, setCount] = useState(0);
  const [yyyymm, setYyyymm] = useState(currentYyyyMm);
  const desk = desks.find((d) => d.id === deskId) ?? desks[0];
  const n = count || desk?.count || 1;
  const [busy, setBusy] = useState(false);
  const [items, setItems] = useState<ProvisionItem[]>([]);
  const [err, setErr] = useState<string | null>(null);

  const generate = useCallback(async () => {
    if (!desk) return;
    setBusy(true);
    setErr(null);
    const r = await post<ProvisionResp>("/api/thailandnow/provision", {
      desk_id: desk.id,
      count: n,
      yyyymm: yyyymm.length === 6 ? yyyymm : currentYyyyMm(),
    });
    setBusy(false);
    if (r.ok && r.data) setItems(r.data.items);
    else setErr(r.error ?? "provision failed");
  }, [desk, n, yyyymm]);

  return (
    <>
      <section className="hud hud--bracket reveal reveal-1 p-3">
        <div className="label mb-2">DESKS</div>
        {loading ? (
          <div className="mono caret" style={{ color: "var(--color-signal)" }}>
            READING CONFIG
          </div>
        ) : error ? (
          <ErrLine msg={error} />
        ) : !ready || desks.length === 0 ? (
          <div className="mono" style={{ color: "var(--color-muted)" }}>
            no desks configured — add options.desks to configs/tawhan.yaml
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            {desks.map((d) => (
              <div key={d.id} className="row-in flex items-center gap-2">
                <span className="pip pip--signal" />
                <span className="mono">{d.id.toUpperCase()}</span>
                <span className="label">{kindLabel(d)}</span>
                <span className="mono" style={{ color: "var(--color-muted)" }}>
                  ×{d.count}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      {desk && (
        <section className="hud hud--bracket reveal reveal-2 p-3">
          <div className="label mb-2">PROVISION · {desk.id.toUpperCase()}</div>
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <label className="label">DESK</label>
            <select
              className="input"
              style={{ width: "auto" }}
              value={desk.id}
              onChange={(e) => {
                setDeskId(e.target.value);
                setCount(0);
              }}
            >
              {desks.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.id.toUpperCase()} ({kindLabel(d)})
                </option>
              ))}
            </select>
            <label className="label">MONTH</label>
            <input
              className="input"
              type="text"
              inputMode="numeric"
              maxLength={6}
              style={{ width: 96 }}
              value={yyyymm}
              onChange={(e) => setYyyymm(e.target.value.replace(/\D/g, "").slice(0, 6))}
            />
            <label className="label">COUNT</label>
            <input
              className="input"
              type="number"
              min={1}
              max={99}
              style={{ width: 72 }}
              value={n}
              onChange={(e) => setCount(Math.max(1, Math.min(99, Number(e.target.value) || 1)))}
            />
            <button className="btn btn--signal" disabled={busy} onClick={generate}>
              {busy ? "GENERATING…" : "GENERATE"}
            </button>
          </div>
          <div className="flex flex-col gap-1">
            <Row label="DOC" value={desk.doc_name} />
            <Row label="CARD" value={desk.card_name} />
            <Row label="LIST" value={desk.trello_list_name} />
          </div>
          {err && (
            <div className="mt-2">
              <ErrLine msg={err} />
            </div>
          )}
        </section>
      )}

      {items.length > 0 && (
        <section className="hud hud--bracket reveal reveal-3 p-3">
          <div className="label mb-2">CREATED · {items.length}</div>
          <div className="flex flex-col gap-1">
            {items.map((it) => (
              <div key={it.nn} className="row-in flex items-center gap-2">
                <span className="pip pip--go" />
                <a className="mono" href={it.doc_url} target="_blank" rel="noreferrer" style={{ color: "var(--color-signal)" }}>
                  {it.doc_name}
                </a>
                <span className="mono" style={{ color: "var(--color-muted)" }}>
                  →
                </span>
                <a className="mono" href={it.card_url} target="_blank" rel="noreferrer" style={{ color: "var(--color-phosphor-dim)" }}>
                  {it.card_name}
                </a>
              </div>
            ))}
          </div>
        </section>
      )}
    </>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="row-in flex items-baseline gap-2">
      <span className="label" style={{ minWidth: 44 }}>
        {label}
      </span>
      <span className="mono" style={{ color: "var(--color-phosphor-dim)" }}>
        {value}
      </span>
    </div>
  );
}

/* --------------------------------- EVENTS --------------------------------- */

function EventsTab() {
  const [query, setQuery] = useState("");
  const [weeks, setWeeks] = useState(4);
  const [events, setEvents] = useState<TnEvent[]>([]);
  const [fetching, setFetching] = useState(false);
  const [busyLabel, setBusyLabel] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [pickedKey, setPickedKey] = useState<string | null>(null);

  // dedup key + ascending-by-start-date view (stable for events sharing a date)
  const keyOf = (e: TnEvent) => e.url || e.title;
  const sorted = useMemo(
    () =>
      [...events].sort((a, b) => {
        const sa = a.start_date ?? "";
        const sb = b.start_date ?? "";
        return sa < sb ? -1 : sa > sb ? 1 : 0;
      }),
    [events],
  );

  const run = useCallback(
    async (endpoint: "/api/thailandnow/events/scout" | "/api/thailandnow/events/deep") => {
      setFetching(true);
      setErr(null);
      setBusyLabel(endpoint === "/api/thailandnow/events/deep" ? "DEEP RESEARCH via NotebookLM… 1-2 min" : null);
      const r = await post<ScoutResp>(endpoint, { query, weeks });
      setFetching(false);
      setBusyLabel(null);
      if (r.ok && r.data) {
        setEvents((prev) => {
          const map = new Map(prev.map((e) => [keyOf(e), e]));
          for (const e of r.data!.events) map.set(keyOf(e), e);
          return [...map.values()];
        });
      } else {
        setErr(r.error ?? (endpoint === "/api/thailandnow/events/deep" ? "DEEP RESEARCH failed" : "SCOUT failed"));
      }
    },
    [query, weeks],
  );

  const picked = pickedKey ? events.find((e) => keyOf(e) === pickedKey) ?? null : null;
  if (picked) {
    return (
      <ThickBox
        key={pickedKey! /* remount per event so editable-date state reseeds */}
        event={picked}
        onBack={() => setPickedKey(null)}
      />
    );
  }

  return (
    <>
      <section className="hud hud--bracket reveal reveal-1 p-3">
        <div className="label mb-2">SCOUT</div>
        <div className="mb-2 flex items-center gap-3">
          <input
            type="range"
            min={1}
            max={52}
            step={1}
            value={weeks}
            onChange={(e) => setWeeks(Number(e.target.value))}
            style={{ width: 180 }}
          />
          <span className="mono" style={{ color: "var(--color-signal)" }}>
            {weeksLabel(weeks)}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input
            className="input"
            style={{ flexGrow: 1, minWidth: 200 }}
            placeholder="optional: business / culture / festival…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button className="btn btn--signal" disabled={fetching} onClick={() => run("/api/thailandnow/events/scout")}>
            {fetching && !busyLabel ? "SCANNING…" : "SCOUT"}
          </button>
          <button className="btn" disabled={fetching} onClick={() => run("/api/thailandnow/events/deep")}>
            DEEP RESEARCH
          </button>
        </div>
        <div className="mono mt-1" style={{ color: "var(--color-muted)" }}>
          SCOUT is instant keyless; DEEP RESEARCH runs NotebookLM (1-2 min). Both append here.
        </div>
      </section>

      <section className="hud hud--bracket reveal reveal-2 p-3">
        <div className="label mb-2">RESULTS{sorted.length ? ` · ${sorted.length}` : ""}</div>
        {busyLabel ? (
          <div className="mono caret" style={{ color: "var(--color-signal)" }}>
            {busyLabel}
          </div>
        ) : fetching ? (
          <div className="mono caret" style={{ color: "var(--color-signal)" }}>
            SCANNING
          </div>
        ) : err ? (
          <ErrLine msg={err} />
        ) : sorted.length === 0 ? (
          <div className="mono" style={{ color: "var(--color-muted)" }}>
            run SCOUT to list upcoming events — click one to draft its publicity bundle
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            {sorted.map((e, i) => (
              <button
                key={keyOf(e) || i}
                className="row-in flex items-center gap-2"
                style={{ textAlign: "left", background: "transparent", border: 0, cursor: "pointer" }}
                onClick={() => setPickedKey(keyOf(e))}
              >
                <span className="pip pip--signal" />
                {e.start_date && (
                  <span className="mono" style={{ color: "var(--color-muted)", minWidth: 84 }}>
                    {e.start_date}
                  </span>
                )}
                <span className="mono">{e.title}</span>
                {e.source && <span className="label">{e.source}</span>}
              </button>
            ))}
          </div>
        )}
      </section>
    </>
  );
}

function ThickBox({ event, onBack }: { event: TnEvent; onBack: () => void }) {
  const [useUrl, setUseUrl] = useState(true);
  const [bundle, setBundle] = useState("");
  const [writing, setWriting] = useState(false);
  const [pubErr, setPubErr] = useState<string | null>(null);
  const [imgs, setImgs] = useState<{ url: string; alt: string }[]>([]);
  const [imgNote, setImgNote] = useState("");
  const [finding, setFinding] = useState(false);
  const [imgErr, setImgErr] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<ProvisionItem | null>(null);
  const [createErr, setCreateErr] = useState<string | null>(null);

  // editable dates — seed from the scouted event; the backend derives the card start/due
  const [startD, setStartD] = useState(event.start_date ?? "");
  const [endD, setEndD] = useState(event.end_date ?? "");
  const [signupD, setSignupD] = useState(event.signup_deadline ?? "");

  const merged = useCallback(
    (): TnEvent => ({
      ...event,
      start_date: startD || undefined,
      end_date: endD || undefined,
      signup_deadline: signupD || undefined,
    }),
    [event, startD, endD, signupD],
  );

  const genBundle = useCallback(async () => {
    setWriting(true);
    setPubErr(null);
    const ev = merged();
    const r = await post<{ bundle: string }>("/api/thailandnow/events/publicize", {
      event: ev,
      urls: useUrl && ev.url ? [ev.url] : [],
    });
    setWriting(false);
    if (r.ok && r.data) setBundle(r.data.bundle);
    else setPubErr(r.error ?? "bundle failed");
  }, [merged, useUrl]);

  const findImages = useCallback(async () => {
    setFinding(true);
    setImgErr(null);
    const r = await post<ImagesResp>("/api/thailandnow/events/images", { url: event.url });
    setFinding(false);
    if (r.ok && r.data) {
      setImgs(r.data.images);
      setImgNote(r.data.note);
    } else setImgErr(r.error ?? "images failed");
  }, [event.url]);

  const create = useCallback(async () => {
    setCreating(true);
    setCreateErr(null);
    const ev = merged();
    const r = await post<ProvisionResp>("/api/thailandnow/events/create", {
      event: ev,
      urls: useUrl && ev.url ? [ev.url] : [],
      bundle_text: bundle,
    });
    setCreating(false);
    if (r.ok && r.data && r.data.items[0]) setCreated(r.data.items[0]);
    else setCreateErr(r.error ?? "create failed");
  }, [merged, useUrl, bundle]);

  return (
    <>
      <section className="hud hud--bracket reveal reveal-1 p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="label">EVENT</span>
          <button className="btn btn--compact" onClick={onBack}>
            ← BACK
          </button>
        </div>
        <div className="mono" style={{ color: "var(--color-phosphor)" }}>
          {event.title}
        </div>
        {event.location && (
          <div className="mono" style={{ color: "var(--color-muted)" }}>
            {event.location}
          </div>
        )}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <label className="label">START</label>
          <input className="input" type="date" value={startD} onChange={(e) => setStartD(e.target.value)} />
          <label className="label">END</label>
          <input className="input" type="date" value={endD} onChange={(e) => setEndD(e.target.value)} />
          <label className="label">SIGNUP</label>
          <input className="input" type="date" value={signupD} onChange={(e) => setSignupD(e.target.value)} />
        </div>
        <div className="mono mt-1" style={{ color: "var(--color-muted)" }}>
          card start/due are set from these (signup deadline → start = due − 7 days)
        </div>
        <label className="mt-2 flex items-center gap-2">
          <input type="checkbox" checked={useUrl} onChange={(e) => setUseUrl(e.target.checked)} />
          <span className="mono" style={{ color: "var(--color-phosphor-dim)" }}>
            use source URL: {event.url}
          </span>
        </label>
      </section>

      <section className="hud hud--bracket reveal reveal-2 p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="label">PUBLICITY BUNDLE</span>
          <button className="btn btn--signal" disabled={writing} onClick={genBundle}>
            {writing ? "WRITING…" : "GENERATE BUNDLE"}
          </button>
        </div>
        {pubErr && <ErrLine msg={pubErr} />}
        <textarea
          className="input"
          rows={16}
          placeholder="generate a bundle (or paste your own) — review before creating the doc"
          value={bundle}
          onChange={(e) => setBundle(e.target.value)}
          style={{ resize: "vertical", fontFamily: "var(--font-mono)" }}
        />
      </section>

      <section className="hud hud--bracket reveal reveal-3 p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="label">IMAGES · {imgs.length}</span>
          <button className="btn btn--compact" disabled={finding} onClick={findImages}>
            {finding ? "FINDING…" : "FIND IMAGES"}
          </button>
        </div>
        {imgErr && <ErrLine msg={imgErr} />}
        {imgs.length > 0 ? (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(120px,1fr))", gap: 8 }}>
            {imgs.map((im) => (
              <a key={im.url} href={im.url} target="_blank" rel="noreferrer" className="row-in">
                <img
                  src={im.url}
                  alt={im.alt}
                  style={{ width: "100%", height: 80, objectFit: "cover", border: "1px solid var(--color-edge)" }}
                />
              </a>
            ))}
          </div>
        ) : (
          <div className="mono" style={{ color: "var(--color-muted)" }}>
            {imgNote || "find images scraped from the event page"}
          </div>
        )}
      </section>

      <section className="hud hud--bracket reveal reveal-4 p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="label">CREATE</span>
          <button className="btn btn--signal" disabled={creating || !bundle.trim()} onClick={create}>
            {creating ? "CREATING…" : "CREATE DOC + CARD"}
          </button>
        </div>
        {createErr && <ErrLine msg={createErr} />}
        {created && (
          <div className="row-in flex flex-wrap items-center gap-2">
            <span className="pip pip--go" />
            <a className="mono" href={created.doc_url} target="_blank" rel="noreferrer" style={{ color: "var(--color-signal)" }}>
              {created.doc_name}
            </a>
            <span className="mono" style={{ color: "var(--color-muted)" }}>
              +
            </span>
            <a className="mono" href={created.card_url} target="_blank" rel="noreferrer" style={{ color: "var(--color-phosphor-dim)" }}>
              {created.card_name}
            </a>
          </div>
        )}
        {!created && !createErr && (
          <div className="mono" style={{ color: "var(--color-muted)" }}>
            the bundle above becomes the Doc body; the event URL(s) go in the card description
          </div>
        )}
      </section>
    </>
  );
}

function ErrLine({ msg }: { msg: string }) {
  return (
    <div className="row-in flex items-center gap-2">
      <span className="pip pip--crit" />
      <span className="mono" style={{ color: "var(--color-critical)", wordBreak: "break-word" }}>
        {msg}
      </span>
    </div>
  );
}
