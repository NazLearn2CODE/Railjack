import { useStore } from "../store";

export default function ServiceFrame() {
  const embed = useStore((s) => s.embed);
  const setEmbed = useStore((s) => s.setEmbed);

  if (!embed) return null;

  return (
    <div className="flex h-full w-full flex-col bg-void">
      <div className="flex items-center justify-between border-b border-edge px-3 py-2 bg-panel">
        <span className="label">
          <span className="text-signal">▸</span> SERVICE / <span className="text-phosphor-dim">{embed.name}</span>
        </span>
        <button
          onClick={() => setEmbed(null)}
          className="btn text-critical !px-2 !py-0.5 !text-[10px]"
        >
          ✕ CLOSE
        </button>
      </div>
      <div className="flex-1 w-full relative min-h-0">
        <iframe
          src={embed.url}
          className="absolute inset-0 w-full h-full border-0 bg-white"
          title={embed.name}
          allow="cross-origin-isolated"
        />
      </div>
    </div>
  );
}
