import { useCallback, useState } from "react";
import { usePolling } from "../api";
import type { ModuleConfig } from "../store";

/**
 * THAILAND NOW — monthly content pipeline. Two tabs sharing a desk-driven engine:
 *   WRITERS — bulk-create blank Docs + Trello cards per writer (Paul/Teerin/TIAN).
 *   EVENTS  — scout upcoming events → publicity bundle → Doc+card.
 *
 * The EVENTS scout→bundle→images flow is live. The GENERATE / CREATE DOC+CARD
 * actions create Google Docs + Trello cards via /provision, which is pending the
 * Google OAuth client (documents+drive) — they're disabled with a reason until then.
 */

interface Desk {
  id: string;
  kind: string;
  count: number;
  doc_name: string;
  card_name: string;
  trello_list_name: string;
}
interface DesksResp {
  desks: Desk[];
  trello_board_short: string;
  ready: boolean;
}
interface TnEvent {
  title: string;
  url: string;
  date?: string;
  location?: string;
  source?: string;
}
interface ScoutResp {
  events: TnEvent[];
  count: number;
  errors: string[];
}
interface ImagesResp {
  images: { url: string; alt: string }[];
  count: number;
  errors: string[];
  note: string;
}

const CT = { "content-type": "application/json" } as const;
const CREATION_HELD =
  "doc/card creation is pending the Google OAuth client (documents+drive) and the /provision endpoint — paste the fast-reactor client and it goes live.";

async function post<T>(url: string, body: unknown): Promise<{ ok: boolean; data?: T; error?: string }> {
  const res = await fetch(url, { method: "POST", headers: CT, body: JSON.stringify(body) });
  if (!res.ok) {
    const d = await res.json().catch(() => ({ detail: res.statusText }));
    return { ok: false, error: typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail) };
  }
  return { ok: true, data: (await res.json()) as T };
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
  const desk = desks.find((d) => d.id === deskId) ?? desks[0];

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
                <span className="label">{d.kind}</span>
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
              onChange={(e) => setDeskId(e.target.value)}
            >
              {desks.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.id} ({d.kind})
                </option>
              ))}
            </select>
            <span className="mono" style={{ color: "var(--color-muted)" }}>
              count ×{desk.count}
            </span>
          </div>
          <div className="flex flex-col gap-1">
            <Row label="DOC" value={desk.doc_name} />
            <Row label="CARD" value={desk.card_name} />
            <Row label="LIST" value={desk.trello_list_name} />
          </div>
          <div className="mt-2">
            <button className="btn btn--signal" disabled title={CREATION_HELD}>
              GENERATE
            </button>
            <div className="mono mt-1" style={{ color: "var(--color-hazard)" }}>
              ⧖ {CREATION_HELD}
            </div>
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
  const [scouting, setScouting] = useState(false);
  const [events, setEvents] = useState<TnEvent[]>([]);
  const [scoutErr, setScoutErr] = useState<string | null>(null);
  const [picked, setPicked] = useState<TnEvent | null>(null);

  const scout = useCallback(async () => {
    setScouting(true);
    setScoutErr(null);
    const r = await post<ScoutResp>("/api/thailandnow/events/scout", { query });
    setScouting(false);
    if (r.ok && r.data) setEvents(r.data.events);
    else setScoutErr(r.error ?? "scout failed");
  }, [query]);

  if (picked) return <ThickBox event={picked} onBack={() => setPicked(null)} />;

  return (
    <>
      <section className="hud hud--bracket reveal reveal-1 p-3">
        <div className="label mb-2">SCOUT</div>
        <div className="flex flex-wrap items-center gap-2">
          <input
            className="input"
            style={{ flexGrow: 1, minWidth: 200 }}
            placeholder="optional: business / culture / festival…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button className="btn btn--signal" disabled={scouting} onClick={scout}>
            {scouting ? "SCANNING…" : "SCOUT"}
          </button>
        </div>
        <div className="mono mt-1" style={{ color: "var(--color-muted)" }}>
          primary source: thailandnow.in.th/events — Naz's own site (TIAN's beat)
        </div>
      </section>

      <section className="hud hud--bracket reveal reveal-2 p-3">
        <div className="label mb-2">RESULTS{events.length ? ` · ${events.length}` : ""}</div>
        {scouting ? (
          <div className="mono caret" style={{ color: "var(--color-signal)" }}>
            SCANNING
          </div>
        ) : scoutErr ? (
          <ErrLine msg={scoutErr} />
        ) : events.length === 0 ? (
          <div className="mono" style={{ color: "var(--color-muted)" }}>
            run SCOUT to list upcoming events — click one to draft its publicity bundle
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            {events.map((e) => (
              <button
                key={e.url}
                className="row-in flex items-center gap-2"
                style={{ textAlign: "left", background: "transparent", border: 0, cursor: "pointer" }}
                onClick={() => setPicked(e)}
              >
                <span className="pip pip--signal" />
                <span className="mono">{e.title}</span>
                {e.source === "duckduckgo" && <span className="label">ddg</span>}
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

  const genBundle = useCallback(async () => {
    setWriting(true);
    setPubErr(null);
    const r = await post<{ bundle: string }>("/api/thailandnow/events/publicize", {
      event,
      urls: useUrl ? [event.url] : [],
    });
    setWriting(false);
    if (r.ok && r.data) setBundle(r.data.bundle);
    else setPubErr(r.error ?? "bundle failed");
  }, [event, useUrl]);

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
            {event.date ? ` · ${event.date}` : ""}
          </div>
        )}
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
          <button className="btn btn--signal" disabled={writing || !useUrl} onClick={genBundle}>
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
        <button className="btn btn--signal" disabled title={CREATION_HELD}>
          CREATE DOC + CARD
        </button>
        <div className="mono mt-1" style={{ color: "var(--color-hazard)" }}>
          ⧖ {CREATION_HELD}
        </div>
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
