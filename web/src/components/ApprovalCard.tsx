// Shared operator-approval card. Presentational: the caller binds onResolve to
// whichever session (top-level active or a registered worker) the gate belongs to.
export default function ApprovalCard({
  name,
  input,
  onResolve,
}: {
  name: string;
  input: unknown;
  onResolve: (approve: boolean) => void;
}) {
  return (
    <div className="row-in border border-hazard/50 bg-hazard/5 px-3 py-2.5">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="label">
          <span className="text-hazard">⚠ APPROVAL REQUIRED</span> · {name}
        </span>
        <span className="label text-hazard">BLOCKING</span>
      </div>
      <pre className="mb-2 overflow-x-auto text-[11px] text-phosphor-dim">
        {typeof input === "string" ? input : JSON.stringify(input, null, 2)}
      </pre>
      <div className="flex gap-2">
        <button className="btn btn--hazard flex-1" onClick={() => onResolve(true)}>
          APPROVE
        </button>
        <button className="btn btn--crit flex-1" onClick={() => onResolve(false)}>
          DENY
        </button>
      </div>
    </div>
  );
}
