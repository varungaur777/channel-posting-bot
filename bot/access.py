"""Who may submit / approve: env config (SUBMITTER_IDS, APPROVER_IDS, OWNER_ID) plus users the owner
added from Telegram (/adduser or the access-request buttons), stored in ``meta/access``.

Users from env can only be changed by editing the env / GitHub secret; users added from Telegram can be
removed again with /removeuser. The lists are cached in memory (one instance runs at a time - lease).
"""

from __future__ import annotations

import logging

from bot.config import Config
from bot.db import Database

log = logging.getLogger(__name__)

POST, APPROVE = "post", "approve"
ROLES = {"post": (POST,), "approve": (APPROVE,), "both": (POST, APPROVE)}
_FIELD = {POST: "submitters", APPROVE: "approvers"}


class Access:
    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg = cfg
        self.db = db
        self.added: dict[str, set[int]] = {POST: set(), APPROVE: set()}

    async def load(self) -> None:
        doc = await self.db.meta.find_one({"_id": "access"}) or {}
        for role, field in _FIELD.items():
            self.added[role] = {int(x) for x in doc.get(field) or []}
        log.info("access: %d extra submitter(s), %d extra approver(s) from DB",
                 len(self.added[POST]), len(self.added[APPROVE]))

    async def _save(self) -> None:
        await self.db.meta.update_one(
            {"_id": "access"},
            {"$set": {field: sorted(self.added[role]) for role, field in _FIELD.items()}},
            upsert=True,
        )

    # ---- checks --------------------------------------------------------------------------------
    def is_owner(self, uid: int | None) -> bool:
        return self.cfg.is_owner(uid)

    def can_submit(self, uid: int | None) -> bool:
        return self.cfg.can_submit(uid) or (uid in self.added[POST])

    def can_approve(self, uid: int | None) -> bool:
        return self.cfg.can_approve(uid) or (uid in self.added[APPROVE])

    def is_staff(self, uid: int | None) -> bool:
        return self.can_submit(uid) or self.can_approve(uid)

    # ---- changes -------------------------------------------------------------------------------
    async def grant(self, uid: int, roles: tuple[str, ...]) -> list[str]:
        """Add roles; returns the roles that were actually new."""
        new = [r for r in roles if not (self.can_submit(uid) if r == POST else self.can_approve(uid))]
        for r in new:
            self.added[r].add(uid)
        if new:
            await self._save()
            log.info("access granted to %s: %s", uid, new)
        return new

    async def revoke(self, uid: int, roles: tuple[str, ...]) -> tuple[list[str], list[str]]:
        """Remove roles added from Telegram. Returns (removed, kept_because_from_env)."""
        removed = [r for r in roles if uid in self.added[r]]
        env_ids = {POST: self.cfg.submitter_ids, APPROVE: self.cfg.approver_ids}
        from_env = [r for r in roles if uid in env_ids[r] or self.cfg.is_owner(uid)]
        for r in removed:
            self.added[r].discard(uid)
        if removed:
            await self._save()
            log.info("access removed from %s: %s", uid, removed)
        return removed, from_env

    def summary(self) -> dict[str, list[int]]:
        return {
            "env_submitters": sorted(self.cfg.submitter_ids),
            "env_approvers": sorted(self.cfg.approver_ids),
            "added_submitters": sorted(self.added[POST]),
            "added_approvers": sorted(self.added[APPROVE]),
        }
