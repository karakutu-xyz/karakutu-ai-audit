import { useEffect } from "react";
import { ApiProvider } from "./api/context";
import { Shell } from "./components/Shell";
import {
  useRoute,
  navigate,
  usePageNavigation,
  useScrollRestoration,
} from "./router";
import { RunsPage } from "./pages/RunsPage";
import { RunDetailPage } from "./pages/RunDetailPage";
import { BenchmarksPage } from "./pages/BenchmarksPage";
import { SettingsPage } from "./pages/SettingsPage";
import { ArticlesPage } from "./pages/ArticlesPage";
import { ArticleDetailPage } from "./pages/ArticleDetailPage";
import { NotFound } from "./pages/NotFound";

function RoutePage() {
  const route = useRoute();
  usePageNavigation();
  useScrollRestoration(route.name === "not-found");
  useEffect(() => {
    if (route.name === "root") navigate("/articles", { replace: true });
    const title =
      route.name === "run"
        ? `${route.params.id} · Runs`
        : route.name === "article"
          ? `${route.params.ref} · Articles`
          : route.name === "not-found"
            ? "Not found"
            : route.name === "root"
              ? "Articles"
              : route.name[0].toUpperCase() + route.name.slice(1);
    document.title = `${title} · Karakutu audit`;
  }, [route.name, route.params.id, route.params.ref]);

  switch (route.name) {
    case "root":
      return null;
    case "runs":
      return <RunsPage />;
    case "run":
      return <RunDetailPage key={route.params.id} id={route.params.id!} />;
    case "benchmarks":
      return <BenchmarksPage />;
    case "settings":
      return <SettingsPage />;
    case "articles":
      return <ArticlesPage />;
    case "article":
      return (
        <ArticleDetailPage key={route.params.ref} articleRef={route.params.ref!} />
      );
    default:
      return <NotFound />;
  }
}

function Workbench() {
  return (
    <Shell>
      <RoutePage />
    </Shell>
  );
}

export default function App() {
  return (
    <ApiProvider>
      <Workbench />
    </ApiProvider>
  );
}
