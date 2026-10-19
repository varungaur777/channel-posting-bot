"""Telegram update handlers."""

from __future__ import annotations

from telegram.ext import Application

from bot.access import Access
from bot.config import Config
from bot.handlers import access as access_handlers
from bot.handlers import admin, approval, submit
from bot.handlers.access import AccessRequests
from bot.handlers.submit import AlbumBuffer
from bot.service import PostService


def register_all(app: Application, cfg: Config, svc: PostService, albums: AlbumBuffer, access: Access) -> None:
    requests = AccessRequests(cfg, svc, access)
    admin.register(app, cfg, svc, access, requests)
    approval.register(app, cfg, svc, access)
    access_handlers.register(app, cfg, svc, access)
    submit.register(app, cfg, svc, albums, access, requests)
