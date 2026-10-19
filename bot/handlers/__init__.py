"""Telegram update handlers."""

from __future__ import annotations

from telegram.ext import Application

from bot.config import Config
from bot.handlers import admin, approval, submit
from bot.handlers.submit import AlbumBuffer
from bot.service import PostService


def register_all(app: Application, cfg: Config, svc: PostService, albums: AlbumBuffer) -> None:
    admin.register(app, cfg, svc)
    approval.register(app, cfg, svc)
    submit.register(app, cfg, svc, albums)
