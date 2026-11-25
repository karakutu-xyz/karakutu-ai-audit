import type { Step } from "../../types";
import { EmptyState as Empty } from "../../components/EmptyState";
import { Icon } from "../../components/Icon";
import { Json } from "../../components/Json";

export function ModelCalls({ step, promptsOnly = false }: {
  step: Step;
  promptsOnly?: boolean;
}) {
  if (!step.model_calls.length)
    return (
      <Empty title="No model calls in this step" icon="code">
        <p>
          This step may use deterministic logic, or has not called the model
          yet.
        </p>
      </Empty>
    );
  if (promptsOnly) {
    return (
      <div className="model-calls">
        <h3>Exact model prompt</h3>
        {step.model_calls.map((call, index) => {
          const request = call.request && typeof call.request === "object"
            ? call.request as Record<string, unknown> : null;
          const messages = Array.isArray(request?.messages)
            ? request.messages.filter((message: unknown) => {
                if (!message || typeof message !== "object") return false;
                const role = (message as Record<string, unknown>).role;
                return role === "system" || role === "user";
              }) : null;
          return <Json key={index} value={messages}
            label={`Model call ${index + 1}: system and user messages`} />;
        })}
      </div>
    );
  }
  return (
    <div className="model-calls">
      {step.model_calls.map((call, index) => (
        <details key={index} open={index === 0}>
          <summary>
            <Icon name="code" size={15} /> Model call {index + 1}
            <span>Request & response</span>
          </summary>
          {"request" in call ? (
            <>
              <Json value={call.request} label="Exact model request" />
              <Json
                value={"response" in call ? call.response : call.raw_completion}
                label="Raw model response"
              />
              {"parsed_output" in call && (
                <Json value={call.parsed_output} label="Parsed model output" />
              )}
              <Json
                value={Object.fromEntries(
                  Object.entries(call).filter(
                    ([key]) =>
                      ![
                        "request",
                        "response",
                        "raw_completion",
                        "parsed_output",
                      ].includes(key),
                  ),
                )}
                label="Call metadata, usage & errors"
              />
            </>
          ) : (
            <Json value={call} label="Complete model call" />
          )}
        </details>
      ))}
    </div>
  );
}
