import { VERDICTS } from "../types";
import { verdictLabel } from "./VerdictChip";

export function VerdictSelect({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">No expected verdict</option>
      {VERDICTS.map((verdict) => (
        <option key={verdict} value={verdict}>
          {verdictLabel(verdict)}
        </option>
      ))}
    </select>
  );
}
