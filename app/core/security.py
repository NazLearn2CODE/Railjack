"""Blueprint §2.2 security layers (L1/L2/L4), enforced at the PreToolUse choke point.

L1 WorkspaceBoundary — Write/Edit paths must resolve inside an allowed root.
L2 ShellPolicy      — catastrophic Bash commands hard-deny before the shell.
L4 ToolReceiptLedger— HMAC-SHA256 receipt per gated tool call (tamper-evident audit log).

L3 (Landlock/Bubblewrap/Docker) is deferred — different enforcement point, own plan.

Design notes:
- evaluate() is PURE (no I/O); the agent hook owns the ledger write.
- L1/L2 hard-deny OUTRANKS operator approval: irrecoverable actions never reach the
  approval card. L4 mints one receipt per gated call regardless of outcome.
- Receipts cover Bash|Write|Edit only (the matcher is CLI-enforced on Bash|Write|Edit);
  read-only tools carry no hook and produce no receipts in v1 — a known audit gap.
"""
import os
import re
import json
import time
import hmac
import hashlib
import secrets
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("orbiter.security")


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of SecurityPolicy.evaluate. hard_deny=True ⇒ skip operator approval."""
    allow: bool
    reason: str = ""
    hard_deny: bool = False


# --------------------------------------------------------------------------- L1
class WorkspaceBoundary:
    """Restricts file paths to a set of resolved workspace roots.

    A path is allowed iff, after ~ expansion + absolutization + symlink-resolving
    resolve(strict=False), it is relative_to some root. strict=False tolerates
    Edit targets that don't exist yet; resolve() still follows symlinks, so a
    in-root symlink that points outside is correctly rejected.
    """
    def __init__(self, roots: list[Path]):
        self.roots = [Path(r).resolve(strict=False) for r in roots]

    def check(self, path: str) -> tuple[bool, str]:
        if not path:
            return True, ""  # nothing to constrain (e.g. Bash carries no file_path)
        target = Path(os.path.expanduser(path))
        if not target.is_absolute():
            target = Path.cwd() / target
        target = target.resolve(strict=False)
        for root in self.roots:
            try:
                target.relative_to(root)
                return True, ""
            except ValueError:
                continue
        return False, f"path '{target}' is outside the workspace boundary {self.roots}"


# --------------------------------------------------------------------------- L2
# Catastrophic-only tripwire: a command listed here hard-denies WITHOUT operator
# review. Risky-but-legitimate commands (rm -rf <project path>, sudo, pip install,
# network egress) are intentionally absent — those stay on the approval path.
# Order matters only for the returned description; first regex hit wins.
_DANGEROUS_SHELL: list[tuple[str, str]] = [
    (r"\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*\s+)+/(?:\s|$)", "recursive rm on filesystem root"),
    (
        r"\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*\s+)+/(?:boot|etc|usr|var|bin|sbin|lib|lib64|root|home|proc|sys|dev|opt|run)(?:\s|$)",
        "recursive rm on a root-critical directory",
    ),
    (r"\bmkfs(?:\.\w+)?\b", "filesystem reformat (mkfs)"),
    (r"\bdd\b[^|]*\bof=/dev/(?:sd[a-z]+|nvme\d+n\d+|vd[a-z]+|hd[a-z]+|disk\d+|mmcblk\d+)", "dd overwrite of a block device"),
    (r">\s*/dev/(?:sd[a-z]+|nvme\d+n\d+|vd[a-z]+|hd[a-z]+|disk\d+|mmcblk\d+)", "redirect to a block device"),
    (r"\bchmod\s+-R\s+[0-7]{3,4}\s+/(?:\s|$)", "recursive chmod on filesystem root"),
    (r":\s*\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\b(?:curl|wget)\b[^|]*\|\s*(?:sh|bash|zsh|dash|ksh)\b", "pipe remote content to a shell"),
    (r"\bchmod\b[^|]*(?:[ug]?[+\-]s|[46][0-7]{3})[^|]*/(?:bin|sbin)/", "setuid/setgid bit on a shell/system binary"),
    (r"(?:>>?)\s*/(?:root/\.ssh/authorized_keys|etc/shadow|etc/passwd|etc/sudoers)\b", "tamper with shadow/passwd/sudoers/authorized_keys"),
    (r"\bsystemctl\b[^|]*\benable\b[^|]*--now", "systemd enable+start persistence"),
]


class ShellPolicy:
    """Pattern blocklist for catastrophic shell commands. Not a command-review surface."""
    def __init__(self):
        self._compiled = [(re.compile(rx, re.IGNORECASE), desc) for rx, desc in _DANGEROUS_SHELL]

    def is_dangerous(self, command: str) -> tuple[bool, Optional[str]]:
        if not command:
            return False, None
        for rx, desc in self._compiled:
            if rx.search(command):
                return True, desc
        return False, None


# --------------------------------------------------------------------------- L4
def receipt_digest(secret: str, payload: dict[str, Any]) -> str:
    """HMAC-SHA256 over canonical (sorted, compact) JSON. Field-order independent."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


class ToolReceiptLedger:
    """Append-only, HMAC-signed audit log of gated tool calls.

    mint_and_append is SYNC and NON-FATAL: any failure (serialization, missing dir,
    unwritable path) is logged and returns None — a broken audit log is a gap; a tool
    call that breaks because the log write failed is an outage. No fsync, no executor:
    premature for single-user local.
    """
    def __init__(self, secret: str, log_path: Path):
        self.secret = secret or secrets.token_hex(32)  # empty -> per-process random
        self.log_path = Path(log_path)

    def mint_and_append(
        self, session_id: str, tool_name: str, tool_input: Any, decision: str, ts: Optional[float] = None
    ) -> Optional[dict]:
        ts = time.time() if ts is None else ts
        payload = {
            "session_id": session_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "decision": decision,
            "ts": ts,
        }
        try:
            receipt = {**payload, "digest": receipt_digest(self.secret, payload)}
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(receipt) + "\n")
            return receipt
        except Exception as e:  # noqa: BLE001 — audit must never break a tool call
            logger.error("receipt write failed (non-fatal): %s", e)
            return None


# -------------------------------------------------------------- facade (L1+L2+L4)
class SecurityPolicy:
    """Composes L1/L2/L4. evaluate() is pure; the hook calls mint_and_append_receipt."""
    def __init__(self, boundary: WorkspaceBoundary, shell: ShellPolicy, ledger: ToolReceiptLedger):
        self.boundary = boundary
        self.shell = shell
        self.ledger = ledger

    def evaluate(self, tool_name: str, tool_input: Any, session_id: str) -> PolicyDecision:
        # L2 — catastrophic shell commands hard-deny outright.
        if tool_name == "Bash":
            command = (tool_input or {}).get("command", "")
            dangerous, desc = self.shell.is_dangerous(command)
            if dangerous:
                return PolicyDecision(allow=False, reason=f"blocked shell command ({desc})", hard_deny=True)
            return PolicyDecision(allow=True)  # benign Bash -> operator approval

        # L1 — Write/Edit paths must resolve inside a workspace root.
        if tool_name in ("Write", "Edit"):
            path = (tool_input or {}).get("file_path", "")
            ok, reason = self.boundary.check(path)
            if not ok:
                return PolicyDecision(allow=False, reason=reason, hard_deny=True)
            return PolicyDecision(allow=True)  # in-bounds -> operator approval

        # Read-only / unknown tools: no policy floor (would need matcher widen for L4).
        return PolicyDecision(allow=True)

    def mint_and_append_receipt(
        self, session_id: str, tool_name: str, tool_input: Any, decision: str
    ) -> Optional[dict]:
        return self.ledger.mint_and_append(session_id, tool_name, tool_input, decision)
