"""Managing categories from the chat: add, update (rename, move, aliases, kind, archive) and merge.

Rules in plain words:
- Names and aliases are unique across ALL categories, ignoring letter case, so a word always means one thing.
- At most two levels: a parent can't have a parent, and a category with children can't get one.
  That makes loops impossible and keeps "how much on a parent" simple.
- Nothing is deleted: archive instead. A parent can only be archived when its children are archived or moved.
- Transactions point to a category's id, so a rename changes all history at once.
- Every change is written to the change log (old and new values).
"""

import psycopg
from psycopg.types.json import Jsonb

from app.errors import UserError
from app.models import CategoryChangeOut, CategoryOut, MergeOut
from app.services.resolve import Candidate, normalize, resolve

KINDS = ("regular", "structural", "excluded")


def _all(conn: psycopg.Connection) -> list[tuple]:
    """Every category (also archived): (id, name, kind, parent_id, description, aliases, is_active)."""
    return conn.execute("SELECT id, name, kind, parent_id, description, aliases, is_active FROM categories").fetchall()


def _find(conn: psycopg.Connection, text: str) -> tuple:
    """Exact name or alias (also archived ones, so they can be restored). No guessing for changes."""
    rows = _all(conn)
    m = resolve(text, [Candidate(r[0], r[1], tuple(r[5] or ())) for r in rows], allow_fuzzy=False)
    if m.kind == "none":
        hint = f" Did you mean: {', '.join(m.suggestions)}?" if m.suggestions else ""
        raise UserError("unknown_category", f'No category called "{text}".{hint} Use the exact name.')
    return next(r for r in rows if r[0] == m.candidate.id)


def _taken_words(conn: psycopg.Connection, except_id: int | None = None) -> dict[str, str]:
    """Every name and alias in use (normalized) → the category that uses it."""
    words: dict[str, str] = {}
    for r in _all(conn):
        if r[0] == except_id:
            continue
        for word in (r[1], *(r[5] or ())):
            words[normalize(word)] = r[1]
    return words


def _check_free(conn: psycopg.Connection, words: list[str], except_id: int | None = None) -> None:
    taken = _taken_words(conn, except_id)
    for word in words:
        owner = taken.get(normalize(word))
        if owner:
            raise UserError("name_taken", f'"{word}" is already used by the category "{owner}".')


def _out(conn: psycopg.Connection, category_id: int) -> CategoryChangeOut:
    r = conn.execute(
        "SELECT c.id, c.name, c.kind, p.name, c.description, c.aliases, c.is_active "
        "FROM categories c LEFT JOIN categories p ON p.id = c.parent_id WHERE c.id = %s", (category_id,)).fetchone()
    return CategoryChangeOut(category=CategoryOut(id=r[0], name=r[1], kind=r[2], parent=r[3], description=r[4],
                                                  aliases=list(r[5] or [])), is_active=r[6])


def _log(conn: psycopg.Connection, category_id: int, action: str, old: dict, new: dict) -> None:
    conn.execute("INSERT INTO change_log (entity, entity_id, action, old_values, new_values) "
                 "VALUES ('category', %s, %s, %s, %s)", (category_id, action, Jsonb(old), Jsonb(new)))


def _parent_for(conn: psycopg.Connection, parent: str, child_id: int | None) -> int:
    row = _find(conn, parent)
    pid, pname, _, p_parent, _, _, p_active = row
    if pid == child_id:
        raise UserError("invalid_parent", "A category can't be its own parent.")
    if p_parent is not None:
        raise UserError("invalid_parent", f'"{pname}" is itself inside another category; only two levels are '
                                          "allowed. Choose a top-level category as the parent.")
    if not p_active:
        raise UserError("invalid_parent", f'"{pname}" is archived.')
    return pid


def _clean_aliases(aliases: list[str]) -> list[str]:
    seen, result = set(), []
    for a in aliases:
        a = " ".join(a.split())
        if a and normalize(a) not in seen:
            seen.add(normalize(a))
            result.append(a)
    return result


def add_category(conn: psycopg.Connection, name: str, kind: str = "regular", parent: str | None = None,
                 description: str | None = None, aliases: list[str] | None = None) -> CategoryChangeOut:
    name = " ".join(name.split())
    if not name:
        raise UserError("invalid_name", "The category needs a name.")
    if kind not in KINDS:
        raise UserError("invalid_kind", "kind must be regular, structural or excluded.")
    aliases = _clean_aliases(aliases or [])
    _check_free(conn, [name, *aliases])
    if normalize(name) in {normalize(a) for a in aliases}:
        raise UserError("invalid_alias", "An alias can't be the same as the name.")
    parent_id = _parent_for(conn, parent, None) if parent else None
    cid = conn.execute(
        "INSERT INTO categories (name, kind, parent_id, description, aliases) VALUES (%s, %s, %s, %s, %s) "
        "RETURNING id", (name, kind, parent_id, description, aliases)).fetchone()[0]
    _log(conn, cid, "create", {}, {"name": name, "kind": kind, "parent_id": parent_id, "aliases": aliases})
    return _out(conn, cid)


def update_category(conn: psycopg.Connection, name: str, *, new_name: str | None = None, kind: str | None = None,
                    parent: str | None = None, remove_parent: bool = False, description: str | None = None,
                    add_aliases: list[str] | None = None, remove_aliases: list[str] | None = None,
                    archived: bool | None = None) -> CategoryChangeOut:
    cid, old_name, old_kind, old_parent, old_desc, old_aliases, old_active = _find(conn, name)
    old_aliases = list(old_aliases or [])
    old = {"name": old_name, "kind": old_kind, "parent_id": old_parent, "description": old_desc,
           "aliases": old_aliases, "is_active": old_active}
    new = dict(old)
    warnings: list[str] = []
    has_children = conn.execute("SELECT 1 FROM categories WHERE parent_id = %s AND is_active LIMIT 1",
                                (cid,)).fetchone() is not None

    if new_name is not None:
        new_name = " ".join(new_name.split())
        if not new_name:
            raise UserError("invalid_name", "The new name can't be empty.")
        _check_free(conn, [new_name], except_id=cid)
        new["name"] = new_name
    if kind is not None:
        if kind not in KINDS:
            raise UserError("invalid_kind", "kind must be regular, structural or excluded.")
        if kind != old_kind:
            warnings.append(f"All past transactions in this category now count as '{kind}' in totals and the forecast.")
        new["kind"] = kind
    if parent is not None and remove_parent:
        raise UserError("invalid_parent", "Give either parent or remove_parent, not both.")
    if parent is not None:
        if has_children:
            raise UserError("invalid_parent", "This category has its own subcategories, so it can't go inside "
                                              "another one (only two levels are allowed).")
        new["parent_id"] = _parent_for(conn, parent, cid)
    if remove_parent:
        new["parent_id"] = None
    if description is not None:
        new["description"] = description.strip() or None
    if add_aliases:
        extra = [a for a in _clean_aliases(add_aliases) if normalize(a) not in {normalize(x) for x in old_aliases}]
        _check_free(conn, extra, except_id=cid)
        if normalize(new["name"]) in {normalize(a) for a in extra}:
            raise UserError("invalid_alias", "An alias can't be the same as the name.")
        new["aliases"] = old_aliases + extra
    if remove_aliases:
        gone = {normalize(a) for a in remove_aliases}
        new["aliases"] = [a for a in new["aliases"] if normalize(a) not in gone]
    if archived is not None:
        if archived and has_children:
            raise UserError("has_children", "Archive or move its subcategories first.")
        if not archived and new["parent_id"]:
            parent_active = conn.execute("SELECT is_active FROM categories WHERE id = %s",
                                         (new["parent_id"],)).fetchone()[0]
            if not parent_active:
                raise UserError("parent_archived", "Its parent is archived: restore the parent first.")
        new["is_active"] = not archived

    changed = {k: v for k, v in new.items() if v != old[k]}
    if not changed:
        raise UserError("no_change", "Nothing to change: the values are the same as before.")
    conn.execute(
        "UPDATE categories SET name = %s, kind = %s, parent_id = %s, description = %s, aliases = %s, is_active = %s "
        "WHERE id = %s",
        (new["name"], new["kind"], new["parent_id"], new["description"], new["aliases"], new["is_active"], cid))
    _log(conn, cid, "update", {k: old[k] for k in changed}, changed)
    result = _out(conn, cid)
    result.warnings = warnings
    return result


def merge_categories(conn: psycopg.Connection, source: str, target: str) -> MergeOut:
    """Moves every transaction from source to target, archives source, and keeps its name as an alias of
    target (so old words still work). The moved ids are logged, so the merge can be undone by hand."""
    sid, sname, skind, _, _, saliases, sactive = _find(conn, source)
    tid, tname, tkind, _, _, taliases, tactive = _find(conn, target)
    if sid == tid:
        raise UserError("same_category", "Source and target are the same category.")
    if not tactive:
        raise UserError("target_archived", f'"{tname}" is archived: restore it first.')
    if conn.execute("SELECT 1 FROM categories WHERE parent_id = %s AND is_active LIMIT 1", (sid,)).fetchone():
        raise UserError("has_children", f'"{sname}" has subcategories: move or merge them first.')
    warnings = []
    if skind != tkind:
        warnings.append(f"The moved transactions now count as '{tkind}' (they were '{skind}').")

    moved = [r[0] for r in conn.execute(
        "UPDATE transactions SET category_id = %s, updated_at = now() WHERE category_id = %s RETURNING id",
        (tid, sid))]
    new_aliases = list(taliases or [])
    for word in (sname, *(saliases or ())):
        if normalize(word) not in {normalize(a) for a in new_aliases} and normalize(word) != normalize(tname):
            new_aliases.append(word)
    conn.execute("UPDATE categories SET aliases = '{}', is_active = FALSE WHERE id = %s", (sid,))
    conn.execute("UPDATE categories SET aliases = %s WHERE id = %s", (new_aliases, tid))
    _log(conn, sid, "merge", {"is_active": sactive, "aliases": list(saliases or [])},
         {"merged_into": tid, "moved_transaction_ids": moved})
    _log(conn, tid, "merge_target", {"aliases": list(taliases or [])}, {"aliases": new_aliases, "from": sid})
    return MergeOut(source=sname, target=tname, moved_transactions=len(moved), warnings=warnings)
