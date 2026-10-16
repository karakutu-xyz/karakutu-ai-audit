"""Verify an apply log against the archive — READ-ONLY.

For every audit the log says was written/posted: GET /audits/{id} and check the three IPTC fields
equal the payload we sent; GET /audits/{id}/discussions and check exactly one discussion exists
by our wallet whose text starts with the bot's byline. Prints a summary and any mismatch.

Usage: .venv311/bin/python scripts/open_audit/verify_apply.py runs/<batch>/results-apply_log.json [--sample 40]
"""
import argparse, asyncio, json, random, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO / ".env.lmstudio", override=True)
import httpx  # noqa: E402
from src.clients import get_client  # noqa: E402
from comment_templates import BOT_PREFIX  # noqa: E402

ap = argparse.ArgumentParser(); ap.add_argument("log"); ap.add_argument("--sample", type=int, default=0); ap.add_argument("--seed", type=int, default=1)
args = ap.parse_args()
log = json.loads(Path(args.log).read_text())
entries = {int(k): v for k, v in log.items() if not k.startswith("_")}
ids = [k for k, v in entries.items() if (v.get("tags") or {}).get("ok") or (v.get("comment") or {}).get("ok")]
if args.sample and args.sample < len(ids):
    random.seed(args.seed); ids = random.sample(ids, args.sample)


async def main():
    c = get_client(); tok = await c.auth.get_token(); H = {"Authorization": f"Bearer {tok}"}
    ok_tags = ok_comm = bad = 0; problems = []
    async with httpx.AsyncClient(timeout=60) as hc:
        for aid in ids:
            e = entries[aid]
            r = await hc.get(f"{c.base_url}/audits/{aid}", headers=H)
            if r.status_code != 200:
                problems.append((aid, f"audit GET {r.status_code}")); bad += 1; continue
            a = r.json()
            if (e.get("tags") or {}).get("ok"):
                # after a re-tag (retag_results.py) the archive must hold the CORRECTED list
                p = (e["retag"]["payload"] if (e.get("retag") or {}).get("ok") else e["tags"]["payload"])
                same = (a.get("subject_tag_notations") == p["subject_tag_notations"] and a.get("content_type_notation") == p["content_type_notation"]
                        and a.get("geographic_scope") == p["geographic_scope"])
                if same: ok_tags += 1
                else: problems.append((aid, f"tags differ: server {a.get('subject_tag_notations')}/{a.get('content_type_notation')}/{a.get('geographic_scope')} vs sent {p}")); bad += 1
            if (e.get("comment") or {}).get("ok"):
                d = await hc.get(f"{c.base_url}/audits/{aid}/discussions", params={"page_size": 10}, headers=H)
                items = (d.json().get("items") or []) if d.status_code == 200 else []
                ours = [it for it in items if str(it.get("comment") or it.get("content") or "").startswith(BOT_PREFIX)]
                top = [it for it in ours if not it.get("parent_id")]
                replies = [it for it in ours if it.get("parent_id")]
                want_reply = bool((e.get("retag") or {}).get("ok")) and e["retag"].get("reply_id") is not None
                top_ok = len(top) == 1 and str(e["comment"].get("discussion_id")) in ("None", str(top[0].get("id")))
                reply_ok = (len(replies) == 1 and str(replies[0].get("parent_id")) == str(e["comment"].get("discussion_id"))) if want_reply else len(replies) == 0
                if top_ok and reply_ok:
                    ok_comm += 1
                else:
                    problems.append((aid, f"discussions: {len(items)} total, {len(top)} bot top-level, {len(replies)} bot replies (reply expected: {want_reply}), expected id {e['comment'].get('discussion_id')}")); bad += 1
            await asyncio.sleep(0.15)
    print(f"verified {len(ids)} audits: tags match {ok_tags} · one bot comment {ok_comm} · problems {bad}")
    for p in problems[:20]: print("  ", p)


asyncio.run(main())
