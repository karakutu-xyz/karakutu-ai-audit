import { Link } from "../router";

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="error-box" role="alert">
      <p>{message}</p>
      {message.includes("API token") && (
        <Link href="/settings">Open Settings to add an API token</Link>
      )}
      {onRetry && (
        <button className="text-button" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}
