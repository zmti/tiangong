"""API 安全测试:AGENT_API_KEY 校验依赖。"""
from __future__ import annotations

import asyncio

from fastapi import HTTPException

from app.config import settings
from app.api.security import require_api_key


def test_no_key_configured_allows_all():
    old = settings.agent_api_key
    try:
        settings.agent_api_key = ""
        assert asyncio.run(require_api_key(authorization=None)) is None
        assert asyncio.run(require_api_key(authorization="Bearer anything")) is None
    finally:
        settings.agent_api_key = old


def test_key_configured_rejects_missing():
    old = settings.agent_api_key
    try:
        settings.agent_api_key = "secret"
        try:
            asyncio.run(require_api_key(authorization=None))
            assert False, "应拒绝无 Authorization 请求"
        except HTTPException as e:
            assert e.status_code == 401
    finally:
        settings.agent_api_key = old


def test_key_configured_rejects_wrong():
    old = settings.agent_api_key
    try:
        settings.agent_api_key = "secret"
        try:
            asyncio.run(require_api_key("Bearer wrong"))
            assert False, "应拒绝错误 Key"
        except HTTPException as e:
            assert e.status_code == 401
    finally:
        settings.agent_api_key = old


def test_key_configured_accepts_correct():
    old = settings.agent_api_key
    try:
        settings.agent_api_key = "secret"
        assert asyncio.run(require_api_key("Bearer secret")) is None
    finally:
        settings.agent_api_key = old
