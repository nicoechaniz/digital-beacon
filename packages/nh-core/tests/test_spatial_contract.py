"""Tests for the Partial.spatial contract: spatial params only, never metadata bag."""
import pytest

from nh_core import (
    ALLOWED_KEYS,
    SPATIAL_KEYS,
    TRANSITIONAL_KEYS,
    is_spatial_valid,
    validate_spatial,
)


def test_spatial_keys_are_known():
    """Every key in SPATIAL_KEYS is in the allowed set."""
    for key in SPATIAL_KEYS:
        assert key in ALLOWED_KEYS


def test_spatial_none_is_valid():
    assert is_spatial_valid(None)
    assert validate_spatial(None) == []


def test_spatial_empty_is_valid():
    assert is_spatial_valid({})
    assert validate_spatial({}) == []


def test_spatial_legal_keys():
    spatial = {"az": 0.0, "dist": 1.0, "q": 0.5, "on": True, "solo": False}
    assert is_spatial_valid(spatial)
    assert validate_spatial(spatial) == []


def test_spatial_transitional_keys():
    spatial = {"az": 30.0, "beacon_gain": 0.8, "active": True}
    assert is_spatial_valid(spatial)
    assert validate_spatial(spatial) == []


def test_spatial_rejects_metadata_key():
    spatial = {"az": 0.0, "source_name": "frogs", "comments": "loud section"}
    assert not is_spatial_valid(spatial)
    errors = validate_spatial(spatial)
    assert len(errors) == 2
    assert "source_name" in errors[0]
    assert "comments" in errors[1]


def test_spatial_rejects_nested_dict():
    spatial = {"az": 0.0, "metadata": {"author": "anon"}}
    assert not is_spatial_valid(spatial)
    errors = validate_spatial(spatial)
    assert len(errors) == 1
    assert "metadata" in errors[0]


def test_spatial_subset_is_valid():
    spatial = {"az": 45.0, "dist": 0.5}
    assert is_spatial_valid(spatial)
    assert validate_spatial(spatial) == []


def test_allowed_keys_union():
    """Ensure ALLOWED_KEYS is the union of SPATIAL_KEYS and TRANSITIONAL_KEYS."""
    assert ALLOWED_KEYS == (SPATIAL_KEYS | TRANSITIONAL_KEYS)


def test_no_overlap():
    """Transitional and spatial key sets must be disjoint."""
    assert SPATIAL_KEYS & TRANSITIONAL_KEYS == set()
