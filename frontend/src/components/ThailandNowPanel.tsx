import { useState } from "react";
import { usePolling } from "../api";
import type { ModuleConfig } from "../store";

/**
 * THAILAND NOW — monthly content pipeline. Two tabs sharing a desk-driven engine:
 *   WRITERS — bulk-create blank Docs + Trello cards per writer (Paul/Teerin/TIAN).
 *   EVENTS  — scout upcoming Thailand events → publicity bundle → Doc+card.
 * Desks come from GET /api/thailandnow/desks (the frontend ModuleConfig is
 * sanitized and carries no options). Slice 1: scaffold + desks read only.
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

export default function ThailandNowPanel({ module: _module }: { module: ModuleConfig }) {
  const [tab, setTab] = useState<"writers" | "events">("writers");
  const { data, error } = usePolling<DesksResp>("/api/thailandnow/desks", 10000);

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
        <WritersTab
          desks={data?.desks ?? []}
          ready={data?.ready ?? false}
          loading={!data && !error}
          error={error}
        />
      )}
      {tab === "events" && <EventsStub />}
    </div>
  );
}

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
  return (
    <>
      <section className="hud hud--bracket reveal reveal-1 p-3">
        <div className="label mb-2">DESKS</div>
        {loading ? (
          <div className="mono caret" style={{ color: "var(--color-signal)" }}>
            READING CONFIG
          </div>
        ) : error ? (
          <div className="row-in flex items-center gap-2">
            <span className="pip pip--crit" />
            <span className="mono" style={{ color: "var(--color-critical)" }}>
              {error}
            </span>
          </div>
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

      <section className="hud hud--bracket reveal reveal-2 p-3">
        <div className="label mb-2">PROVISION</div>
        <div className="mono" style={{ color: "var(--color-muted)" }}>
          desk controls arrive in slice 2+
        </div>
      </section>
    </>
  );
}

function EventsStub() {
  return (
    <section className="hud hud--bracket reveal reveal-1 p-3">
      <div className="label mb-2">EVENTS RADAR</div>
      <div className="mono" style={{ color: "var(--color-muted)" }}>
        event scouting + publicity bundle arrive in slice 5+
      </div>
    </section>
  );
}
