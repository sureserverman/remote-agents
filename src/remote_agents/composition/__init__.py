"""Composition roots for the service, the backend, and each frontend surface.

A member of the closed root set (DEC-015): modules here may import adapters, application
and config to wire them together, and nothing outside the set may.
"""
