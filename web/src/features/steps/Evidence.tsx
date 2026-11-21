import type { Step } from "../../types";
import { EmptyState as Empty } from "../../components/EmptyState";
import { Icon } from "../../components/Icon";
import { Json } from "../../components/Json";
import { humanize } from "../../format";

export function Evidence({ step }: { step: Step }) {
  if (!step.evidence?.length)
    return (
      <Empty title="No external evidence in this step" icon="search">
        <p>Fetch, search, probe, and archive requests appear here when made.</p>
      </Empty>
    );
  return (
    <div className="model-calls">
      {step.evidence.map((item, index) => (
        <details key={index} open={index === 0}>
          <summary>
            <Icon name="search" size={15} />
            {humanize(String(item.type ?? "Evidence"))}
            <span>Evidence {index + 1}</span>
          </summary>
          <Json value={item} label="Exact evidence request & result" />
        </details>
      ))}
    </div>
  );
}
