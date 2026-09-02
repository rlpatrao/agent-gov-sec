# Archived documentation

Documents that described a part of the platform that no longer exists move here
rather than being deleted, so their history stays visible to everyone with a
checkout. (The top-level `archive/` directory is local-only and gitignored — it
is not a substitute, because nothing placed there survives a clone.)

Rules:

- A document is archived when the thing it describes is removed or replaced,
  not merely renamed — a rename updates the document in place.
- Move the whole file (`git mv docs/<stack>/<doc>.md docs/archive/`), and add a
  line to the table below saying what replaced it, if anything.
- `scripts/check_doc_refs.py` does not scan this directory, and nothing outside
  it may link into it as if it were current documentation.

| Archived document | Replaced by |
|---|---|
