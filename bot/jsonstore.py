"""Local JSON-file fallback with a small Motor-like API (used when MONGO_URL is empty).

Only the subset of MongoDB the bot needs is implemented: equality (incl. "array contains"), $in, $nin,
$ne, $lt/$lte/$gt/$gte, $exists, $or/$and in filters; $set, $unset, $inc, $push, $setOnInsert in updates;
sort/limit/to_list on cursors. Every operation runs without awaiting in between, so on one asyncio loop
each call is atomic, like find_one_and_update on a real server. Every write rewrites the file atomically
(tmp file + os.replace). Good for local development only: one process, small data.
"""

from __future__ import annotations

import copy
import json
import operator
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

_MISSING = object()
_CMP = {"$lt": operator.lt, "$lte": operator.le, "$gt": operator.gt, "$gte": operator.ge}


# ---- (de)serialisation -----------------------------------------------------------------------
def _encode(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return {"$date": obj.isoformat()}
    raise TypeError(f"cannot store {type(obj).__name__} in JSON store")


def _decode(obj: dict) -> Any:
    if len(obj) == 1 and "$date" in obj:
        return datetime.fromisoformat(obj["$date"])
    return obj


# ---- matching --------------------------------------------------------------------------------
def _get(doc: Any, key: str) -> Any:
    cur = doc
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _eq(value: Any, target: Any) -> bool:
    if value is _MISSING:
        return target is None
    if isinstance(value, list) and not isinstance(target, list):
        return target in value
    return value == target


def _match_value(value: Any, cond: Any) -> bool:
    if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
        for op, arg in cond.items():
            if op == "$in":
                ok = any(_eq(value, t) for t in arg)
            elif op == "$nin":
                ok = not any(_eq(value, t) for t in arg)
            elif op == "$ne":
                ok = not _eq(value, arg)
            elif op == "$exists":
                ok = (value is not _MISSING) == bool(arg)
            elif op in _CMP:
                if value is _MISSING or value is None:
                    ok = False
                else:
                    try:
                        ok = _CMP[op](value, arg)
                    except TypeError:
                        ok = False
            else:
                raise NotImplementedError(f"JSON store does not support {op}")
            if not ok:
                return False
        return True
    return _eq(value, cond)


def matches(doc: dict, flt: dict | None) -> bool:
    for key, cond in (flt or {}).items():
        if key == "$or":
            if not any(matches(doc, f) for f in cond):
                return False
        elif key == "$and":
            if not all(matches(doc, f) for f in cond):
                return False
        elif not _match_value(_get(doc, key), cond):
            return False
    return True


# ---- updates ---------------------------------------------------------------------------------
def _set(doc: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    cur = doc
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _unset(doc: dict, key: str) -> None:
    parts = key.split(".")
    cur = doc
    for part in parts[:-1]:
        cur = cur.get(part)
        if not isinstance(cur, dict):
            return
    cur.pop(parts[-1], None)


def apply_update(doc: dict, update: dict, *, inserting: bool = False) -> None:
    for op, fields in update.items():
        if op == "$set":
            for k, v in fields.items():
                _set(doc, k, copy.deepcopy(v))
        elif op == "$setOnInsert":
            if inserting:
                for k, v in fields.items():
                    _set(doc, k, copy.deepcopy(v))
        elif op == "$unset":
            for k in fields:
                _unset(doc, k)
        elif op == "$inc":
            for k, v in fields.items():
                cur = _get(doc, k)
                _set(doc, k, (0 if cur is _MISSING or cur is None else cur) + v)
        elif op == "$push":
            for k, v in fields.items():
                cur = _get(doc, k)
                lst = [] if cur is _MISSING or cur is None else list(cur)
                lst.append(copy.deepcopy(v))
                _set(doc, k, lst)
        else:
            raise NotImplementedError(f"JSON store does not support update {op}")


def _sort_key(value: Any) -> tuple:
    # MongoDB order: missing/null < numbers < strings < dates
    if value is _MISSING or value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (4, value)
    if isinstance(value, int | float):
        return (1, value)
    if isinstance(value, str):
        return (2, value)
    if isinstance(value, datetime):
        return (3, value.timestamp())
    return (5, str(value))


def _normalise_sort(key_or_list: Any, direction: int | None = None) -> list[tuple[str, int]]:
    if isinstance(key_or_list, str):
        return [(key_or_list, direction or 1)]
    return [(k, d) for k, d in key_or_list]


def _sorted(docs: list[dict], spec: list[tuple[str, int]]) -> list[dict]:
    out = list(docs)
    for key, direction in reversed(spec):  # stable multi-key sort
        out.sort(key=lambda d, k=key: _sort_key(_get(d, k)), reverse=direction < 0)
    return out


# ---- public classes --------------------------------------------------------------------------
class JsonCursor:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs
        self._sort: list[tuple[str, int]] = []
        self._limit = 0

    def sort(self, key_or_list: Any, direction: int | None = None) -> JsonCursor:
        self._sort = _normalise_sort(key_or_list, direction)
        return self

    def limit(self, n: int) -> JsonCursor:
        self._limit = n
        return self

    async def to_list(self, length: int | None = None) -> list[dict]:
        docs = _sorted(self._docs, self._sort) if self._sort else list(self._docs)
        caps = [x for x in (self._limit, length) if x]
        if caps:
            docs = docs[: min(caps)]
        return [copy.deepcopy(d) for d in docs]


class JsonCollection:
    def __init__(self, store: JsonStore, name: str) -> None:
        self._store = store
        self.name = name

    @property
    def _docs(self) -> list[dict]:
        return self._store.data.setdefault(self.name, [])

    def _first(self, flt: dict | None, sort: Any = None) -> dict | None:
        found = [d for d in self._docs if matches(d, flt)]
        if sort:
            found = _sorted(found, _normalise_sort(sort))
        return found[0] if found else None

    async def create_index(self, *args: Any, **kwargs: Any) -> str:
        return "noop"

    async def find_one(self, flt: dict | None = None, sort: Any = None) -> dict | None:
        doc = self._first(flt, sort)
        return copy.deepcopy(doc) if doc else None

    def find(self, flt: dict | None = None) -> JsonCursor:
        return JsonCursor([d for d in self._docs if matches(d, flt)])

    async def count_documents(self, flt: dict | None = None) -> int:
        return sum(1 for d in self._docs if matches(d, flt))

    async def insert_one(self, doc: dict) -> SimpleNamespace:
        if "_id" in doc and any(d.get("_id") == doc["_id"] for d in self._docs):
            raise DuplicateKeyError(f"duplicate _id {doc['_id']!r} in {self.name}")
        self._docs.append(copy.deepcopy(doc))
        self._store.save()
        return SimpleNamespace(inserted_id=doc.get("_id"))

    def _upsert_doc(self, flt: dict, update: dict) -> dict:
        doc = {k: copy.deepcopy(v) for k, v in flt.items() if not k.startswith("$") and not isinstance(v, dict)}
        apply_update(doc, update, inserting=True)
        self._docs.append(doc)
        return doc

    async def update_one(self, flt: dict, update: dict, upsert: bool = False) -> SimpleNamespace:
        doc = self._first(flt)
        if doc is None:
            if upsert:
                self._upsert_doc(flt, update)
                self._store.save()
            return SimpleNamespace(matched_count=0, modified_count=0)
        apply_update(doc, update)
        self._store.save()
        return SimpleNamespace(matched_count=1, modified_count=1)

    async def update_many(self, flt: dict, update: dict) -> SimpleNamespace:
        n = 0
        for d in self._docs:
            if matches(d, flt):
                apply_update(d, update)
                n += 1
        if n:
            self._store.save()
        return SimpleNamespace(matched_count=n, modified_count=n)

    async def find_one_and_update(
        self,
        flt: dict,
        update: dict,
        upsert: bool = False,
        return_document: Any = ReturnDocument.BEFORE,
        sort: Any = None,
        **_: Any,
    ) -> dict | None:
        doc = self._first(flt, sort)
        if doc is None:
            if not upsert:
                return None
            doc = self._upsert_doc(flt, update)
            self._store.save()
            return copy.deepcopy(doc) if return_document == ReturnDocument.AFTER else None
        before = copy.deepcopy(doc)
        apply_update(doc, update)
        self._store.save()
        return copy.deepcopy(doc) if return_document == ReturnDocument.AFTER else before

    async def delete_one(self, flt: dict) -> SimpleNamespace:
        doc = self._first(flt)
        if doc is None:
            return SimpleNamespace(deleted_count=0)
        self._docs.remove(doc)
        self._store.save()
        return SimpleNamespace(deleted_count=1)


class JsonStore:
    """A whole "database" in one JSON file: {collection: [documents]}."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.data: dict[str, list[dict]] = {}
        if path and path.exists():
            self.data = json.loads(path.read_text("utf-8"), object_hook=_decode)

    def __getitem__(self, name: str) -> JsonCollection:
        return JsonCollection(self, name)

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, default=_encode, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.path)
