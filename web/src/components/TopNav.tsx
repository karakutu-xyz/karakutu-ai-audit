import { Link, useRoute } from "../router";

export function TopNav() {
  const route = useRoute();
  const active =
    route.name === "run"
      ? "runs"
      : route.name === "article"
        ? "articles"
        : route.name;

  return (
    <nav aria-label="Main">
      <ul>
        {["articles", "runs", "benchmarks", "settings"].map((name) => (
          <li key={name}>
            <Link
              href={`/${name}`}
              className={name === "settings" ? "quiet-nav" : undefined}
              aria-current={active === name ? "page" : undefined}
            >
              {name[0].toUpperCase() + name.slice(1)}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
