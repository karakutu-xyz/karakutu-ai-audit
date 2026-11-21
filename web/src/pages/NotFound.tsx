import { Link } from "../router";

export function NotFound() {
  return (
    <section className="placeholder">
      <h1>Page not found</h1>
      <p>
        This address does not match a page. Check the address or return to the
        run ledger.
      </p>
      <Link href="/runs">Open Runs</Link>
    </section>
  );
}
