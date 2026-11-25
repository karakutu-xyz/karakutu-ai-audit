import { useState } from "react";
import { useApi } from "../api/hooks";
import { CreateBenchmarkDialog } from "../dialogs/CreateBenchmarkDialog";
import { navigate } from "../router";
import { RunsPage } from "./RunsPage";

export function BenchmarksPage() {
  const [open, setOpen] = useState(false);
  const { token, epoch } = useApi();
  return (
    <>
      <RunsPage benchmark onCreate={() => setOpen(true)} />
      {open && (
        <CreateBenchmarkDialog
          key={epoch}
          token={token}
          onClose={() => setOpen(false)}
          onCreated={(runId) => navigate(`/runs/${encodeURIComponent(runId)}`)}
        />
      )}
    </>
  );
}
