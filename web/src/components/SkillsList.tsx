import { useStore } from "../store";

export default function SkillsList() {
  const skills = useStore((s) => s.skills);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      {skills.length === 0 ? (
        <div className="px-3 py-6 text-center">
          <div className="label mb-2">NO SKILLS</div>
          <p className="text-[11px] leading-relaxed text-faint">
            No agent skills scanned in workspace or user directory.
          </p>
        </div>
      ) : (
        skills.map((sk) => (
          <div
            key={`${sk.source}-${sk.name}`}
            className="border-b border-edge-soft px-3 py-2.5 hover:bg-panel-2"
          >
            <div className="mb-1 flex items-center justify-between gap-2">
              <span className="display text-[11px] font-semibold text-phosphor-dim tracking-[0.05em] truncate" title={sk.name}>
                {sk.name}
              </span>
              <span className="mono text-[10px] bg-edge px-1.5 py-0.5 text-faint uppercase shrink-0">
                {sk.source}
              </span>
            </div>
            <p className="text-[10px] text-faint leading-relaxed line-clamp-2" title={sk.description}>
              {sk.description || "No description provided."}
            </p>
          </div>
        ))
      )}
    </div>
  );
}
