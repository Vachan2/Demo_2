"""
Shared pytest fixtures for the user-service test suite.
"""
from __future__ import annotations

import json
import unittest.mock as mock

import pytest


@pytest.fixture
def sample_profile():
    return {
        "id": 1,
        "username": "alice",
        "email": "alice@example.com",
        "full_name": "Alice Anderson",
        "created_at": "2024-01-01T00:00:00",
    }


@pytest.fixture
def fake_redis(sample_profile):
    """
    Mock Redis client.
    First call to .get() returns None (cache miss).
    Subsequent calls return the serialised profile (cache hit).
    """
    client = mock.AsyncMock()
    client.get.side_effect = [None, json.dumps(sample_profile)]
    client.setex = mock.AsyncMock(return_value=True)
    return client


@pytest.fixture
def fake_pool(sample_profile):
    """Mock asyncpg pool that returns a fake user row."""
    pool = mock.MagicMock()
    conn = mock.AsyncMock()

    row = {
        "id":         sample_profile["id"],
        "username":   sample_profile["username"],
        "email":      sample_profile["email"],
        "full_name":  sample_profile["full_name"],
        "created_at": mock.MagicMock(
            isoformat=lambda: sample_profile["created_at"]
        ),
    }
    conn.fetchrow = mock.AsyncMock(return_value=row)

    cm = mock.AsyncMock()
    cm.__aenter__ = mock.AsyncMock(return_value=conn)
    cm.__aexit__  = mock.AsyncMock(return_value=False)
    pool.acquire  = mock.MagicMock(return_value=cm)
    return pool
