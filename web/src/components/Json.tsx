import { useState } from "react";

export function Json({ value, label = "JSON" }: { value: unknown; label?: string }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const text = JSON.stringify(value, null, 2) ?? "null";

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setCopyError(false);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopyError(true);
      setCopied(false);
    }
  }
  return (
    <div className="json-inspector">
      <div className="inspector-bar">
        <span>{label}</span>
        <button className="text-button" onClick={copy}>
          {copied ? "Copied" : "Copy JSON"}
        </button>
      </div>
      {copyError && (
        <p role="status">
          Could not copy. Select the JSON below to copy it manually.
        </p>
      )}
      <pre tabIndex={0}>{text}</pre>
    </div>
  );
}
